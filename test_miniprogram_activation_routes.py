"""Offline activation API tests. WeChat exchange is always stubbed."""

import os
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.miniprogram_v1 import (
    MemberActivationRequest, redeem_member_activation,
)
from app.database import Base
from app.mall.activation_service import (
    ACTIVATION_FAILURE_MESSAGE, issue_one_time_activation_code,
)
from app.mall.wechat_identity_service import WechatCodeError, WechatIdentity
from app.models import (
    BusinessRecord, Member, MemberActivationCredential, MemberWechatBinding,
    PointsAccount, PointsGrant, PointsLedgerEntry, UploadBatch, User,
)
from app.time_utils import utc8_now


APP_ID = "wx1234567890abcdef"


class MiniprogramActivationRouteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WECHAT_MINIPROGRAM_APP_ID": APP_ID,
            "MINIPROGRAM_SESSION_SIGNING_KEY": "test-only-32-byte-session-signing-key-2026",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "activation.db"))
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        partner = User(username="activation-route-partner", password_hash="test",
                       role="partner", is_active=True)
        self.db.add(partner)
        self.db.flush()
        now = utc8_now()
        batch = UploadBatch(user_id=partner.id, filename="activation-route.xlsx",
                            total_rows=1, success_rows=1, failed_rows=0,
                            acceptance_status="已承接", redemption_mode="MALL_REDEMPTION",
                            claim_deadline=now + timedelta(days=30))
        self.db.add(batch)
        self.db.flush()
        self.business = BusinessRecord(
            user_id=partner.id, batch_id=batch.id, business_no="BR-M7-22-1",
            public_business_no="BR-M7-22-1", name="仅供测试",
            phone="13800000001", plate_number="京A00001",
            points_amount=Decimal("12.50"), bank_card="",
            redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION",
        )
        self.db.add(self.business)
        self.db.flush()
        self.issued = issue_one_time_activation_code(
            self.db, business_record=self.business, now=now,
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def body(self, **changes):
        values = {
            "wechat_code": "one-use-wechat-code",
            "business_public_no": self.business.public_business_no,
            "phone": self.business.phone,
            "plate_number": self.business.plate_number,
            "activation_code": self.issued.activation_code,
        }
        values.update(changes)
        return MemberActivationRequest(**values)

    def redeem(self, body=None, openid="m7-22-openid"):
        response = Response()
        with patch("app.api.miniprogram_v1.exchange_wechat_code",
                   return_value=WechatIdentity(APP_ID, openid)) as exchange:
            result = redeem_member_activation(body or self.body(), response, self.db)
        exchange.assert_called_once_with("one-use-wechat-code")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return result

    def test_success_commits_one_grant_and_binding_without_token_or_private_data(self):
        result = self.redeem()
        self.assertEqual(result.model_dump(), {"status": "activated"})
        self.assertEqual(self.db.query(Member).count(), 1)
        self.assertEqual(self.db.query(MemberWechatBinding).count(), 1)
        self.assertEqual(self.db.query(PointsAccount).one().available_points,
                         Decimal("12.50"))
        self.assertEqual(self.db.query(PointsGrant).count(), 1)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)
        self.assertEqual(self.business.claim_status, "ACTIVATED")
        self.assertEqual(self.db.query(MemberActivationCredential).one().status, "USED")

    def test_wrong_factor_commits_attempts_but_never_creates_member(self):
        for index in range(5):
            with self.subTest(attempt=index), self.assertRaises(HTTPException) as failed:
                self.redeem(self.body(activation_code="WRONG-WRONG"))
            self.assertEqual(failed.exception.status_code, 400)
            self.assertEqual(failed.exception.detail, ACTIVATION_FAILURE_MESSAGE)
        credential = self.db.query(MemberActivationCredential).one()
        self.assertEqual(credential.failed_attempts, 5)
        self.assertEqual(credential.status, "LOCKED")
        self.assertEqual(self.db.query(Member).count(), 0)

    def test_missing_business_and_reuse_have_same_public_failure(self):
        with self.assertRaises(HTTPException) as unknown:
            self.redeem(self.body(business_public_no="BR-NOT-FOUND"))
        self.assertEqual(unknown.exception.detail, ACTIVATION_FAILURE_MESSAGE)
        self.redeem()
        with self.assertRaises(HTTPException) as reused:
            self.redeem()
        self.assertEqual(reused.exception.detail, ACTIVATION_FAILURE_MESSAGE)
        self.assertEqual(self.db.query(PointsGrant).count(), 1)

    def test_bad_wechat_code_does_not_touch_activation_attempts(self):
        with patch("app.api.miniprogram_v1.exchange_wechat_code",
                   side_effect=WechatCodeError()):
            with self.assertRaises(HTTPException) as failed:
                redeem_member_activation(self.body(), Response(), self.db)
        self.assertEqual(failed.exception.status_code, 401)
        self.assertEqual(self.db.query(MemberActivationCredential).one().failed_attempts, 0)

    def test_missing_session_configuration_fails_before_wechat_exchange(self):
        with patch.dict(os.environ, {"MINIPROGRAM_SESSION_SIGNING_KEY": ""}), \
             patch("app.api.miniprogram_v1.exchange_wechat_code") as exchange:
            with self.assertRaises(HTTPException) as failed:
                redeem_member_activation(self.body(), Response(), self.db)
        self.assertEqual(failed.exception.status_code, 503)
        exchange.assert_not_called()

    def test_failed_database_operation_rolls_back(self):
        def mutate_then_fail(db, **kwargs):
            self.business.claim_status = "ACTIVATED"
            db.flush()
            raise ValueError("private-database-detail")

        with patch("app.api.miniprogram_v1.exchange_wechat_code",
                   return_value=WechatIdentity(APP_ID, "openid")), \
             patch("app.api.miniprogram_v1.activate_mall_business",
                   side_effect=mutate_then_fail):
            with self.assertRaises(HTTPException) as failed:
                redeem_member_activation(self.body(), Response(), self.db)
        self.assertEqual(failed.exception.status_code, 503)
        self.assertNotIn("private-database-detail", failed.exception.detail)
        self.db.expire_all()
        self.assertEqual(self.business.claim_status, "PENDING_ACTIVATION")

    def test_body_is_bounded(self):
        with self.assertRaises(ValidationError):
            self.body(activation_code="A" * 33)
        with self.assertRaises(ValidationError):
            self.body(wechat_code="")


if __name__ == "__main__":
    unittest.main()
