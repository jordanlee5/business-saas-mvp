import unittest
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import event, update

from app.mall import (
    execute_supplier_settlement_confirmation,
    execute_supplier_settlement_generation,
    reconcile_completed_order,
)
from app.admin_permissions import SUPER_ADMIN
from app.models import InventoryBalance, PointsAccount, Supplier, SupplierSettlementItem, User
import test_order_service as order_fixture


class CompletedOrderReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = order_fixture.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def completed_order(self):
        order_public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(order_public_id)
        self.fixture.ship(order_public_id)
        self.fixture.complete(order_public_id)
        return order_public_id

    def reconcile(self, order_public_id):
        with self.fixture.Session() as db:
            return reconcile_completed_order(db, order_public_id=order_public_id)

    def generate(self):
        return execute_supplier_settlement_generation(
            self.fixture.engine,
            actor_admin_id=self.fixture.operator.id,
            supplier_id=self.fixture.supplier.id,
            period_start=order_fixture.NOW,
            period_end=order_fixture.NOW + timedelta(days=1),
            now=order_fixture.NOW + timedelta(days=2),
        )

    def test_completed_order_without_settlement_is_read_only(self):
        public_id = self.completed_order()
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.fixture.engine, "before_cursor_execute", capture)
        try:
            result = self.reconcile(public_id)
        finally:
            event.remove(self.fixture.engine, "before_cursor_execute", capture)

        self.assertTrue(statements)
        self.assertEqual(set(statements), {"SELECT"})
        self.assertEqual(result.total_points, Decimal("105.00"))
        self.assertEqual(result.total_quantity, 3)
        self.assertEqual(result.total_cost_amount, Decimal("29.00"))
        self.assertEqual(result.pending_cost_amount, Decimal("0.00"))
        self.assertEqual(result.confirmed_cost_amount, Decimal("0.00"))
        self.assertEqual(result.unbatched_cost_amount, Decimal("29.00"))
        self.assertEqual(len(result.lines), 2)
        self.assertTrue(all(line.settlement_status is None for line in result.lines))

    def test_pending_then_confirmed_batch_matches_every_order_item(self):
        public_id = self.completed_order()
        batch = self.generate()
        pending = self.reconcile(public_id)
        self.assertEqual(pending.pending_cost_amount, Decimal("29.00"))
        self.assertEqual(pending.unbatched_cost_amount, Decimal("0.00"))
        self.assertEqual(
            {line.settlement_public_id for line in pending.lines},
            {batch.settlement_public_id},
        )

        with self.fixture.Session.begin() as db:
            admin = User(
                username="reconciliation-super-admin",
                password_hash="test-only",
                role="admin",
                admin_level=SUPER_ADMIN,
                is_active=True,
            )
            db.add(admin)
            db.flush()
            admin_id = admin.id
        execute_supplier_settlement_confirmation(
            self.fixture.engine,
            actor_admin_id=admin_id,
            settlement_public_id=batch.settlement_public_id,
            now=order_fixture.NOW + timedelta(days=2, minutes=1),
        )
        confirmed = self.reconcile(public_id)
        self.assertEqual(confirmed.confirmed_cost_amount, Decimal("29.00"))
        self.assertEqual(confirmed.pending_cost_amount, Decimal("0.00"))
        self.assertEqual(
            {line.settlement_status for line in confirmed.lines},
            {"CONFIRMED"},
        )

    def test_partial_supplier_batch_keeps_other_line_unbatched(self):
        second_supplier = Supplier(
            supplier_public_id="SUP-ORDER-002",
            name="另一个结算供应商",
            is_active=True,
            created_at=order_fixture.NOW,
            updated_at=order_fixture.NOW,
        )
        self.fixture.db.add(second_supplier)
        self.fixture.db.flush()
        self.fixture.sku_two.supplier_id = second_supplier.id
        self.fixture.db.commit()
        public_id = self.completed_order()
        self.generate()
        result = self.reconcile(public_id)
        self.assertEqual(result.pending_cost_amount, Decimal("24.00"))
        self.assertEqual(result.unbatched_cost_amount, Decimal("5.00"))
        self.assertEqual(result.confirmed_cost_amount, Decimal("0.00"))
        self.assertEqual(
            sum(line.settlement_status is None for line in result.lines), 1
        )

    def test_rejects_noncompleted_and_refunded_orders(self):
        public_id = self.fixture.place().order_public_id
        with self.assertRaisesRegex(ValueError, "仅支持已完成"):
            self.reconcile(public_id)
        self.fixture.fulfill(public_id)
        self.fixture.ship(public_id)
        self.fixture.complete(public_id)
        self.fixture.refund(public_id)
        with self.assertRaisesRegex(ValueError, "仅支持已完成"):
            self.reconcile(public_id)

    def test_rejects_points_and_inventory_balance_drift(self):
        public_id = self.completed_order()
        with self.fixture.engine.begin() as conn:
            conn.execute(update(PointsAccount).where(
                PointsAccount.id == self.fixture.account.id
            ).values(available_points=Decimal("1.00")))
        with self.assertRaisesRegex(ValueError, "余额"):
            self.reconcile(public_id)
        with self.fixture.engine.begin() as conn:
            conn.execute(update(PointsAccount).where(
                PointsAccount.id == self.fixture.account.id
            ).values(available_points=Decimal("65.00")))
            conn.execute(update(InventoryBalance).where(
                InventoryBalance.sku_id == self.fixture.sku_one.id
            ).values(on_hand_quantity=99))
        with self.assertRaisesRegex(RuntimeError, "库存"):
            self.reconcile(public_id)

    def test_rejects_settlement_snapshot_drift(self):
        public_id = self.completed_order()
        self.generate()
        with self.fixture.engine.begin() as conn:
            conn.execute(update(SupplierSettlementItem).values(
                order_public_id_snapshot="ORD-CORRUPTED"
            ))
        with self.assertRaises((RuntimeError, ValueError)):
            self.reconcile(public_id)

    def test_rejects_pending_session_changes(self):
        public_id = self.completed_order()
        with self.fixture.Session() as db:
            db.add(User(username="unsaved", password_hash="test-only"))
            with self.assertRaisesRegex(ValueError, "待写入"):
                reconcile_completed_order(db, order_public_id=public_id)


if __name__ == "__main__":
    unittest.main()
