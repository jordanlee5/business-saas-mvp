import unittest
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.mall import (
    BusinessChannel,
    BusinessClaimStatus,
    POINTS_BALANCE_MISMATCH_MESSAGE,
    PointsGrantStatus,
    activate_mall_business,
    assert_points_account_balance_consistent,
    audit_points_account_balance,
    calculate_points_account_balance,
    calculate_points_grant_balance,
    issue_one_time_activation_code,
    record_initial_points_grant,
)
from app.models import (
    BusinessRecord,
    Member,
    PointsAccount,
    PointsGrant,
    PointsLedgerEntry,
    UploadBatch,
    User,
)


NOW = datetime(2026, 9, 7, 10, 0, 0)
DEADLINE = datetime(2026, 9, 30, 23, 59, 59)


class PointsLedgerServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        uploader = User(
            username="points_ledger_partner",
            password_hash="test-only",
            role="partner",
        )
        self.db.add(uploader)
        self.db.flush()
        self.batch = UploadBatch(
            user_id=uploader.id,
            filename="points-ledger.xlsx",
            total_rows=2,
            success_rows=2,
            failed_rows=0,
            acceptance_status="已承接",
            redemption_mode=BusinessChannel.MALL_REDEMPTION.value,
            claim_deadline=DEADLINE,
            created_at=NOW,
        )
        self.db.add(self.batch)
        self.db.flush()
        self.business = self.add_business(
            public_no="BR-MALL-LEDGER-1",
            points=Decimal("125.55"),
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_business(self, *, public_no, points):
        record = BusinessRecord(
            user_id=self.batch.user_id,
            batch_id=self.batch.id,
            business_no=public_no,
            public_business_no=public_no,
            name="积分账本客户",
            phone="13800000001",
            plate_number="桂A10001",
            points_amount=points,
            bank_card="",
            redemption_mode=BusinessChannel.MALL_REDEMPTION.value,
            claim_status=BusinessClaimStatus.PENDING_ACTIVATION.value,
            created_at=NOW,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def activate(self, business=None):
        record = business or self.business
        issued = issue_one_time_activation_code(
            self.db,
            business_record=record,
            now=NOW,
        )
        return activate_mall_business(
            self.db,
            business_public_no=record.public_business_no,
            phone=record.phone,
            plate_number=record.plate_number,
            activation_code=issued.activation_code,
            wechat_app_id="wx-ledger-test",
            openid="openid-ledger-member",
            unionid="unionid-ledger-member",
            now=NOW,
        )

    def test_activation_balances_recalculate_exactly(self):
        result = self.activate()
        self.assertTrue(result.success)
        account = self.db.query(PointsAccount).one()
        grant = self.db.query(PointsGrant).one()

        grant_balance = calculate_points_grant_balance(
            self.db,
            grant_id=grant.id,
        )
        account_balance = calculate_points_account_balance(
            self.db,
            account_id=account.id,
        )
        audit = audit_points_account_balance(
            self.db,
            account_id=account.id,
        )

        self.assertEqual(
            grant_balance.available_points,
            Decimal("125.55"),
        )
        self.assertEqual(grant_balance.reserved_points, Decimal("0.00"))
        self.assertEqual(grant_balance.entry_count, 1)
        self.assertEqual(account_balance, grant_balance)
        self.assertTrue(audit.is_consistent)
        self.assertEqual(audit.inconsistent_grant_ids, ())

    def test_multiple_grants_recalculate_member_total(self):
        self.assertTrue(self.activate().success)
        second_business = self.add_business(
            public_no="BR-MALL-LEDGER-2",
            points=Decimal("74.45"),
        )
        second_business.phone = "13800000002"
        second_business.plate_number = "桂A10002"
        self.assertTrue(self.activate(second_business).success)

        account = self.db.query(PointsAccount).one()
        audit = assert_points_account_balance_consistent(
            self.db,
            account_id=account.id,
        )

        self.assertEqual(
            audit.ledger_balance.available_points,
            Decimal("200.00"),
        )
        self.assertEqual(audit.ledger_balance.entry_count, 2)
        self.assertEqual(len(audit.grant_audits), 2)
        self.assertEqual(account.available_points, Decimal("200.00"))
        self.assertEqual(account.version, 2)

    def test_initial_grant_write_is_idempotent(self):
        self.assertTrue(self.activate().success)
        account = self.db.query(PointsAccount).one()
        grant = self.db.query(PointsGrant).one()
        version_before = account.version

        replay = record_initial_points_grant(
            self.db,
            grant=grant,
            idempotency_key=f"mall-activation-grant:{self.business.id}",
            reference_type="BUSINESS_RECORD",
            reference_id=self.business.public_business_no,
            now=NOW,
        )

        self.assertFalse(replay.created)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)
        self.assertEqual(account.available_points, Decimal("125.55"))
        self.assertEqual(account.version, version_before)
        self.assertEqual(
            replay.account_balance.available_points,
            Decimal("125.55"),
        )

    def test_idempotency_key_conflict_fails_closed(self):
        self.assertTrue(self.activate().success)
        grant = self.db.query(PointsGrant).one()

        with self.assertRaisesRegex(
            ValueError,
            "积分流水幂等键冲突",
        ):
            record_initial_points_grant(
                self.db,
                grant=grant,
                idempotency_key=(
                    f"mall-activation-grant:{self.business.id}"
                ),
                reference_type="BUSINESS_RECORD",
                reference_id="BR-DIFFERENT-REFERENCE",
                now=NOW,
            )

        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)

    def test_audit_detects_account_and_grant_cache_drift(self):
        self.assertTrue(self.activate().success)
        account = self.db.query(PointsAccount).one()
        grant = self.db.query(PointsGrant).one()
        account.available_points = Decimal("120.00")
        grant.available_points = Decimal("121.00")

        audit = audit_points_account_balance(
            self.db,
            account_id=account.id,
        )

        self.assertFalse(audit.is_consistent)
        self.assertEqual(audit.inconsistent_grant_ids, (grant.id,))
        self.assertEqual(
            audit.ledger_balance.available_points,
            Decimal("125.55"),
        )
        with self.assertRaisesRegex(
            ValueError,
            POINTS_BALANCE_MISMATCH_MESSAGE,
        ):
            assert_points_account_balance_consistent(
                self.db,
                account_id=account.id,
            )

    def test_idempotent_replay_rejects_existing_cache_drift(self):
        self.assertTrue(self.activate().success)
        account = self.db.query(PointsAccount).one()
        grant = self.db.query(PointsGrant).one()
        account.available_points = Decimal("124.55")

        with self.assertRaisesRegex(
            ValueError,
            POINTS_BALANCE_MISMATCH_MESSAGE,
        ):
            record_initial_points_grant(
                self.db,
                grant=grant,
                idempotency_key=(
                    f"mall-activation-grant:{self.business.id}"
                ),
                reference_type="BUSINESS_RECORD",
                reference_id=self.business.public_business_no,
                now=NOW,
            )

        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)

    def test_missing_account_or_grant_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "积分批次不存在"):
            calculate_points_grant_balance(self.db, grant_id=999)
        with self.assertRaisesRegex(ValueError, "积分账户不存在"):
            calculate_points_account_balance(self.db, account_id=999)
        with self.assertRaisesRegex(ValueError, "积分账户不存在"):
            audit_points_account_balance(self.db, account_id=999)

        self.assertEqual(self.db.query(Member).count(), 0)


if __name__ == "__main__":
    unittest.main()
