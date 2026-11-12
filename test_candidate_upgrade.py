"""Synthetic 0018 -> 0021 data-preservation and non-destructive recovery checks."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine,inspect,MetaData,Table,select,text
from sqlalchemy.orm import sessionmaker
from app.models import User,BusinessRecord,Member,HomeBanner,HomeEntryImage,AdminPhoneLimit
from app.time_utils import utc8_now
from app.schema_readiness import assert_database_schema_ready,DatabaseSchemaNotReadyError

BASE='0018_member_qualifications'
HEAD='0021_admin_phone_login'

def fingerprint(engine,names):
    metadata=MetaData()
    with engine.connect() as c:
        rows={name:sorted([json.dumps(dict(r),sort_keys=True,default=str) for r in c.execute(select(Table(name,metadata,autoload_with=engine))).mappings()]) for name in names}
    return hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()

def seed(factory):
    with factory.begin() as db:
        user=User(username='virtual-release-operator',password_hash='synthetic-hash-only',role='admin',admin_level='operator',is_active=True)
        db.add(user);db.flush()
        db.add(Member(member_public_id='MEM-RELEASE-VIRTUAL',is_active=True))
        db.add(BusinessRecord(user_id=user.id,public_business_no='RELEASE-VIRTUAL-001',name='虚拟升级验证',points_amount=100))
        return user.id

def upgrade_checks(engine,config,actor,names,before):
    command.upgrade(config,HEAD);command.check(config);assert_database_schema_ready(engine)
    assert fingerprint(engine,names)==before,'Existing business rows changed'
    tables=('home_entry_images','home_banners','admin_phone_bindings','admin_phone_challenges','admin_phone_limits')
    with engine.connect() as c:
        assert all(c.execute(text('SELECT COUNT(*) FROM '+table)).scalar()==0 for table in tables)
    factory=sessionmaker(bind=engine)
    now=utc8_now()
    with factory.begin() as db:
        db.add(HomeEntryImage(entry_key='home',image_path='/uploads/mall_home/home/'+'a'*32+'.webp',revision=1,updated_by_id=actor,updated_at=now))
        db.add(HomeBanner(slot=1,title='虚拟发布轮播',image_path='/uploads/mall_banners/1/'+'b'*32+'.webp',sort_order=10,enabled=False,target_category='',revision=1,updated_by_id=actor,updated_at=now))
        db.add(AdminPhoneLimit(key_digest='c'*64,count=1,expires_at=9999999999))
    all_names=[n for n in inspect(engine).get_table_names() if n!='alembic_version']
    persisted=fingerprint(engine,all_names)
    try:command.downgrade(config,BASE)
    except RuntimeError:pass
    else:raise AssertionError('Nonempty downgrade was allowed')
    assert fingerprint(engine,all_names)==persisted
    assert_database_schema_ready(engine)
    return dict(old_tables=len(names),existing_rows_preserved=True,new_tables_initially_empty=True,
                no_automatic_phone_binding=True,nonempty_downgrade_blocked=True,failed_downgrade_preserves_candidate=True)

class CandidateUpgradeTests(unittest.TestCase):
    def test_upgrade_preserves_old_rows_and_recovery_uses_separate_preupgrade_copy(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);source=root/'candidate.db';point=root/'preupgrade.db';restored=root/'restored.db'
            url='sqlite:///'+source.as_posix();engine=create_engine(url)
            config=Config('alembic.ini');config.attributes['database_url']=url
            try:
                command.upgrade(config,BASE);actor=seed(sessionmaker(bind=engine))
                names=[n for n in inspect(engine).get_table_names() if n!='alembic_version'];before=fingerprint(engine,names)
                with closing(sqlite3.connect(source)) as src,closing(sqlite3.connect(point)) as dest:src.backup(dest)
                original_digest=hashlib.sha256(point.read_bytes()).hexdigest()
                result=upgrade_checks(engine,config,actor,names,before)
                # Keep post-upgrade rows; recovery goes to a NEW database, never over the candidate.
                shutil.copy2(point,restored);recovery=create_engine('sqlite:///'+restored.as_posix())
                try:
                    self.assertEqual(fingerprint(recovery,names),before)
                    with recovery.connect() as c:self.assertEqual(c.execute(text('SELECT version_num FROM alembic_version')).scalar(),BASE)
                    with self.assertRaises(DatabaseSchemaNotReadyError):assert_database_schema_ready(recovery)
                    self.assertNotIn('home_banners',inspect(recovery).get_table_names())
                finally:recovery.dispose()
                self.assertEqual(hashlib.sha256(point.read_bytes()).hexdigest(),original_digest)
                with engine.connect() as c:self.assertEqual(c.execute(text('SELECT COUNT(*) FROM home_banners')).scalar(),1)
                print(json.dumps({**result,'preupgrade_point_unchanged':True,'separate_recovery_old_rows_match':True,
                    'new_code_rejects_old_schema':True,'candidate_edits_preserved':True,'real_database_access':False}))
            finally:engine.dispose()

if __name__=='__main__':unittest.main()
