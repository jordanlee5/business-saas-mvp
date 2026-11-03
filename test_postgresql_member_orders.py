import os
import re
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event, local
from unittest.mock import patch
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
import test_miniprogram_orders as cases
from app.database import Base, create_database_engine, resolve_database_url
from test_postgresql_migration import validate_postgresql_test_database_url, get_database_state, build_alembic_config

class PostgreSQLMemberOrderTests(cases.MemberOrderTests):
    @classmethod
    def setUpClass(cls):
        configured=os.environ.get('POSTGRES_TEST_DATABASE_URL','')
        if not configured:raise unittest.SkipTest('未配置独立 PostgreSQL 测试数据库')
        if os.environ.get('POSTGRES_TEST_ALLOW_RESET')!='1':raise RuntimeError('必须显式允许独立测试库重置')
        cls.url=validate_postgresql_test_database_url(configured,resolve_database_url())
    def setUp(self):
        tables,revision=get_database_state(self.url)
        if tables-{'alembic_version'} or revision is not None:raise RuntimeError('测试库非空，停止')
        config=build_alembic_config(self.url);command.upgrade(config,'head')
        engine=create_database_engine(self.url)
        def cleanup():
            with engine.begin() as c:
                # Only fixtures created after this test's verified empty-database guard.
                for table in reversed(Base.metadata.sorted_tables):
                    c.execute(table.delete())
            engine.dispose();command.downgrade(config,'base')
            tables,revision=get_database_state(self.url)
            self.assertFalse(tables-{'alembic_version'});self.assertIsNone(revision)
        self.addCleanup(cleanup)
        with patch('test_order_service.create_engine',return_value=engine):super().setUp()

    def test_refund_commits_while_first_settlement_is_waiting(self):
        self._race_refund_and_settlement("refund")

    def test_first_settlement_commits_while_refund_is_waiting(self):
        self._race_refund_and_settlement("settle")

    def test_refund_and_settlement_in_two_sessions_of_same_operator(self):
        self._race_refund_and_settlement("refund", same_operator=True)

    def test_settlement_then_refund_in_two_sessions_of_same_operator(self):
        self._race_refund_and_settlement("settle", same_operator=True)

    def _race_refund_and_settlement(self, first, *, same_operator=False):
        from app.admin_permissions import OPERATOR
        from app.mall import execute_order_shipping, execute_supplier_settlement_generation
        from app.mall.order_reconciliation_service import reconcile_completed_order
        from app.models import (AdminActionLog, InventoryBalance, InventoryMovement, Order, OrderItem,
                                PointsAccount, PointsGrant, PointsLedgerEntry, ProductSku, Supplier,
                                SupplierSettlementBatch, SupplierSettlementItem, User)
        from test_order_service import NOW

        f = self.f
        with f.Session.begin() as db:
            other = Supplier(supplier_public_id="SUP-RACE-B", name="第二家测试供应商", is_active=True)
            operator = User(username="settlement-race-operator", password_hash="test-only",
                            role="admin", admin_level=OPERATOR, is_active=True)
            db.add_all([other, operator])
            db.flush()
            db.get(ProductSku, f.sku_two.id).supplier_id = other.id
            settlement_actor = f.operator.id if same_operator else operator.id

        placed = f.place()
        oid = placed.order_public_id
        f.fulfill(oid)
        with f.Session() as db:
            lines = db.query(OrderItem).filter_by(order_id=placed.order_id).order_by(OrderItem.id).all()
        for line in lines:
            execute_order_shipping(f.engine, actor_admin_id=f.operator.id, order_public_id=oid,
                                   shipping_carrier="测试物流", tracking_number=f"TEST-{line.id}",
                                   quantities={line.id: line.quantity}, now=NOW + timedelta(minutes=1))
        f.complete(oid)
        supplier_id, refund_actor = f.supplier.id, f.operator.id

        def settle():
            return execute_supplier_settlement_generation(
                f.engine, actor_admin_id=settlement_actor, supplier_id=supplier_id,
                period_start=NOW, period_end=NOW + timedelta(days=1), now=NOW + timedelta(days=2))

        actions = {"refund": lambda: f.refund(oid, actor_admin_id=refund_actor), "settle": settle}
        second = "settle" if first == "refund" else "refund"
        reason = "没有未结算" if second == "settle" else "已进入供应商结算"
        run_with_order_lock_wait(self, f.engine, actions[first], actions[second], reason, settlement_actor)

        # A new request after both transactions finish must see the same decision.
        with self.assertRaisesRegex(ValueError, reason):
            actions[second]()
        if first == "refund":
            self.assertTrue(actions[first]().replayed)
        else:
            with self.assertRaisesRegex(ValueError, "没有未结算"):
                settle()

        refunded = first == "refund"
        with f.Session() as db:
            self.assertEqual(db.get(Order, placed.order_id).status, "REFUNDED" if refunded else "COMPLETED")
            account = db.get(PointsAccount, f.account.id)
            self.assertEqual((account.available_points, account.reserved_points), (170 if refunded else 65, 0))
            grants = db.query(PointsGrant).filter_by(account_id=account.id).order_by(PointsGrant.id)
            self.assertEqual([(g.available_points, g.reserved_points) for g in grants],
                             [(70, 0), (100, 0)] if refunded else [(0, 0), (65, 0)])
            entries = db.query(PointsLedgerEntry).filter_by(reference_type="ORDER", reference_id=oid, entry_type="REFUND")
            self.assertEqual(sorted((e.grant_id, e.available_points_delta) for e in entries),
                             [(f.early_grant.id, 70), (f.later_grant.id, 35)] if refunded else [])
            stock = db.query(InventoryBalance).order_by(InventoryBalance.sku_id)
            self.assertEqual([(b.on_hand_quantity, b.reserved_quantity) for b in stock],
                             [(5, 0), (4, 0)] if refunded else [(3, 0), (3, 0)])
            returns = db.query(InventoryMovement).filter_by(reference_type="ORDER", reference_id=oid, movement_type="RETURN")
            self.assertEqual(sorted((m.sku_id, m.quantity_delta) for m in returns),
                             [(f.sku_one.id, 2), (f.sku_two.id, 1)] if refunded else [])
            batches = db.query(SupplierSettlementBatch).all()
            self.assertEqual(len(batches), 0 if refunded else 1)
            settlement_items = db.query(SupplierSettlementItem).all()
            self.assertEqual([(i.order_id, i.supplier_id, i.line_cost_amount) for i in settlement_items],
                             [] if refunded else [(placed.order_id, supplier_id, 24)])
            if batches:
                self.assertEqual((batches[0].status, batches[0].total_cost_amount), ("PENDING_CONFIRMATION", 24))
                report = reconcile_completed_order(db, order_public_id=oid)
                self.assertEqual((report.pending_cost_amount, report.unbatched_cost_amount), (24, 5))
            logs = db.query(AdminActionLog).filter(AdminActionLog.action_type.in_(
                ["mall_order_refund", "mall_supplier_settlement_generate"]))
            winner = ("mall_order_refund", refund_actor) if refunded else ("mall_supplier_settlement_generate", settlement_actor)
            self.assertEqual(Counter((log.action_type, log.admin_id) for log in logs), Counter([winner]))


