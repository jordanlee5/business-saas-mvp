import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import OPERATOR, SUPER_ADMIN
from app.database import Base
from app.mall import (
    ADJUSTMENT_PERMISSION_MESSAGE,
    POINTS_BALANCE_MISMATCH_MESSAGE,
    adjust_points_grant,
    audit_points_account_balance,
    execute_points_adjustment,
    record_initial_points_grant,
)
from app.models import (
    AdminActionLog,
    BusinessRecord,
    Member,
    PointsAccount,
    PointsGrant,
    PointsLedgerEntry,
    UploadBatch,
    User,
)


NOW = datetime(2026, 9, 7, 18, 0, 0)
ZERO = Decimal("0.00")


class PointsAdjustmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "adjustment.db"
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.super_admin = self.add_user(
            "adjust-super-admin",
            role="admin",
            admin_level=SUPER_ADMIN,
        )
        self.other_super_admin = self.add_user(
            "adjust-other-super-admin",
            role="admin",
            admin_level=SUPER_ADMIN,
        )
        self.operator = self.add_user(
            "adjust-operator",
            role="admin",
            admin_level=OPERATOR,
        )
        self.partner = self.add_user("adjust-partner", role="partner")
        self.batch = UploadBatch(
            user_id=self.partner.id,
            filename="points-adjustment.xlsx",
            total_rows=3,
            success_rows=3,
            failed_rows=0,
            acceptance_status="已承接",
            redemption_mode="MALL_REDEMPTION",
            claim_deadline=NOW + timedelta(days=30),
        )
        self.db.add(self.batch)
        self.db.flush()
        member = Member(member_public_id="adjustment-member")
        self.db.add(member)
        self.db.flush()
        self.account = PointsAccount(member_id=member.id)
        self.db.add(self.account)
        self.db.flush()
        self.grant = self.add_grant()
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_user(self, username, *, role, admin_level=None, is_active=True):
        user = User(
            username=username,
            password_hash="test-only",
            role=role,
            admin_level=admin_level,
            is_active=is_active,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def add_grant(self, *, points="125.55", expires_at=None):
        number = self.db.query(BusinessRecord).count() + 1
        business = BusinessRecord(
            user_id=self.partner.id,
            batch_id=self.batch.id,
            business_no=f"BR-ADJUST-{number}",
            public_business_no=f"BR-ADJUST-{number}",
            name="人工调整测试",
            phone=f"1380000{number:04d}",
            plate_number=f"桂A{number:05d}",
            points_amount=Decimal(points),
            bank_card="",
            redemption_mode="MALL_REDEMPTION",
            claim_status="ACTIVATED",
        )
        self.db.add(business)
        self.db.flush()
        grant = PointsGrant(
            account_id=self.account.id,
            business_record_id=business.id,
            granted_points=Decimal(points),
            available_points=ZERO,
            reserved_points=ZERO,
            activated_at=NOW - timedelta(days=30),
            expires_at=expires_at or NOW + timedelta(days=335),
            status="ACTIVE",
        )
        self.db.add(grant)
        self.db.flush()
        record_initial_points_grant(
            self.db,
            grant=grant,
            idempotency_key=f"fixture-adjustment-grant:{grant.id}",
            reference_type="BUSINESS_RECORD",
            reference_id=business.public_business_no,
            now=grant.activated_at,
        )
        return grant

    def add_history(self, grant, *, available, reserved="0", kind="CONSUME"):
        available = Decimal(available)
        reserved = Decimal(reserved)
        self.db.add(PointsLedgerEntry(
            grant_id=grant.id,
            entry_type=kind,
            available_points_delta=available,
            reserved_points_delta=reserved,
            idempotency_key=f"fixture-adjustment-flow:{grant.id}:{kind}",
        ))
        grant.available_points += available
        grant.reserved_points += reserved
        self.account.available_points += available
        self.account.reserved_points += reserved
        if grant.available_points == ZERO and grant.reserved_points == ZERO:
            grant.status = "EXHAUSTED"
        self.db.flush()

    def adjust(self, *, grant=None, delta="-10", actor=None, reason="录入纠错", key="REQ-1", now=NOW):
        return adjust_points_grant(
            self.db,
            grant_id=(grant or self.grant).id,
            delta_points=delta,
            actor_admin_id=(actor or self.super_admin).id,
            reason=reason,
            idempotency_key=key,
            now=now,
        )

    def fresh_audit(self):
        with self.Session() as db:
            return audit_points_account_balance(db, account_id=self.account.id)

    def test_negative_adjustment_appends_ledger_log_and_updates_caches(self):
        original = self.db.query(PointsLedgerEntry).one()
        original_snapshot = (
            original.entry_type,
            original.available_points_delta,
            original.idempotency_key,
            original.created_at,
        )
        result = self.adjust(delta="-25.10", reason="  重复  录入\n纠错  ")
        self.db.commit()
        self.assertTrue(result.created)
        self.assertEqual(result.delta_points, Decimal("-25.10"))
        self.assertEqual(result.grant_available_points, Decimal("100.45"))
        self.assertEqual(result.account_available_points, Decimal("100.45"))
        self.assertEqual(result.grant_status, "ACTIVE")
        entry = self.db.get(PointsLedgerEntry, result.ledger_entry_id)
        action_log = self.db.get(AdminActionLog, result.admin_action_log_id)
        self.assertEqual(entry.entry_type, "ADJUST")
        self.assertEqual(entry.reserved_points_delta, ZERO)
        self.assertEqual(entry.actor_admin_id, self.super_admin.id)
        self.assertEqual(entry.reason, "重复 录入 纠错")
        self.assertEqual(entry.reference_type, "ADMIN_ACTION_LOG")
        self.assertEqual(entry.reference_id, str(action_log.id))
        self.assertEqual(action_log.action_type, "mall_points_adjust")
        self.assertEqual(action_log.target_type, "points_grant")
        self.assertEqual(action_log.target_id, self.grant.id)
        self.assertIn("变化 -25.10", action_log.description)
        self.assertEqual(self.account.version, 2)
        self.db.refresh(original)
        self.assertEqual(
            (original.entry_type, original.available_points_delta,
             original.idempotency_key, original.created_at),
            original_snapshot,
        )
        self.assertTrue(self.fresh_audit().is_consistent)
        self.assertEqual(
            self.db.get(BusinessRecord, self.grant.business_record_id).claim_status,
            "ACTIVATED",
        )
        self.assertEqual(self.grant.granted_points, Decimal("125.55"))

    def test_positive_adjustment_restores_only_previously_reduced_points(self):
        self.add_history(self.grant, available="-50")
        self.db.commit()
        result = self.adjust(delta="25")
        self.db.commit()
        self.assertEqual(result.grant_available_points, Decimal("100.55"))
        self.assertEqual(self.grant.granted_points, Decimal("125.55"))
        self.assertEqual(self.grant.expires_at, NOW + timedelta(days=335))
        self.assertTrue(self.fresh_audit().is_consistent)

    def test_full_negative_adjustment_marks_empty_grant_exhausted(self):
        result = self.adjust(delta="-125.55")
        self.assertEqual(result.grant_available_points, ZERO)
        self.assertEqual(result.grant_status, "EXHAUSTED")
        self.assertEqual(self.account.available_points, ZERO)

    def test_positive_adjustment_reactivates_exhausted_grant(self):
        self.add_history(self.grant, available="-125.55")
        self.db.commit()
        self.assertEqual(self.grant.status, "EXHAUSTED")
        result = self.adjust(delta="25")
        self.assertEqual(result.grant_status, "ACTIVE")
        self.assertEqual(result.grant_available_points, Decimal("25.00"))

    def test_adjustment_never_changes_reserved_points(self):
        self.add_history(
            self.grant,
            available="-50",
            reserved="50",
            kind="RESERVE",
        )
        self.db.commit()
        result = self.adjust(delta="-20")
        self.assertEqual(result.grant_available_points, Decimal("55.55"))
        self.assertEqual(result.grant_reserved_points, Decimal("50.00"))
        self.assertEqual(result.account_reserved_points, Decimal("50.00"))

    def test_balance_boundaries_reject_overdraft_and_overgrant(self):
        for delta, message in (
            ("-125.56", "不能超过批次可用积分"),
            ("0.01", "不能超过批次原始授予积分上限"),
        ):
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, message):
                self.adjust(delta=delta, key=f"BOUND-{delta}")
        self.assertEqual(self.db.query(AdminActionLog).count(), 0)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)
        self.assertEqual(self.grant.available_points, Decimal("125.55"))

    def test_reserved_balance_counts_toward_positive_adjustment_cap(self):
        self.add_history(
            self.grant,
            available="-50",
            reserved="50",
            kind="RESERVE",
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "原始授予积分上限"):
            self.adjust(delta="0.01")

    def test_values_are_rounded_to_two_decimal_places(self):
        self.add_history(self.grant, available="-10")
        self.db.commit()
        result = self.adjust(delta="1.005")
        self.assertEqual(result.delta_points, Decimal("1.01"))
        self.assertEqual(result.grant_available_points, Decimal("116.56"))

    def test_invalid_delta_reason_key_and_identifiers_are_rejected(self):
        invalid_calls = (
            {"delta_points": "0"},
            {"delta_points": True},
            {"delta_points": "NaN"},
            {"reason": "   "},
            {"reason": "x" * 501},
            {"idempotency_key": ""},
            {"idempotency_key": "x" * 83},
            {"grant_id": 0},
            {"grant_id": True},
        )
        base = dict(
            grant_id=self.grant.id,
            delta_points="-1",
            actor_admin_id=self.super_admin.id,
            reason="纠错",
            idempotency_key="VALID",
            now=NOW,
        )
        for changes in invalid_calls:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                adjust_points_grant(self.db, **(base | changes))
        self.assertEqual(self.db.query(AdminActionLog).count(), 0)

    def test_only_current_active_super_admin_is_authorized(self):
        disabled = self.add_user(
            "adjust-disabled",
            role="admin",
            admin_level=SUPER_ADMIN,
            is_active=False,
        )
        self.db.commit()
        for actor_id in (
            self.operator.id,
            self.partner.id,
            disabled.id,
            999,
            0,
            True,
        ):
            with self.subTest(actor_id=actor_id), self.assertRaisesRegex(
                PermissionError,
                ADJUSTMENT_PERMISSION_MESSAGE,
            ):
                adjust_points_grant(
                    self.db,
                    grant_id=999,
                    delta_points="-1",
                    actor_admin_id=actor_id,
                    reason="无权限测试",
                    idempotency_key=f"AUTH-{actor_id}",
                    now=NOW,
                )
        self.assertEqual(self.db.query(AdminActionLog).count(), 0)

    def test_repeat_request_is_idempotent(self):
        first = self.adjust(delta="-10", key="REPLAY")
        self.db.commit()
        version = self.account.version
        replay = self.adjust(delta="-10", key="REPLAY")
        self.db.commit()
        self.assertFalse(replay.created)
        self.assertEqual(replay.ledger_entry_id, first.ledger_entry_id)
        self.assertEqual(replay.admin_action_log_id, first.admin_action_log_id)
        self.assertEqual(self.db.query(AdminActionLog).count(), 1)
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 2)
        self.assertEqual(self.account.version, version)

    def test_replay_after_later_adjustment_returns_current_balances(self):
        first = self.adjust(delta="-10", key="FIRST")
        self.db.commit()
        self.adjust(delta="-5", key="SECOND")
        self.db.commit()
        replay = self.adjust(delta="-10", key="FIRST")
        self.assertFalse(replay.created)
        self.assertEqual(replay.ledger_entry_id, first.ledger_entry_id)
        self.assertEqual(replay.grant_available_points, Decimal("110.55"))
        self.assertEqual(self.account.version, 3)

    def test_same_request_id_with_different_content_is_rejected(self):
        self.adjust(delta="-10", reason="原原因", key="CONFLICT")
        self.db.commit()
        other_grant = self.add_grant(points="20")
        self.db.commit()
        cases = (
            dict(delta="-9", reason="原原因", actor=self.super_admin, grant=self.grant),
            dict(delta="-10", reason="新原因", actor=self.super_admin, grant=self.grant),
            dict(delta="-10", reason="原原因", actor=self.other_super_admin, grant=self.grant),
            dict(delta="-10", reason="原原因", actor=self.super_admin, grant=other_grant),
        )
        for index, case in enumerate(cases):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "请求号冲突"):
                self.adjust(key="CONFLICT", **case)
        self.assertEqual(self.db.query(AdminActionLog).count(), 1)

    def test_collision_with_non_adjustment_ledger_is_rejected(self):
        initial = self.db.query(PointsLedgerEntry).one()
        initial.idempotency_key = "points-adjustment:COLLISION"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "请求号冲突"):
            self.adjust(key="COLLISION")

    def test_missing_or_modified_action_log_breaks_idempotent_replay(self):
        result = self.adjust(key="AUDIT-LINK")
        self.db.commit()
        action_log = self.db.get(AdminActionLog, result.admin_action_log_id)
        action_log.description = "被修改"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "审计记录不完整"):
            self.adjust(key="AUDIT-LINK")

    def test_expired_and_frozen_grants_are_rejected(self):
        at_boundary = self.add_grant(points="10", expires_at=NOW)
        frozen = self.add_grant(points="10")
        frozen.status = "FROZEN"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "已到期"):
            self.adjust(grant=at_boundary, delta="-1", key="EXPIRED")
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.adjust(grant=frozen, delta="-1", key="FROZEN")

    def test_timezone_aware_time_obeys_utc8_expiry_boundary(self):
        grant = self.add_grant(points="10", expires_at=NOW)
        self.db.commit()
        before_utc = datetime(2026, 9, 7, 9, 59, 59, 999999, tzinfo=timezone.utc)
        result = self.adjust(grant=grant, delta="-1", key="TZ", now=before_utc)
        self.assertTrue(result.created)
        exact_utc = datetime(2026, 9, 7, 10, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "已到期"):
            self.adjust(grant=self.grant, delta="-1", key="TZ-EDGE", now=exact_utc + timedelta(days=335))

    def test_cache_drift_in_target_or_sibling_blocks_without_repair(self):
        sibling = self.add_grant(points="20")
        self.db.commit()
        sibling.available_points = Decimal("19.00")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, POINTS_BALANCE_MISMATCH_MESSAGE):
            self.adjust()
        self.assertEqual(sibling.available_points, Decimal("19.00"))
        self.assertEqual(self.db.query(AdminActionLog).count(), 0)

    def test_success_is_rollback_safe(self):
        self.adjust(delta="-10", key="ROLLBACK")
        self.db.rollback()
        with self.Session() as db:
            self.assertEqual(db.query(AdminActionLog).count(), 0)
            self.assertEqual(db.query(PointsLedgerEntry).count(), 1)
            self.assertEqual(db.get(PointsGrant, self.grant.id).available_points, Decimal("125.55"))
            self.assertEqual(db.get(PointsAccount, self.account.id).version, 1)

    def _serialized_adjustment(self, *, delta, key, barrier):
        barrier.wait(timeout=10)
        return execute_points_adjustment(
            self.engine,
            grant_id=self.grant.id,
            delta_points=delta,
            actor_admin_id=self.super_admin.id,
            reason="并发纠错",
            idempotency_key=key,
            now=NOW,
        )

    def test_serialized_concurrent_adjustments_do_not_lose_updates(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self._serialized_adjustment,
                    delta=delta,
                    key=key,
                    barrier=barrier,
                )
                for delta, key in (("-10", "CONCURRENT-1"), ("-15", "CONCURRENT-2"))
            ]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(x.delta_points for x in results), Decimal("-25.00"))
        with self.Session() as db:
            self.assertEqual(db.get(PointsGrant, self.grant.id).available_points, Decimal("100.55"))
            self.assertEqual(db.get(PointsAccount, self.account.id).version, 3)
            self.assertEqual(db.query(AdminActionLog).count(), 2)
            self.assertEqual(db.query(PointsLedgerEntry).filter_by(entry_type="ADJUST").count(), 2)
        self.assertTrue(self.fresh_audit().is_consistent)

    def test_concurrent_duplicate_request_creates_one_adjustment(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self._serialized_adjustment,
                    delta="-10",
                    key="CONCURRENT-SAME",
                    barrier=barrier,
                )
                for _ in range(2)
            ]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(x.created for x in results), 1)
        with self.Session() as db:
            self.assertEqual(db.get(PointsGrant, self.grant.id).available_points, Decimal("115.55"))
            self.assertEqual(db.query(AdminActionLog).count(), 1)
            self.assertEqual(db.query(PointsLedgerEntry).filter_by(entry_type="ADJUST").count(), 1)

    def test_stale_session_uses_latest_committed_balance(self):
        self.assertEqual(self.account.available_points, Decimal("125.55"))
        barrier = Barrier(1)
        self._serialized_adjustment(delta="-10", key="OTHER-TRANSACTION", barrier=barrier)
        self.assertEqual(self.account.available_points, Decimal("125.55"))
        result = self.adjust(delta="-5", key="STALE-SESSION")
        self.db.commit()
        self.assertEqual(result.grant_available_points, Decimal("110.55"))
        self.assertEqual(self.account.available_points, Decimal("110.55"))
        self.assertTrue(self.fresh_audit().is_consistent)

    def test_transaction_entry_rolls_back_failure_after_writes(self):
        from app.mall.points_adjustment_service import (
            assert_points_account_balance_consistent as real_assert,
        )

        calls = []
        def fail_after_write(db, **kwargs):
            calls.append(kwargs["account_id"])
            if len(calls) == 2:
                raise ValueError("测试写后失败")
            return real_assert(db, **kwargs)

        with patch(
            "app.mall.points_adjustment_service.assert_points_account_balance_consistent",
            side_effect=fail_after_write,
        ):
            with self.assertRaisesRegex(ValueError, "测试写后失败"):
                execute_points_adjustment(
                    self.engine,
                    grant_id=self.grant.id,
                    delta_points="-10",
                    actor_admin_id=self.super_admin.id,
                    reason="回滚测试",
                    idempotency_key="WRITE-ROLLBACK",
                    now=NOW,
                )
        with self.Session() as db:
            self.assertEqual(db.query(AdminActionLog).count(), 0)
            self.assertEqual(db.query(PointsLedgerEntry).count(), 1)
            self.assertEqual(db.get(PointsGrant, self.grant.id).available_points, Decimal("125.55"))
            self.assertEqual(db.get(PointsAccount, self.account.id).version, 1)


if __name__ == "__main__":
    unittest.main()
