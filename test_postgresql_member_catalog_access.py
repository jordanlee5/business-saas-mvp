"""Isolated PG HTTP acceptance, cross-process revocation and checkout locking."""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
import unittest

from alembic import command
from sqlalchemy import text

from app.database import Base, create_database_engine, resolve_database_url
from app import models as m
from app.mall import qualification_service as q
from app.api import catalog_access as access
import test_member_catalog_access as cases
import test_order_service as order_fixture
from test_postgresql_migration import validate_postgresql_test_database_url, get_database_state, build_alembic_config


class PostgreSQLMemberCatalogAccessTests(cases.MemberCatalogAccessTests):
    def test_client_entry_status_with_postgresql_timestamps(self):
        path = '/api/miniprogram/v1/catalog-access'
        self.assertEqual(self.request(path, owner=None).status_code, 401)
        self.assertEqual(self.request(path, owner=1).json()['status'], 'unconfirmed')
        self.assertEqual(self.request(path).json(), {'status':'eligible','valid_for_seconds':900})
        self.change('DISABLE')
        self.assertEqual(self.request(path).json()['status'], 'disabled')
        self.change('ENABLE')
        self.assertEqual(self.request(path).json()['status'], 'eligible')

    @classmethod
    def setUpClass(cls):
        configured=os.environ.get("POSTGRES_TEST_DATABASE_URL","")
        if not configured: raise unittest.SkipTest("未配置独立 PostgreSQL 测试数据库")
        if os.environ.get("POSTGRES_TEST_ALLOW_RESET")!="1": raise RuntimeError("必须显式允许独立测试库重置")
        cls.database_url=validate_postgresql_test_database_url(configured,resolve_database_url())

    def setUp(self):
        tables,revision=get_database_state(self.database_url)
        if tables-{"alembic_version"} or revision is not None: raise RuntimeError("测试库非空，停止")
        config=build_alembic_config(self.database_url);command.upgrade(config,"head")
        engine=create_database_engine(self.database_url)
        def cleanup():
            with engine.begin() as connection:
                for table in reversed(Base.metadata.sorted_tables): connection.execute(table.delete())
            engine.dispose();command.downgrade(config,"base")
            tables,revision=get_database_state(self.database_url)
            self.assertEqual(tables,{"alembic_version"});self.assertIsNone(revision)
        self.addCleanup(cleanup)
        with patch("test_order_service.create_engine",return_value=engine): super().setUp()

    def test_already_running_other_process_observes_revocation(self):
        # Bearer supplied via stdin, never arguments/output/evidence. The worker
        # remains alive across the independent operator transaction.
        code=r'''
import json,os,sys
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.database import create_database_engine
from app import main
from app.api import miniprogram_v1,member_addresses,catalog_access
from app.mall import product_media_storage
import test_order_service
state=json.loads(sys.stdin.readline())
engine=create_database_engine(os.environ['POSTGRES_TEST_DATABASE_URL'])
with patch.object(miniprogram_v1,'SessionLocal',sessionmaker(bind=engine)), patch.object(member_addresses,'engine',engine), patch.object(main,'get_current_user',return_value=None), patch.object(catalog_access,'utc8_now',return_value=test_order_service.NOW), patch.object(product_media_storage,'PRODUCT_IMAGE_STORAGE_ROOT',state['storage']):
    client=TestClient(main.app)
    def probe():
        return [client.get(path,headers={'Authorization':'Bearer '+state['token']}).status_code for path in ['/api/miniprogram/v1/products',state['image'],'/api/miniprogram/v1/orders']]
    print(json.dumps(probe()),flush=True)
    assert sys.stdin.readline().strip()=='probe'
    print(json.dumps(probe()),flush=True)
    client.close()
engine.dispose()
'''
        child=subprocess.Popen([sys.executable,"-c",code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            child.stdin.write(json.dumps(dict(token=self.case.tokens[0],storage=str(self.storage),image=self.thumb))+"\n");child.stdin.flush()
            self.assertEqual(json.loads(child.stdout.readline()),[200,200,200])
            self.change("DISABLE")
            out,err=child.communicate("probe\n",timeout=30)
            self.assertEqual(child.returncode,0,err)
            self.assertEqual(json.loads(out),[403,403,200])
        finally:
            if child.poll() is None: child.kill();child.communicate()

    def test_revocation_committing_first_blocks_waiting_new_checkout(self):
        ready=Event();pid=[];before=self.balances()
        original=access.require_qualification
        def gated(db,member_id,**kwargs):
            pid.append(db.execute(text("select pg_backend_pid()")).scalar_one());ready.set()
            return original(db,member_id,**kwargs)
        with self.f.Session() as admin:
            row=admin.query(m.MemberQualification).one()
            q.update(admin,actor_id=self.f.operator.id,qualification_id=row.id,version=row.version,
                     action="DISABLE",reason="虚拟并发停用",now=order_fixture.NOW)
            with patch("app.api.member_orders.require_qualification",side_effect=gated), ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(self.case.call,"POST",body=self.case.body)
                try:
                    self.assertTrue(ready.wait(10))
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        with self.f.engine.connect() as monitor:
                            wait=monitor.execute(text("select wait_event_type from pg_stat_activity where pid=:pid"),{"pid":pid[0]}).scalar()
                        if wait=="Lock": break
                        time.sleep(.03)
                    else: self.fail("新兑换未等待资格锁")
                finally:
                    admin.commit()
                self.assertEqual(future.result(timeout=15).status_code,403)
        self.assertEqual(self.balances(),before)


if __name__=="__main__": unittest.main()
