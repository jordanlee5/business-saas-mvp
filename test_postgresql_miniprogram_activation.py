"""Disposable PostgreSQL acceptance for the miniapp activation API."""

import os
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from alembic import command
from fastapi import HTTPException, Response
from sqlalchemy.orm import sessionmaker

from app.api.miniprogram_v1 import MemberActivationRequest, redeem_member_activation
from app.database import create_database_engine, resolve_database_url
from app.mall.activation_service import issue_one_time_activation_code
from app.mall.wechat_identity_service import WechatIdentity
from app.models import (
    BusinessRecord, Member, MemberActivationCredential, MemberWechatBinding,
    PointsAccount, PointsGrant, PointsLedgerEntry, UploadBatch, User,
)
from app.time_utils import UTC8_TIMEZONE, utc8_now
from test_postgresql_migration import (
    POSTGRES_TEST_ALLOW_RESET_ENV_NAME, POSTGRES_TEST_DATABASE_URL_ENV_NAME,
    build_alembic_config, get_database_state, validate_postgresql_test_database_url,
)


APP_ID = "wx1234567890abcdef"


class PostgreSQLMiniprogramActivationTests(unittest.TestCase):
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

    def test_activation_commits_once_and_cannot_be_replayed(self):
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
                "MINIPROGRAM_SESSION_SIGNING_KEY": "test-only-32-byte-session-signing-key-2026",
            }), Session() as db:
                partner = User(username="pg-activation-partner", password_hash="test",
                               role="partner", is_active=True)
                db.add(partner)
                db.flush()
                now = utc8_now()
                batch = UploadBatch(user_id=partner.id, filename="pg-activation.xlsx",
                                    total_rows=1, success_rows=1, failed_rows=0,
                                    acceptance_status="已承接",
                                    redemption_mode="MALL_REDEMPTION",
                                    claim_deadline=now.replace(tzinfo=UTC8_TIMEZONE) + timedelta(days=30))
                db.add(batch)
                db.flush()
                business = BusinessRecord(
                    user_id=partner.id, batch_id=batch.id,
                    business_no="BR-PG-ACTIVATION", public_business_no="BR-PG-ACTIVATION",
                    name="测试姓名", phone="13800000001", plate_number="京A00001",
                    points_amount=Decimal("12.50"), bank_card="",
                    redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION",
                )
                db.add(business)
                db.flush()
                issued = issue_one_time_activation_code(db, business_record=business, now=now)
                db.commit()
                db.refresh(batch)
                self.assertEqual(
                    batch.claim_deadline,
                    now.replace(tzinfo=UTC8_TIMEZONE) + timedelta(days=30),
                )
                credential = db.query(MemberActivationCredential).one()
                self.assertEqual(
                    credential.issued_at, now.replace(tzinfo=UTC8_TIMEZONE),
                )
                body = MemberActivationRequest(
                    wechat_code="offline-pg-code", business_public_no="BR-PG-ACTIVATION",
                    phone="13800000001", plate_number="京A00001",
                    activation_code=issued.activation_code,
                )
                with patch("app.api.miniprogram_v1.exchange_wechat_code",
                           return_value=WechatIdentity(APP_ID, "pg-activation-openid")):
                    response = Response()
                    result = redeem_member_activation(body, response, db)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(result.model_dump(), {"status": "activated"})
                    with self.assertRaises(HTTPException) as replay:
                        redeem_member_activation(body, Response(), db)
                    self.assertEqual(replay.exception.status_code, 400)
                self.assertEqual(db.query(Member).count(), 1)
                self.assertEqual(db.query(MemberWechatBinding).count(), 1)
                self.assertEqual(db.query(PointsAccount).one().available_points,
                                 Decimal("12.50"))
                self.assertEqual(db.query(PointsGrant).count(), 1)
                self.assertEqual(db.query(PointsLedgerEntry).count(), 1)
                self.assertEqual(db.query(MemberActivationCredential).one().status, "USED")
        finally:
            if engine is not None:
                engine.dispose()
            if upgraded:
                command.downgrade(config, "base")


if __name__ == "__main__":
    unittest.main()
