"""Isolated PostgreSQL race and persistence acceptance; never reset a business DB."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from alembic import command
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.database import Base, create_database_engine, resolve_database_url
from app import models as m
from app.mall import qualification_service as q
from test_member_qualifications import seed, activation, NOW, START, END
from test_postgresql_migration import (validate_postgresql_test_database_url,
    get_database_state, build_alembic_config)


class PostgreSQLMemberQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured=os.environ.get("POSTGRES_TEST_DATABASE_URL", "")
        if not configured:
            raise unittest.SkipTest("未配置独立 PostgreSQL 测试数据库")
        if os.environ.get("POSTGRES_TEST_ALLOW_RESET") != "1":
            raise RuntimeError("必须显式允许重置独立测试库")
        cls.url=validate_postgresql_test_database_url(configured, resolve_database_url())

    def test_confirmation_and_update_races_activation_and_reconnection(self):
        tables, revision = get_database_state(self.url)
        self.assertFalse(tables-{"alembic_version"}); self.assertIsNone(revision)
        config=build_alembic_config(self.url)
        command.upgrade(config,"head")
        engine=create_database_engine(self.url)
        Session=sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with Session() as db:
                actor, partner, batch, business=seed(db)
                second_actor=m.User(username="virtual-other-operator",password_hash="test",role="admin",admin_level="operator",is_active=True)
                db.add(second_actor); db.commit()
                actor_ids=[actor.id,second_actor.id]; batch_id,business_id=batch.id,business.id
            start=Barrier(2, timeout=20)
            def confirm(actor_id):
                with Session.begin() as db:
                    db.execute(text("SET LOCAL lock_timeout='15s'")); start.wait()
                    return q.confirm(db,actor_id=actor_id,batch_id=batch_id,business_ids=[business_id],valid_from=START,valid_until=END,reason="虚拟并发确认",now=NOW)
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertCountEqual(list(pool.map(confirm,actor_ids)),[1,0])
            with Session.begin() as db:
                business=db.get(m.BusinessRecord,business_id)
                self.assertTrue(activation(db,business).success)
                row=db.query(m.MemberQualification).one()
                qualification_id,member_id,version=row.id,row.member_id,row.version
                self.assertTrue(q.has_effective_qualification(db,member_id,now=NOW))
                self.assertEqual(db.query(m.MemberQualificationEvent).count(),2)
            start=Barrier(2, timeout=20)
            def disable(actor_id):
                try:
                    with Session.begin() as db:
                        db.execute(text("SET LOCAL lock_timeout='15s'")); start.wait()
                        q.update(db,actor_id=actor_id,qualification_id=qualification_id,version=version,action="DISABLE",reason="虚拟并发停用",now=NOW)
                    return "updated"
                except ValueError as exc:
                    self.assertIn("已被更新",str(exc)); return "stale"
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertCountEqual(list(pool.map(disable,actor_ids)),["updated","stale"])
            engine.dispose()  # New connections must see persisted revocation and audit.
            with Session() as db:
                row=db.get(m.MemberQualification,qualification_id)
                self.assertFalse(q.has_effective_qualification(db,member_id,now=NOW))
                self.assertEqual(db.query(m.MemberQualificationEvent).count(),3)
                self.assertEqual(q.local_time(row.valid_until),END)
                self.assertEqual(db.query(m.PointsGrant).count(),1)
                self.assertTrue(db.get(m.Member,member_id).is_active)
                q.update(db,actor_id=actor_ids[0],qualification_id=row.id,version=row.version,action="ENABLE",reason="虚拟恢复",now=NOW)
                db.commit()
            with Session() as db:
                self.assertTrue(q.has_effective_qualification(db,member_id,now=NOW))
                self.assertFalse(q.has_effective_qualification(db,member_id,now=END))
            with self.assertRaisesRegex(RuntimeError,"拒绝删除留痕"):
                command.downgrade(config,"0017_backoffice_sessions")
        finally:
            # This database was proven empty before the test and exists only in
            # the disposable isolated container used by the rehearsal runner.
            Base.metadata.drop_all(engine)
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM alembic_version"))
            engine.dispose()
        tables, revision=get_database_state(self.url)
        self.assertEqual(tables,{"alembic_version"}); self.assertIsNone(revision)


if __name__ == "__main__":
    unittest.main()
