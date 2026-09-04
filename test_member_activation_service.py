import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.mall import (
    ACTIVATION_FAILURE_MESSAGE,
    ActivationCredentialStatus,
    ActivationSecurityMethod,
    BusinessChannel,
    BusinessClaimStatus,
    PointsLedgerEntryType,
    activate_mall_business,
    issue_activation_credential,
    issue_one_time_activation_code,
)
from app.models import (
    BusinessRecord,
    Member,
    MemberActivationCredential,
    MemberWechatBinding,
    PointsAccount,
    PointsGrant,
    PointsLedgerEntry,
    UploadBatch,
    User,
)


NOW = datetime(2026, 9, 3, 18, 0, 0)
DEADLINE = datetime(2026, 9, 30, 23, 59, 59)


class MemberActivationServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        uploader = User(
            username="mall_activation_partner",
            password_hash="test-only",
            role="partner",
        )
        self.db.add(uploader)
        self.db.flush()
        self.batch = UploadBatch(
            user_id=uploader.id,
            filename="mall-activation.xlsx",
            total_rows=1,
            success_rows=1,
            failed_rows=0,
            acceptance_status="已承接",
            redemption_mode=BusinessChannel.MALL_REDEMPTION.value,
            claim_deadline=DEADLINE,
            created_at=NOW,
        )
        self.db.add(self.batch)
        self.db.flush()
        self.business = self.add_business(
            "BR-MALL-ACTIVATION-1",
            "138 0000 0001",
            "桂A 10001",
            125.55,
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_business(
        self,
        public_no,
        phone,
        plate_number,
        points,
    ):
        record = BusinessRecord(
            user_id=self.batch.user_id,
            batch_id=self.batch.id,
            business_no=public_no,
            public_business_no=public_no,
            name="商城客户",
            phone=phone,
            plate_number=plate_number,
            points_amount=points,
            bank_card="",
            redemption_mode=BusinessChannel.MALL_REDEMPTION.value,
            claim_status=BusinessClaimStatus.PENDING_ACTIVATION.value,
            created_at=NOW,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def activate(self, issued, business=None, **overrides):
        record = business or self.business
        values = {
            "business_public_no": record.public_business_no,
            "phone": record.phone,
            "plate_number": record.plate_number,
            "activation_code": issued.activation_code,
            "wechat_app_id": "wx-test-app",
            "openid": "openid-member-1",
            "unionid": "unionid-member-1",
            "now": NOW,
        }
        values.update(overrides)
        return activate_mall_business(self.db, **values)

    def test_issue_persists_only_versioned_hash(self):
        issued = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW,
        )
        credential = self.db.query(MemberActivationCredential).one()
        normalized_code = issued.activation_code.replace("-", "")

        self.assertRegex(
            issued.activation_code,
            r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5}-"
            r"[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5}$",
        )
        self.assertNotEqual(credential.secret_digest, normalized_code)
        self.assertNotEqual(credential.secret_salt, normalized_code)
        self.assertNotIn(issued.activation_code, repr(issued))
        self.assertNotIn(
            normalized_code,
            " ".join(
                str(getattr(credential, column.name))
                for column in MemberActivationCredential.__table__.columns
            ),
        )
        self.assertEqual(issued.issue_version, 1)
        self.assertEqual(credential.status, "ACTIVE")
        self.assertEqual(credential.expires_at, DEADLINE)

    def test_reissue_invalidates_old_code_and_resets_lock(self):
        first = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            max_attempts=1,
            now=NOW,
        )
        failed = self.activate(
            first,
            activation_code="AAAAA-AAAAA",
        )
        self.assertFalse(failed.success)
        self.assertEqual(
            self.db.query(MemberActivationCredential).one().status,
            ActivationCredentialStatus.LOCKED.value,
        )

        second = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW + timedelta(minutes=1),
        )
        credential = self.db.query(MemberActivationCredential).one()

        self.assertEqual(second.issue_version, 2)
        self.assertEqual(credential.failed_attempts, 0)
        self.assertEqual(credential.status, "ACTIVE")
        self.assertIsNone(credential.locked_at)
        old_result = self.activate(first)
        self.assertFalse(old_result.success)
        self.assertEqual(old_result.message, ACTIVATION_FAILURE_MESSAGE)

    def test_sms_method_is_reserved_but_fails_closed(self):
        with self.assertRaisesRegex(
            NotImplementedError,
            "短信验证码通道尚未接入",
        ):
            issue_activation_credential(
                self.db,
                business_record=self.business,
                security_method=ActivationSecurityMethod.SMS_OTP,
                now=NOW,
            )
        self.assertEqual(
            self.db.query(MemberActivationCredential).count(),
            0,
        )

    def test_only_accepted_pending_mall_business_can_issue(self):
        cases = (
            ("batch", "acceptance_status", "待承接"),
            (
                "business",
                "claim_status",
                BusinessClaimStatus.FROZEN.value,
            ),
            (
                "business",
                "redemption_mode",
                BusinessChannel.CASH_REBATE.value,
            ),
        )
        for target_name, field, value in cases:
            with self.subTest(field=field):
                target = getattr(self, target_name)
                original = getattr(target, field)
                setattr(target, field, value)
                with self.db.no_autoflush:
                    with self.assertRaises(ValueError):
                        issue_one_time_activation_code(
                            self.db,
                            business_record=self.business,
                            now=NOW,
                        )
                setattr(target, field, original)
                self.db.rollback()
                self.batch = self.db.get(UploadBatch, self.batch.id)
                self.business = self.db.get(
                    BusinessRecord,
                    self.business.id,
                )

    def test_failed_attempts_commit_and_lock_with_generic_message(self):
        issued = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            max_attempts=3,
            now=NOW,
        )
        self.db.commit()

        for attempt in range(1, 4):
            result = self.activate(
                issued,
                phone="13900000000",
                activation_code="WRONG-WRONG",
                now=NOW + timedelta(seconds=attempt),
            )
            self.assertFalse(result.success)
            self.assertEqual(result.message, ACTIVATION_FAILURE_MESSAGE)
            self.db.commit()

        self.db.close()
        self.db = self.Session()
        credential = self.db.query(MemberActivationCredential).one()
        self.assertEqual(credential.failed_attempts, 3)
        self.assertEqual(
            credential.status,
            ActivationCredentialStatus.LOCKED.value,
        )
        self.assertIsNotNone(credential.locked_at)
        self.assertEqual(self.db.query(PointsGrant).count(), 0)

    def test_success_creates_binding_account_grant_and_grant_ledger(self):
        issued = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW,
        )
        result = self.activate(
            issued,
            phone="138-0000-0001",
            plate_number="桂a10001",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.granted_points, Decimal("125.55"))
        self.assertEqual(result.available_points, Decimal("125.55"))
        self.assertEqual(
            result.expires_at,
            datetime(2027, 9, 3, 18, 0, 0),
        )
        self.assertEqual(self.db.query(Member).count(), 1)
        self.assertEqual(self.db.query(MemberWechatBinding).count(), 1)
        account = self.db.query(PointsAccount).one()
        grant = self.db.query(PointsGrant).one()
        ledger = self.db.query(PointsLedgerEntry).one()
        credential = self.db.query(MemberActivationCredential).one()

        self.assertEqual(account.available_points, Decimal("125.55"))
        self.assertEqual(account.reserved_points, Decimal("0.00"))
        self.assertEqual(account.version, 1)
        self.assertEqual(grant.business_record_id, self.business.id)
        self.assertEqual(grant.available_points, Decimal("125.55"))
        self.assertEqual(
            ledger.entry_type,
            PointsLedgerEntryType.GRANT.value,
        )
        self.assertEqual(
            ledger.idempotency_key,
            f"mall-activation-grant:{self.business.id}",
        )
        self.assertEqual(
            ledger.available_points_delta,
            Decimal("125.55"),
        )
        self.assertEqual(
            self.business.claim_status,
            BusinessClaimStatus.ACTIVATED.value,
        )
        self.assertEqual(
            credential.status,
            ActivationCredentialStatus.USED.value,
        )
        self.assertEqual(credential.used_at, NOW)

    def test_same_wechat_member_can_receive_multiple_grants(self):
        first = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW,
        )
        self.assertTrue(self.activate(first).success)

        second_business = self.add_business(
            "BR-MALL-ACTIVATION-2",
            "13800000002",
            "桂A10002",
            74.45,
        )
        second = issue_one_time_activation_code(
            self.db,
            business_record=second_business,
            now=NOW,
        )
        result = self.activate(second, business=second_business)

        self.assertTrue(result.success)
        self.assertEqual(self.db.query(Member).count(), 1)
        self.assertEqual(self.db.query(MemberWechatBinding).count(), 1)
        self.assertEqual(self.db.query(PointsAccount).count(), 1)
        self.assertEqual(self.db.query(PointsGrant).count(), 2)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 2)
        account = self.db.query(PointsAccount).one()
        self.assertEqual(account.available_points, Decimal("200.00"))
        self.assertEqual(account.version, 2)

    def test_used_code_cannot_create_duplicate_grant(self):
        issued = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW,
        )
        self.assertTrue(self.activate(issued).success)
        duplicate = self.activate(issued)

        self.assertFalse(duplicate.success)
        self.assertEqual(duplicate.message, ACTIVATION_FAILURE_MESSAGE)
        self.assertEqual(self.db.query(Member).count(), 1)
        self.assertEqual(self.db.query(PointsGrant).count(), 1)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)

    def test_expired_claim_marks_business_and_credential_expired(self):
        issued = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW,
        )
        result = self.activate(
            issued,
            now=DEADLINE + timedelta(microseconds=1),
        )

        self.assertFalse(result.success)
        self.assertEqual(
            self.business.claim_status,
            BusinessClaimStatus.EXPIRED.value,
        )
        self.assertEqual(
            self.db.query(MemberActivationCredential).one().status,
            ActivationCredentialStatus.EXPIRED.value,
        )
        self.assertEqual(self.db.query(PointsGrant).count(), 0)

    def test_successful_activation_is_rollback_safe(self):
        issued = issue_one_time_activation_code(
            self.db,
            business_record=self.business,
            now=NOW,
        )
        self.db.commit()
        result = self.activate(issued)
        self.assertTrue(result.success)

        self.db.rollback()
        self.assertEqual(self.db.query(Member).count(), 0)
        self.assertEqual(self.db.query(PointsGrant).count(), 0)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 0)
        self.db.refresh(self.business)
        self.assertEqual(
            self.business.claim_status,
            BusinessClaimStatus.PENDING_ACTIVATION.value,
        )
        self.assertEqual(
            self.db.query(MemberActivationCredential).one().status,
            ActivationCredentialStatus.ACTIVE.value,
        )


if __name__ == "__main__":
    unittest.main()