def run_with_order_lock_wait(test, engine, first_action, second_action, rejection, settlement_actor):
    """Run both normal write paths and observe their actual row-lock contention in PostgreSQL."""
    worker = local()
    release = Event()
    holder_pid, waiter_pid = Queue(), Queue()

    def begin(conn):
        role = getattr(worker, "role", None)
        if role:
            conn.exec_driver_sql("SET LOCAL lock_timeout = '15s'")
        if role == "second":
            waiter_pid.put(conn.connection.driver_connection.info.backend_pid)

    def after_query(conn, cursor, statement, parameters, context, executemany):
        if getattr(worker, "role", None) != "first" or release.is_set():
            return
        if "FOR UPDATE" in statement and re.search(r"\b(?:FROM|JOIN)\s+orders\b", statement, re.I):
            holder_pid.put(conn.connection.driver_connection.info.backend_pid)
            if not release.wait(12):
                raise AssertionError("Timed out before observing the competing transaction")

    def run(role, action):
        worker.role = role
        return action()

    event.listen(engine, "begin", begin)
    event.listen(engine, "after_cursor_execute", after_query)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(run, "first", first_action)
                holder = holder_pid.get(timeout=5)
                second = pool.submit(run, "second", second_action)
                waiter = waiter_pid.get(timeout=5)
                test.assertNotEqual(holder, waiter)
                with engine.connect() as observer:
                    deadline = time.monotonic() + 5
                    while holder not in observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}):
                        if second.done():
                            second.result()
                            test.fail("Second operation finished without waiting on the first")
                        if time.monotonic() >= deadline:
                            test.fail("PostgreSQL did not report the expected blocking transaction")
                        time.sleep(0.02)
                # The generator has passed authorization and reached the order row.
                # Its shared permission lock must still exclude an account update.
                with test.assertRaises(OperationalError) as locked:
                    with engine.begin() as probe:
                        probe.execute(text("SELECT id FROM users WHERE id=:id FOR NO KEY UPDATE NOWAIT"),
                                      {"id": settlement_actor})
                test.assertEqual(locked.exception.orig.sqlstate, "55P03")
            finally:
                release.set()
            first.result(timeout=15)
            with test.assertRaisesRegex(ValueError, rejection):
                second.result(timeout=15)
    finally:
        event.remove(engine, "begin", begin)
        event.remove(engine, "after_cursor_execute", after_query)
