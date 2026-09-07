import hashlib
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.mall import (
    audit_points_account_balance,
    calculate_points_expiry,
    expire_points_grant,
    list_due_points_grants,
    list_expiring_points_grants,
    record_initial_points_grant,
)
from app.models import (
    BusinessRecord, Member, PointsAccount, PointsGrant, PointsLedgerEntry,
    UploadBatch, User,
)
from app.points_expiry_task import main, run_points_expiry_task
from app.schema_readiness import CURRENT_SCHEMA_REVISION, DatabaseSchemaNotReadyError


NOW = datetime(2026, 9, 7, 12, 0, 0)
ZERO = Decimal("0.00")


class PointsExpiryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "expiry_test.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
            connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"),
                               {"revision": CURRENT_SCHEMA_REVISION})
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.db = self.Session()
        user = User(username="expiry-fixture", password_hash="test-only", role="partner")
        self.db.add(user)
        self.db.flush()
        self.batch = UploadBatch(
            user_id=user.id, filename="expiry-fixture.xlsx", total_rows=1,
            success_rows=1, failed_rows=0, acceptance_status="已承接",
            redemption_mode="MALL_REDEMPTION", claim_deadline=NOW,
        )
        self.db.add(self.batch)
        self.db.flush()
        self.account = self.add_account()
        self.grant = self.add_grant()
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_account(self):
        number = self.db.query(Member).count() + 1
        member = Member(member_public_id=f"expiry-member-{number}")
        self.db.add(member)
        self.db.flush()
        account = PointsAccount(member_id=member.id)
        self.db.add(account)
        self.db.flush()
        return account

    def add_grant(self, *, points="125.55", expires_at=NOW, account=None):
        account = account or self.account
        number = self.db.query(BusinessRecord).count() + 1
        business = BusinessRecord(
            user_id=self.batch.user_id, batch_id=self.batch.id,
            business_no=f"BR-EXPIRY-{number}", public_business_no=f"BR-EXPIRY-{number}",
            name="到期测试", phone="13800000001", plate_number="桂A10001",
            points_amount=Decimal(points), bank_card="",
            redemption_mode="MALL_REDEMPTION", claim_status="ACTIVATED",
        )
        self.db.add(business)
        self.db.flush()
        grant = PointsGrant(
            account_id=account.id, business_record_id=business.id,
            granted_points=Decimal(points), available_points=ZERO, reserved_points=ZERO,
            activated_at=expires_at - timedelta(days=365), expires_at=expires_at,
            status="ACTIVE",
        )
        self.db.add(grant)
        self.db.flush()
        record_initial_points_grant(
            self.db, grant=grant, idempotency_key=f"fixture-grant:{grant.id}",
            reference_type="BUSINESS_RECORD", reference_id=business.public_business_no,
            now=grant.activated_at,
        )
        return grant

    def add_test_flow(self, grant, *, available, reserved="0", kind="CONSUME"):
        """仅用于构造未来消费/预占留下的已对平历史数据，不开放业务写入口。"""
        account = self.db.get(PointsAccount, grant.account_id)
        available, reserved = Decimal(available), Decimal(reserved)
        self.db.add(PointsLedgerEntry(
            grant_id=grant.id, entry_type=kind,
            available_points_delta=available, reserved_points_delta=reserved,
            idempotency_key=f"fixture-flow:{grant.id}:{kind}",
        ))
        grant.available_points += available
        grant.reserved_points += reserved
        account.available_points += available
        account.reserved_points += reserved
        self.db.flush()

    def fresh_audit(self):
        with self.Session() as db:
            return audit_points_account_balance(db, account_id=self.account.id)

    def test_exact_expiry_boundary_and_original_history_preserved(self):
        original = self.db.query(PointsLedgerEntry).one()
        original_fields = (original.entry_type, original.available_points_delta, original.created_at)
        with self.assertRaisesRegex(ValueError, "尚未到期"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW - timedelta(microseconds=1))
        result = expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.db.commit()
        self.assertTrue(result.changed)
        self.assertEqual(result.expired_points, Decimal("125.55"))
        self.assertEqual(self.grant.status, "EXPIRED")
        self.assertEqual(self.account.available_points, ZERO)
        self.assertEqual(self.account.version, 2)
        self.db.refresh(original)
        self.assertEqual((original.entry_type, original.available_points_delta, original.created_at), original_fields)
        expiry = self.db.get(PointsLedgerEntry, result.ledger_entry_id)
        self.assertEqual(expiry.available_points_delta, Decimal("-125.55"))
        self.assertEqual(expiry.reserved_points_delta, ZERO)
        self.assertEqual((expiry.reference_type, expiry.reference_id), ("POINTS_GRANT", str(self.grant.id)))
        self.assertTrue(self.fresh_audit().is_consistent)
        self.assertEqual(self.db.get(BusinessRecord, self.grant.business_record_id).claim_status, "ACTIVATED")
        self.assertEqual(self.batch.acceptance_status, "已承接")

    def test_replay_in_new_session_does_not_deduct_or_increment_version(self):
        expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.db.commit()
        with self.Session() as db:
            replay = expire_points_grant(db, grant_id=self.grant.id, now=NOW + timedelta(days=1))
            db.commit()
            self.assertFalse(replay.changed)
            self.assertEqual(replay.expired_points, ZERO)
            self.assertEqual(db.query(PointsLedgerEntry).count(), 2)
            self.assertEqual(db.get(PointsAccount, self.account.id).version, 2)

    def test_utc_instant_matches_utc8_boundary(self):
        now_utc = datetime(2026, 9, 7, 4, tzinfo=timezone.utc)
        page = list_due_points_grants(self.db, now=now_utc)
        self.assertEqual(page.as_of, NOW)
        self.assertEqual([item.grant_id for item in page.items], [self.grant.id])
        self.assertTrue(expire_points_grant(self.db, grant_id=self.grant.id, now=now_utc).changed)

    def test_leap_day_expiry_uses_anniversary_not_365_day_guess(self):
        activated = datetime(2024, 2, 29, 15, 12, 30)
        expiry = calculate_points_expiry(activated)
        grant = self.add_grant(expires_at=expiry)
        grant.activated_at = activated
        self.db.commit()
        self.assertEqual(expiry, datetime(2025, 2, 28, 15, 12, 30))
        with self.assertRaisesRegex(ValueError, "尚未到期"):
            expire_points_grant(self.db, grant_id=grant.id, now=expiry - timedelta(microseconds=1))
        self.assertTrue(expire_points_grant(self.db, grant_id=grant.id, now=expiry).changed)

    def test_only_remaining_points_expire_and_other_grants_survive(self):
        self.add_test_flow(self.grant, available="-88.43")
        future = self.add_grant(points="74.45", expires_at=NOW + timedelta(days=1))
        self.db.commit()
        result = expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.db.commit()
        self.assertEqual(result.expired_points, Decimal("37.12"))
        self.assertEqual(self.account.available_points, Decimal("74.45"))
        self.assertEqual(future.status, "ACTIVE")
        self.assertTrue(self.fresh_audit().is_consistent)

    def test_exhausted_zero_balance_closes_without_zero_ledger(self):
        self.add_test_flow(self.grant, available="-125.55")
        self.grant.status = "EXHAUSTED"
        self.db.commit()
        result = expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertTrue(result.changed)
        self.assertIsNone(result.ledger_entry_id)
        self.assertEqual(self.grant.status, "EXPIRED")
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 2)
        self.assertFalse(expire_points_grant(self.db, grant_id=self.grant.id, now=NOW).changed)

    def test_frozen_grant_is_reported_and_not_automatically_unfrozen(self):
        self.grant.status = "FROZEN"
        self.db.commit()
        self.assertIn("冻结", list_due_points_grants(self.db, now=NOW).items[0].block_reason)
        with self.assertRaisesRegex(ValueError, "冻结"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertEqual(self.grant.status, "FROZEN")

    def test_reserved_points_block_expiry_even_when_available_is_zero(self):
        self.add_test_flow(self.grant, available="-125.55", reserved="125.55", kind="RESERVE")
        self.db.commit()
        self.assertIn("预占", list_due_points_grants(self.db, now=NOW).items[0].block_reason)
        with self.assertRaisesRegex(ValueError, "预占"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertEqual(self.grant.reserved_points, Decimal("125.55"))

    def test_account_cache_drift_blocks_without_repair(self):
        self.account.available_points = Decimal("120.00")
        self.db.commit()
        self.assertIn("不一致", list_due_points_grants(self.db, now=NOW).items[0].block_reason)
        with self.assertRaisesRegex(ValueError, "不一致"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertEqual(self.account.available_points, Decimal("120.00"))
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 1)

    def test_sibling_grant_cache_drift_also_blocks(self):
        other = self.add_grant(expires_at=NOW + timedelta(days=60))
        other.available_points = Decimal("100.00")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "不一致"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertEqual(self.grant.status, "ACTIVE")

    def test_idempotency_key_collision_is_rejected(self):
        original = self.db.query(PointsLedgerEntry).one()
        original.idempotency_key = f"points-expiry:{self.grant.id}"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "冲突"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertEqual(self.account.available_points, Decimal("125.55"))

    def test_expired_status_with_positive_balance_is_rejected(self):
        self.grant.status = "EXPIRED"
        self.db.commit()
        self.assertIsNotNone(list_due_points_grants(self.db, now=NOW).items[0].block_reason)
        with self.assertRaisesRegex(ValueError, "仍有余额"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)

    def test_unfunded_grant_cannot_be_closed_as_zero_balance(self):
        self.db.query(PointsLedgerEntry).delete()
        self.grant.available_points = ZERO
        self.account.available_points = ZERO
        self.db.commit()
        self.assertIn("首笔入账", list_due_points_grants(self.db, now=NOW).items[0].block_reason)
        with self.assertRaisesRegex(ValueError, "首笔入账"):
            expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)

    def test_upcoming_window_account_filter_and_cursor(self):
        one = self.add_grant(expires_at=NOW + timedelta(microseconds=1))
        edge = self.add_grant(expires_at=NOW + timedelta(days=30))
        self.add_grant(expires_at=NOW + timedelta(days=30, microseconds=1))
        another_account = self.add_account()
        self.add_grant(expires_at=NOW + timedelta(days=5), account=another_account)
        self.db.commit()
        page = list_expiring_points_grants(self.db, now=NOW, account_id=self.account.id, limit=1)
        self.assertEqual([item.grant_id for item in page.items], [one.id])
        self.assertTrue(page.has_more)
        second = list_expiring_points_grants(
            self.db, now=NOW, account_id=self.account.id,
            after_grant_id=page.next_after_grant_id, limit=1,
        )
        self.assertEqual([item.grant_id for item in second.items], [edge.id])
        self.assertFalse(second.has_more)

    def test_due_pagination_has_no_duplicate_and_excludes_processed(self):
        second = self.add_grant()
        self.db.commit()
        first_page = list_due_points_grants(self.db, now=NOW, limit=1)
        next_page = list_due_points_grants(
            self.db, now=NOW, limit=1, after_grant_id=first_page.next_after_grant_id,
        )
        self.assertTrue(first_page.has_more)
        self.assertEqual(next_page.items[0].grant_id, second.id)
        self.assertFalse(next_page.has_more)
        expire_points_grant(self.db, grant_id=self.grant.id, now=NOW)
        self.assertEqual([x.grant_id for x in list_due_points_grants(self.db, now=NOW).items], [second.id])

    def test_query_never_autoflushes_or_overwrites_pending_changes(self):
        self.db.autoflush = True
        self.account.available_points = Decimal("120.00")
        pending = Member(member_public_id="never-flushed")
        self.db.add(pending)
        statements = []
        def capture(conn, cursor, statement, params, context, many):
            statements.append(statement.lstrip().split()[0].upper())
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            page = list_due_points_grants(self.db, now=NOW)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(set(statements), {"SELECT"})
        self.assertIsNone(pending.id)
        self.assertEqual(self.account.available_points, Decimal("120.00"))
        self.assertEqual(page.items[0].available_points, Decimal("125.55"))

    def test_invalid_query_parameters_and_missing_objects(self):
        for options in ({"limit": 0}, {"limit": 1001}, {"after_grant_id": -1}, {"limit": True}, {"now": "bad"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                list_due_points_grants(self.db, **options)
        for days in (0, 367, True, 1.5):
            with self.subTest(days=days), self.assertRaises(ValueError):
                list_expiring_points_grants(self.db, days=days)
        with self.assertRaisesRegex(ValueError, "账户不存在"):
            list_due_points_grants(self.db, account_id=999)
        with self.assertRaisesRegex(ValueError, "批次不存在"):
            expire_points_grant(self.db, grant_id=999)

    def test_default_task_preview_leaves_database_bytes_unchanged(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        report = run_points_expiry_task(self.engine, now=NOW)
        self.assertEqual(report["page_count"], 1)
        self.assertEqual(report["changed_count"], 0)
        self.assertFalse(report["committed"])
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_apply_commits_page_and_second_run_has_no_work(self):
        self.add_grant(points="74.45")
        self.db.commit()
        report = run_points_expiry_task(self.engine, apply=True, now=NOW)
        self.assertTrue(report["committed"])
        self.assertEqual(report["changed_count"], 2)
        self.assertEqual(report["expired_points"], Decimal("200.00"))
        self.assertTrue(self.fresh_audit().is_consistent)
        again = run_points_expiry_task(self.engine, apply=True, now=NOW)
        self.assertEqual(again["page_count"], 0)
        self.assertEqual(again["expired_points"], ZERO)

    def test_one_blocked_item_prevents_entire_page_writes(self):
        second = self.add_grant(account=self.add_account())
        second.status = "FROZEN"
        self.db.commit()
        report = run_points_expiry_task(self.engine, apply=True, now=NOW)
        self.assertEqual(report["blocked_count"], 1)
        self.assertFalse(report["committed"])
        self.assertEqual(report["changed_count"], 0)
        self.assertEqual(self.fresh_audit().ledger_balance.available_points, Decimal("125.55"))

    def test_failure_after_first_grant_rolls_back_whole_page(self):
        self.add_grant(account=self.add_account())
        self.db.commit()
        calls = []
        def fail_second(db, **kwargs):
            calls.append(kwargs["grant_id"])
            if len(calls) == 2:
                raise ValueError("测试第二批失败")
            return expire_points_grant(db, **kwargs)
        with patch("app.points_expiry_task.expire_points_grant", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "第二批失败"):
                run_points_expiry_task(self.engine, apply=True, now=NOW)
        with self.Session() as db:
            self.assertEqual(db.query(PointsLedgerEntry).count(), 2)
            self.assertEqual(db.query(PointsGrant).filter_by(status="ACTIVE").count(), 2)
        self.assertEqual(self.fresh_audit().ledger_balance.available_points, Decimal("125.55"))

    def test_two_sqlite_tasks_do_not_double_expire(self):
        self.add_grant(points="74.45")
        self.db.commit()
        barrier = Barrier(2)
        def worker():
            barrier.wait(timeout=10)
            return run_points_expiry_task(self.engine, apply=True, now=NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            reports = [future.result(timeout=15) for future in futures]
        self.assertEqual(sum(x["changed_count"] for x in reports), 2)
        self.assertEqual(sum(x["expired_points"] for x in reports), Decimal("200.00"))
        with self.Session() as db:
            self.assertEqual(db.query(PointsLedgerEntry).filter_by(entry_type="EXPIRE").count(), 2)
        self.assertTrue(self.fresh_audit().is_consistent)

    def test_stale_session_audit_and_new_grant_use_latest_balance(self):
        self.assertEqual(self.account.available_points, Decimal("125.55"))
        run_points_expiry_task(self.engine, apply=True, now=NOW)
        self.assertEqual(self.account.available_points, Decimal("125.55"))  # 旧 ORM 对象
        audit = audit_points_account_balance(self.db, account_id=self.account.id)
        self.assertTrue(audit.is_consistent)
        self.assertEqual(audit.cached_available_points, ZERO)
        self.add_grant(points="74.45", expires_at=NOW + timedelta(days=365))
        self.db.commit()
        self.assertEqual(self.account.available_points, Decimal("74.45"))
        self.assertTrue(self.fresh_audit().is_consistent)

    def test_schema_not_ready_refuses_task_without_migration(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '0002_mall_core_foundation'"))
        with self.assertRaises(DatabaseSchemaNotReadyError):
            run_points_expiry_task(self.engine, apply=True, now=NOW)
        self.assertEqual(self.fresh_audit().ledger_balance.available_points, Decimal("125.55"))

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "missing.db"
        engine = create_engine(f"sqlite:///{missing}")
        try:
            with self.assertRaisesRegex(ValueError, "文件不存在"):
                run_points_expiry_task(engine)
            self.assertFalse(missing.exists())
        finally:
            engine.dispose()

    def test_upcoming_cannot_be_applied(self):
        with self.assertRaisesRegex(ValueError, "只读"):
            run_points_expiry_task(self.engine, apply=True, upcoming_days=30)

    def test_cli_json_default_and_blocked_exit_code(self):
        with patch("app.points_expiry_task.create_database_engine", return_value=self.engine):
            with patch("app.mall.points_expiry_service.utc8_now", return_value=NOW):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([]), 0)
                import json
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["mode"], "preview")
                self.assertEqual(payload["page_available_points"], "125.55")
                self.assertTrue(payload["as_of"].endswith("+08:00"))
                self.assertNotIn("phone", output.getvalue())
                self.grant.status = "FROZEN"
                self.db.commit()
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--apply"]), 2)


if __name__ == "__main__":
    unittest.main()
