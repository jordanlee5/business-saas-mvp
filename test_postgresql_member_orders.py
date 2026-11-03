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

    def _submit_overlap_pair(self, first_action, second_action, second_actor):
        # Hold the first batch transaction open until PostgreSQL reports the
        # second operator waiting for it on the same supplier.
        run_with_order_lock_wait(self, self.f.engine, first_action, second_action, None, second_actor)

    def test_first_settlement_commits_while_refund_is_waiting(self):
        self._race_refund_and_settlement("settle")

    def test_refund_and_settlement_in_two_sessions_of_same_operator(self):
        self._race_refund_and_settlement("refund", same_operator=True)

    def test_settlement_then_refund_in_two_sessions_of_same_operator(self):
        self._race_refund_and_settlement("settle", same_operator=True)

    def test_batch_settles_remaining_orders_after_competing_refund(self):
        self._race_multi_order_batch(refund_first=True)

    def test_batch_includes_all_orders_when_it_precedes_competing_refund(self):
        self._race_multi_order_batch(refund_first=False)

    def _race_multi_order_batch(self, *, refund_first):
        from io import BytesIO
        from openpyxl import load_workbook
        from app.admin_permissions import OPERATOR, SUPER_ADMIN
        from app.mall import (OrderLineRequest, confirm_supplier_settlement, execute_supplier_settlement_export,
                              execute_supplier_settlement_generation)
        from app.mall.supplier_settlement_reporting_service import get_supplier_settlement_detail
        from app.models import (AdminActionLog, InventoryBalance, InventoryMovement, Order, PointsAccount,
                                PointsLedgerEntry, SupplierSettlementBatch, User)
        from test_order_service import NOW

        f = self.f
        sku_a, sku_b = f.sku_one.id, f.sku_two.id
        supplier_id, refund_actor = f.supplier.id, f.operator.id
        with f.Session.begin() as db:
            operator = User(username="batch-operator", password_hash="test-only", role="admin",
                            admin_level=OPERATOR, is_active=True)
            db.add(operator)
            db.flush()
            batch_actor = operator.id

        # The middle order has two lines, so both must enter or leave the batch together.
        orders = []
        for index, quantities in enumerate([[(sku_a, 1)], [(sku_a, 1), (sku_b, 1)], [(sku_b, 2)]]):
            order = f.place(key=f"batch-order-{index}", lines=[OrderLineRequest(sku, qty) for sku, qty in quantities])
            f.fulfill(order.order_public_id)
            f.ship(order.order_public_id, tracking_number=f"BATCH-PARCEL-{index}")
            f.complete(order.order_public_id)
            orders.append(order.order_public_id)
        left, middle, right = orders

        def settle():
            return execute_supplier_settlement_generation(
                f.engine, actor_admin_id=batch_actor, supplier_id=supplier_id,
                period_start=NOW, period_end=NOW + timedelta(days=1), now=NOW + timedelta(days=2))

        def refund():
            return f.refund(middle, actor_admin_id=refund_actor)

        if refund_first:
            run_with_order_lock_wait(self, f.engine, refund, settle, None, batch_actor)
            self.assertTrue(refund().replayed)
        else:
            run_with_order_lock_wait(self, f.engine, settle, refund, "已进入供应商结算", batch_actor)
            with self.assertRaisesRegex(ValueError, "已进入供应商结算"):
                refund()
        with self.assertRaisesRegex(ValueError, "没有未结算"):
            settle()

        with f.Session() as db:
            db.connection(execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
            self.assertEqual(db.scalar(text("SHOW transaction_read_only")), "on")
            batch = db.query(SupplierSettlementBatch).one()
            batch_public_id = batch.settlement_public_id
            detail = get_supplier_settlement_detail(db, settlement_public_id=batch_public_id)
            expected_lines = [(left, f.sku_one.sku_code, 1, 12), (right, f.sku_two.sku_code, 2, 10)]
            if not refund_first:
                expected_lines += [(middle, f.sku_one.sku_code, 1, 12), (middle, f.sku_two.sku_code, 1, 5)]
            self.assertCountEqual([(i.order_public_id_snapshot, i.sku_code_snapshot, i.quantity, i.line_cost_amount)
                                   for i in detail.items], expected_lines)
            self.assertEqual((detail.status, detail.order_count, detail.item_count, detail.total_quantity,
                              detail.total_cost_amount),
                             ("PENDING_CONFIRMATION", 2, 2, 3, 22) if refund_first
                             else ("PENDING_CONFIRMATION", 3, 4, 5, 39))
            self.assertEqual(dict(db.query(Order.order_public_id, Order.status).all()),
                             {left: "COMPLETED", middle: "REFUNDED" if refund_first else "COMPLETED", right: "COMPLETED"})
            account = db.get(PointsAccount, f.account.id)
            self.assertEqual((account.available_points, account.reserved_points), (80 if refund_first else 15, 0))
            refunds = db.query(PointsLedgerEntry).filter_by(entry_type="REFUND")
            self.assertCountEqual([(row.reference_id, row.grant_id, row.available_points_delta) for row in refunds],
                                  [(middle, f.early_grant.id, 30), (middle, f.later_grant.id, 35)] if refund_first else [])
            stock = db.query(InventoryBalance).order_by(InventoryBalance.sku_id)
            self.assertEqual([(b.on_hand_quantity, b.reserved_quantity) for b in stock],
                             [(4, 0), (2, 0)] if refund_first else [(3, 0), (1, 0)])
            returns = db.query(InventoryMovement).filter_by(movement_type="RETURN")
            self.assertCountEqual([(m.reference_id, m.sku_id, m.quantity_delta) for m in returns],
                                  [(middle, sku_a, 1), (middle, sku_b, 1)] if refund_first else [])
            logs = db.query(AdminActionLog).filter(AdminActionLog.action_type.in_(
                ["mall_order_refund", "mall_supplier_settlement_generate"]))
            expected_logs = [("mall_supplier_settlement_generate", batch_actor)]
            if refund_first:
                expected_logs.append(("mall_order_refund", refund_actor))
            self.assertCountEqual([(log.action_type, log.admin_id) for log in logs], expected_logs)

            # A viewer keeps one coherent version while a different connection confirms.
            with f.Session.begin() as writer:
                writer.execute(text("SET LOCAL lock_timeout = '2s'"))
                reviewer = User(username="batch-reviewer", password_hash="test-only", role="admin",
                                admin_level=SUPER_ADMIN, is_active=True)
                writer.add(reviewer)
                writer.flush()
                confirm_supplier_settlement(writer, actor_admin_id=reviewer.id,
                                            settlement_public_id=batch_public_id, now=NOW + timedelta(days=3))
            db.expire_all()
            self.assertEqual(get_supplier_settlement_detail(db, settlement_public_id=batch_public_id).status,
                             "PENDING_CONFIRMATION")

        from app.main import mall_settlement_detail_page
        from test_supplier_settlement_routes import request
        path = f"/mall-settlements/{batch_public_id}"
        with patch("app.main.SessionLocal", f.Session), patch("app.main.get_current_user", return_value=f.operator):
            page = mall_settlement_detail_page(request(path), batch_public_id)
        self.assertEqual(page.status_code, 200)
        html = page.body.decode()
        self.assertIn("<dt>状态</dt><dd>已确认</dd>", html)
        self.assertIn(f"<dd>¥ {detail.total_cost_amount:.2f}</dd>", html)
        self.assertIn(left, html)
        self.assertIn(right, html)
        self.assertEqual(middle in html, not refund_first)

        exported = execute_supplier_settlement_export(
            f.engine, actor_admin_id=batch_actor, settlement_public_id=batch_public_id, now=NOW + timedelta(days=4))
        self.assertEqual(exported.status, "CONFIRMED")
        workbook = load_workbook(BytesIO(exported.content), read_only=True)
        try:
            summary = dict(workbook["结算汇总"].values)
            self.assertEqual((summary["订单数"], summary["订单项数"], summary["人民币成本总额"]),
                             (detail.order_count, detail.item_count, detail.total_cost_amount))
            rows = workbook["结算明细"].values
            header = next(rows)
            columns = [header.index(key) for key in ("订单公开编号", "SKU编码", "数量", "行成本")]
            self.assertCountEqual([tuple(row[i] for i in columns) for row in rows], expected_lines)
        finally:
            workbook.close()

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
            if rejection is None:
                second.result(timeout=15)
            else:
                with test.assertRaisesRegex(ValueError, rejection):
                    second.result(timeout=15)
    finally:
        event.remove(engine, "begin", begin)
        event.remove(engine, "after_cursor_execute", after_query)
