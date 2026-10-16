"""在独立空白 PostgreSQL 测试库验收会员私有积分流水接口。"""

import os
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from alembic import command
from fastapi import HTTPException, Response
from sqlalchemy.orm import sessionmaker

from app.api.miniprogram_v1 import get_member_points_ledger
from app.database import create_database_engine, resolve_database_url
from app.mall.miniprogram_session_service import issue_session
from app.mall.points_ledger_service import record_initial_points_grant
from app.models import (
    BusinessRecord, Member, MemberWechatBinding, PointsAccount, PointsGrant,
    UploadBatch, User,
)
from app.time_utils import utc8_now
from test_postgresql_migration import (
    POSTGRES_TEST_ALLOW_RESET_ENV_NAME, POSTGRES_TEST_DATABASE_URL_ENV_NAME,
    build_alembic_config, get_database_state, validate_postgresql_test_database_url,
)


APP_ID = "wx1234567890abcdef"
SIGNING_KEY = "test-only-32-byte-session-signing-key-2026"


class PostgreSQLMiniprogramPointsLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(POSTGRES_TEST_DATABASE_URL_ENV_NAME, "").strip()
        if not configured:
            raise unittest.SkipTest("未配置独立 PostgreSQL 测试数据库")
        if os.environ.get(POSTGRES_TEST_ALLOW_RESET_ENV_NAME) != "1":
            raise RuntimeError("必须显式设置 POSTGRES_TEST_ALLOW_RESET=1")
        cls.database_url = validate_postgresql_test_database_url(
            configured, resolve_database_url(),
        )

    def test_private_ledger_on_disposable_database(self):
        tables, revision = get_database_state(self.database_url)
        self.assertFalse(tables - {"alembic_version"})
        self.assertIsNone(revision)
        config = build_alembic_config(self.database_url)
        upgraded = False
        engine = None
        try:
            command.upgrade(config, "head")
            upgraded = True
            engine = create_database_engine(self.database_url)
            Session = sessionmaker(bind=engine, autoflush=False)
            with patch.dict(os.environ, {
                "WECHAT_MINIPROGRAM_APP_ID": APP_ID,
                "MINIPROGRAM_SESSION_SIGNING_KEY": SIGNING_KEY,
            }), Session() as db:
                member = Member(member_public_id="MEMBER-PG-LEDGER", is_active=True)
                other = Member(member_public_id="MEMBER-PG-OTHER", is_active=True)
                db.add_all([member, other])
                db.flush()
                binding = MemberWechatBinding(member_id=member.id,
                                              wechat_app_id=APP_ID,
                                              openid="pg-ledger-openid")
                other_binding = MemberWechatBinding(member_id=other.id,
                                                    wechat_app_id=APP_ID,
                                                    openid="pg-other-openid")
                db.add_all([binding, other_binding])
                partner = User(username="pg-ledger-partner", password_hash="test",
                               role="partner", is_active=True)
                db.add(partner)
                db.flush()
                now = utc8_now()
                batch = UploadBatch(user_id=partner.id, filename="pg-ledger.xlsx",
                                    total_rows=1, success_rows=1, failed_rows=0,
                                    acceptance_status="已承接",
                                    redemption_mode="MALL_REDEMPTION",
                                    claim_deadline=now + timedelta(days=40))
                db.add(batch)
                db.flush()
                business = BusinessRecord(
                    user_id=partner.id, batch_id=batch.id, business_no="BR-PG-LEDGER",
                    public_business_no="BR-PG-LEDGER", name="测试姓名",
                    phone="13800000000", plate_number="京A00001",
                    points_amount=Decimal("12.50"), bank_card="",
                    redemption_mode="MALL_REDEMPTION", claim_status="ACTIVATED",
                )
                db.add(business)
                db.flush()
                account = PointsAccount(member_id=member.id)
                db.add_all([account, PointsAccount(member_id=other.id)])
                db.flush()
                grant = PointsGrant(
                    account_id=account.id, business_record_id=business.id,
                    granted_points=Decimal("12.50"),
                    available_points=Decimal("0.00"),
                    reserved_points=Decimal("0.00"), activated_at=now,
                    expires_at=now + timedelta(days=20), status="ACTIVE",
                )
                db.add(grant)
                db.flush()
                record_initial_points_grant(
                    db, grant=grant, idempotency_key="pg-ledger-grant",
                    reference_type="BUSINESS_RECORD", reference_id="BR-PG-LEDGER",
                    now=now,
                )
                db.commit()
                token, _ = issue_session(binding.id, APP_ID, binding.openid)
                other_token, _ = issue_session(other_binding.id, APP_ID,
                                               other_binding.openid)
                response = Response()
                own = get_member_points_ledger(
                    response, "Bearer " + token, page=1, page_size=1, db=db,
                )
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(own.total, 1)
                self.assertEqual(own.items[0].available_points_delta, "12.50")
                self.assertNotIn("BR-PG-LEDGER", str(own.model_dump()))
                self.assertEqual(get_member_points_ledger(
                    Response(), "Bearer " + other_token,
                    page=1, page_size=20, db=db,
                ).total, 0)
                with self.assertRaises(HTTPException) as unauthenticated:
                    get_member_points_ledger(Response(), None,
                                             page=1, page_size=20, db=db)
                self.assertEqual(unauthenticated.exception.status_code, 401)
                account.available_points = Decimal("99.00")
                db.commit()
                with self.assertRaises(HTTPException) as drift:
                    get_member_points_ledger(Response(), "Bearer " + token,
                                             page=1, page_size=20, db=db)
                self.assertEqual(drift.exception.status_code, 503)
        finally:
            if engine is not None:
                engine.dispose()
            if upgraded:
                command.downgrade(config, "base")


if __name__ == "__main__":
    unittest.main()
