"""商城订单各未完成状态及已退款状态的只读证据预检。"""

import unittest
from datetime import timedelta

from sqlalchemy import event, update

import test_order_service as fixtures

from app.mall import execute_order_cancellation
from app.mall.audit import MallAuditActionType
from app.mall.order_state_preflight_service import (
    inspect_in_progress_order_state,
    inspect_refunded_order_state,
    inspect_unfulfilled_order_state,
)
from app.models import (
    AdminActionLog, InventoryMovement, Order, OrderItem, PointsAccount,
    PointsLedgerEntry, SupplierSettlementBatch, SupplierSettlementItem,
)


class UnfulfilledOrderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def inspect(self, order_public_id):
        with self.fixture.Session() as db:
            return inspect_unfulfilled_order_state(db, order_public_id=order_public_id)

    def test_created_order_is_read_only_and_has_no_release(self):
        order = self.fixture.place()
        with self.fixture.Session() as db:
            before = db.query(PointsLedgerEntry).count()
            result = inspect_unfulfilled_order_state(
                db, order_public_id=order.order_public_id,
            )
            self.assertEqual(db.query(PointsLedgerEntry).count(), before)
            self.assertFalse(db.new or db.dirty or db.deleted)
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(result.item_count, 2)
        self.assertEqual(result.allocation_count, 2)
        self.assertEqual(result.points_release_entry_ids, ())
        self.assertEqual(result.inventory_release_movement_ids, ())

    def test_member_cancelled_order_has_release_without_admin_audit(self):
        order = self.fixture.place()
        self.fixture.cancel(order.order_public_id)
        result = self.inspect(order.order_public_id)
        self.assertEqual(result.status, "CANCELLED")
        self.assertIsNone(result.cancellation_actor_admin_id)
        self.assertEqual(len(result.points_release_entry_ids), 2)
        self.assertEqual(len(result.inventory_release_movement_ids), 2)

    def test_admin_cancelled_order_has_matching_audit(self):
        order = self.fixture.place()
        execute_order_cancellation(
            self.fixture.engine,
            actor_admin_id=self.fixture.operator.id,
            order_public_id=order.order_public_id,
            reason="用户请求取消",
            now=fixtures.NOW,
        )
        result = self.inspect(order.order_public_id)
        self.assertEqual(result.cancellation_actor_admin_id, self.fixture.operator.id)
        self.assertEqual(len(result.points_release_entry_ids), 2)

    def test_invalid_or_missing_order_is_rejected(self):
        for bad_id in (None, "", "X" * 33):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.inspect(bad_id)
        with self.assertRaisesRegex(ValueError, "订单不存在"):
            self.inspect("MISSING-ORDER")

    def test_fulfilling_order_is_outside_this_slice(self):
        order = self.fixture.place()
        self.fixture.fulfill(order.order_public_id)
        with self.assertRaisesRegex(ValueError, "仅支持待处理或已取消"):
            self.inspect(order.order_public_id)

    def test_dirty_session_is_rejected_before_read(self):
        order = self.fixture.place()
        with self.fixture.Session() as db:
            db.query(Order).filter_by(id=order.order_id).one().updated_at = (
                fixtures.NOW
            )
            with self.assertRaisesRegex(ValueError, "无待写入变更"):
                inspect_unfulfilled_order_state(db, order_public_id=order.order_public_id)

    def test_stray_shipping_audit_is_rejected(self):
        order = self.fixture.place()
        with self.fixture.Session.begin() as db:
            db.add(AdminActionLog(
                admin_id=self.fixture.operator.id,
                action_type=MallAuditActionType.ORDER_SHIP.value,
                target_type="mall_order",
                target_id=order.order_id,
                description="不应存在的发货审计",
                created_at=fixtures.NOW,
            ))
        with self.assertRaises(RuntimeError):
            self.inspect(order.order_public_id)

    def test_tampered_release_evidence_is_rejected(self):
        order = self.fixture.place()
        self.fixture.cancel(order.order_public_id)
        with self.fixture.Session.begin() as db:
            entry = db.query(PointsLedgerEntry).filter_by(entry_type="RELEASE").first()
            entry.reason = "篡改"
        with self.assertRaises(RuntimeError):
            self.inspect(order.order_public_id)

    def test_tampered_admin_cancel_audit_is_rejected(self):
        order = self.fixture.place()
        execute_order_cancellation(
            self.fixture.engine,
            actor_admin_id=self.fixture.operator.id,
            order_public_id=order.order_public_id,
            reason="用户请求取消",
            now=fixtures.NOW,
        )
        with self.fixture.Session.begin() as db:
            log = db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_CANCEL.value,
                target_id=order.order_id,
            ).one()
            log.description = "无效描述"
        with self.assertRaisesRegex(RuntimeError, "取消审计"):
            self.inspect(order.order_public_id)


class InProgressOrderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def inspect(self, public_id):
        with self.fixture.Session() as db:
            return inspect_in_progress_order_state(db, order_public_id=public_id)

    def test_fulfilling_order_is_select_only_and_has_consumption(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.fixture.engine, "before_cursor_execute", capture)
        try:
            result = self.inspect(public_id)
        finally:
            event.remove(self.fixture.engine, "before_cursor_execute", capture)
        self.assertTrue(statements)
        self.assertEqual(set(statements), {"SELECT"})
        self.assertEqual(result.status, "FULFILLING")
        self.assertEqual(result.item_count, 2)
        self.assertEqual(result.allocation_count, 2)
        self.assertEqual(result.fulfillment_actor_admin_id, self.fixture.operator.id)
        self.assertIsNone(result.shipping_action_log_id)
        self.assertEqual(len(result.points_consume_entry_ids), 2)
        self.assertEqual(len(result.inventory_outbound_movement_ids), 2)

    def test_shipped_order_has_matching_shipping_evidence(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        shipped = self.fixture.ship(public_id)
        result = self.inspect(public_id)
        self.assertEqual(result.status, "SHIPPED")
        self.assertEqual(result.shipping_action_log_id, shipped.action_log_id)
        self.assertEqual(len(result.points_consume_entry_ids), 2)

    def test_other_statuses_and_bad_ids_are_rejected(self):
        for bad_id in (None, "", "X" * 33):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.inspect(bad_id)
        with self.assertRaisesRegex(ValueError, "订单不存在"):
            self.inspect("MISSING-ORDER")
        public_id = self.fixture.place().order_public_id
        with self.assertRaisesRegex(ValueError, "仅支持待发货或已发货"):
            self.inspect(public_id)
        self.fixture.cancel(public_id)
        with self.assertRaisesRegex(ValueError, "仅支持待发货或已发货"):
            self.inspect(public_id)

    def test_completion_and_refund_are_outside_this_slice(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        self.fixture.ship(public_id)
        self.fixture.complete(public_id)
        with self.assertRaisesRegex(ValueError, "仅支持待发货或已发货"):
            self.inspect(public_id)
        self.fixture.refund(public_id)
        with self.assertRaisesRegex(ValueError, "仅支持待发货或已发货"):
            self.inspect(public_id)

    def test_dirty_session_and_account_drift_are_rejected(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        with self.fixture.Session() as db:
            db.add(Order(order_public_id="UNSAVED"))
            with self.assertRaisesRegex(ValueError, "无待写入变更"):
                inspect_in_progress_order_state(db, order_public_id=public_id)
        with self.fixture.engine.begin() as conn:
            conn.execute(update(PointsAccount).where(
                PointsAccount.id == self.fixture.account.id
            ).values(available_points=1))
        with self.assertRaisesRegex(ValueError, "余额"):
            self.inspect(public_id)

    def test_tampered_consumption_or_outbound_is_rejected(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        with self.fixture.Session.begin() as db:
            db.query(PointsLedgerEntry).filter_by(entry_type="CONSUME").first().reason = "篡改"
        with self.assertRaises(RuntimeError):
            self.inspect(public_id)
        with self.fixture.Session.begin() as db:
            db.query(PointsLedgerEntry).filter_by(entry_type="CONSUME").first().reason = (
                "订单确认履约消费积分"
            )
            db.query(InventoryMovement).filter_by(movement_type="OUTBOUND").first().reason = "篡改"
        with self.assertRaises(RuntimeError):
            self.inspect(public_id)

    def test_missing_fulfillment_audit_is_rejected(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        with self.fixture.Session.begin() as db:
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_FULFILL.value
            ).delete()
        with self.assertRaisesRegex(RuntimeError, "履约审计"):
            self.inspect(public_id)

    def test_premature_supplier_settlement_is_rejected(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        with self.fixture.Session.begin() as db:
            order = db.query(Order).filter_by(order_public_id=public_id).one()
            item = db.query(OrderItem).filter_by(order_id=order.id).first()
            supplier = self.fixture.supplier
            batch = SupplierSettlementBatch(
                settlement_public_id="SET-PREMATURE-001",
                supplier_id=supplier.id,
                supplier_public_id_snapshot=supplier.supplier_public_id,
                supplier_name_snapshot=supplier.name,
                period_start=fixtures.NOW - timedelta(days=1),
                period_end=fixtures.NOW,
                order_count=1, item_count=1, total_quantity=item.quantity,
                total_cost_amount=item.line_cost_amount,
                generated_by_admin_id=self.fixture.operator.id,
                generated_at=fixtures.NOW + timedelta(minutes=1),
            )
            db.add(batch)
            db.flush()
            db.add(SupplierSettlementItem(
                settlement_batch_id=batch.id, supplier_id=supplier.id,
                order_id=order.id, order_item_id=item.id,
                order_public_id_snapshot=public_id,
                order_completed_at=fixtures.NOW,
                product_public_id_snapshot=item.product_public_id_snapshot,
                product_name_snapshot=item.product_name_snapshot,
                sku_code_snapshot=item.sku_code_snapshot,
                sku_name_snapshot=item.sku_name_snapshot,
                supplier_public_id_snapshot=item.supplier_public_id_snapshot,
                supplier_name_snapshot=item.supplier_name_snapshot,
                unit_cost_price=item.unit_cost_price, quantity=item.quantity,
                line_cost_amount=item.line_cost_amount,
            ))
        with self.assertRaisesRegex(RuntimeError, "提前进入供应商结算"):
            self.inspect(public_id)

    def test_stray_cancellation_and_completion_audit_are_rejected(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        with self.fixture.Session.begin() as db:
            order = db.query(Order).filter_by(order_public_id=public_id).one()
            db.add(AdminActionLog(
                admin_id=self.fixture.operator.id,
                action_type=MallAuditActionType.ORDER_CANCEL.value,
                target_type="mall_order", target_id=order.id,
                description="不应存在的取消审计", created_at=fixtures.NOW,
            ))
        with self.assertRaisesRegex(RuntimeError, "取消审计"):
            self.inspect(public_id)
        with self.fixture.Session.begin() as db:
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_CANCEL.value
            ).delete()
            order = db.query(Order).filter_by(order_public_id=public_id).one()
            db.add(AdminActionLog(
                admin_id=self.fixture.operator.id,
                action_type=MallAuditActionType.ORDER_COMPLETE.value,
                target_type="mall_order", target_id=order.id,
                description="不应存在的完成审计", created_at=fixtures.NOW,
            ))
        with self.assertRaisesRegex(RuntimeError, "完成证据"):
            self.inspect(public_id)

    def test_shipping_audit_and_time_order_are_rechecked(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        self.fixture.ship(public_id)
        with self.fixture.Session.begin() as db:
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_SHIP.value
            ).first().description = "篡改"
        with self.assertRaisesRegex(RuntimeError, "发货审计"):
            self.inspect(public_id)
        with self.fixture.Session.begin() as db:
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_SHIP.value
            ).first().description = (
                f"订单 {public_id} 发货；物流公司 顺丰速运；运单号 SF-M5-5-0001"
            )
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_FULFILL.value
            ).first().created_at = fixtures.NOW + timedelta(minutes=2)
        with self.assertRaisesRegex(RuntimeError, "发货时间早于"):
            self.inspect(public_id)


class RefundedOrderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def refunded_order(self):
        public_id = self.fixture.place().order_public_id
        self.fixture.fulfill(public_id)
        self.fixture.ship(public_id)
        self.fixture.complete(public_id)
        self.fixture.refund(public_id)
        return public_id

    def inspect(self, public_id):
        with self.fixture.Session() as db:
            return inspect_refunded_order_state(db, order_public_id=public_id)

    def test_refunded_order_is_select_only_with_full_refund_evidence(self):
        public_id = self.refunded_order()
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.fixture.engine, "before_cursor_execute", capture)
        try:
            result = self.inspect(public_id)
        finally:
            event.remove(self.fixture.engine, "before_cursor_execute", capture)
        self.assertTrue(statements)
        self.assertEqual(set(statements), {"SELECT"})
        self.assertEqual(result.status, "REFUNDED")
        self.assertEqual(result.item_count, 2)
        self.assertEqual(result.allocation_count, 2)
        self.assertEqual(result.refund_actor_admin_id, self.fixture.operator.id)
        self.assertTrue(result.fulfillment_action_log_id)
        self.assertTrue(result.shipping_action_log_id)
        self.assertTrue(result.completion_action_log_id)
        self.assertTrue(result.refund_action_log_id)
        self.assertEqual(len(result.points_refund_entry_ids), 2)
        self.assertEqual(len(result.inventory_return_movement_ids), 2)

    def test_rejects_other_statuses_bad_ids_and_dirty_session(self):
        for bad_id in (None, "", "X" * 33):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.inspect(bad_id)
        with self.assertRaisesRegex(ValueError, "订单不存在"):
            self.inspect("MISSING-ORDER")
        public_id = self.fixture.place().order_public_id
        with self.assertRaisesRegex(ValueError, "仅支持已退款"):
            self.inspect(public_id)
        self.fixture.fulfill(public_id)
        self.fixture.ship(public_id)
        self.fixture.complete(public_id)
        with self.assertRaisesRegex(ValueError, "仅支持已退款"):
            self.inspect(public_id)
        self.fixture.refund(public_id)
        with self.fixture.Session() as db:
            db.add(Order(order_public_id="UNSAVED"))
            with self.assertRaisesRegex(ValueError, "无待写入变更"):
                inspect_refunded_order_state(db, order_public_id=public_id)

    def test_refund_points_and_inventory_tampering_fail_closed(self):
        public_id = self.refunded_order()
        with self.fixture.Session.begin() as db:
            db.query(PointsLedgerEntry).filter_by(entry_type="REFUND").first().reason = "篡改"
        with self.assertRaisesRegex(RuntimeError, "退款证据"):
            self.inspect(public_id)
        with self.fixture.Session.begin() as db:
            db.query(PointsLedgerEntry).filter_by(entry_type="REFUND").first().reason = "订单退款退回积分"
            db.query(InventoryMovement).filter_by(movement_type="RETURN").first().reason = "篡改"
        with self.assertRaisesRegex(RuntimeError, "退回证据"):
            self.inspect(public_id)

    def test_missing_refund_audit_and_reversed_time_fail_closed(self):
        public_id = self.refunded_order()
        with self.fixture.Session.begin() as db:
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_REFUND.value
            ).delete()
        with self.assertRaisesRegex(RuntimeError, "退款审计"):
            self.inspect(public_id)
        with self.fixture.Session.begin() as db:
            order = db.query(Order).filter_by(order_public_id=public_id).one()
            db.add(AdminActionLog(
                admin_id=self.fixture.operator.id,
                action_type=MallAuditActionType.ORDER_REFUND.value,
                target_type="mall_order", target_id=order.id,
                description=(
                    f"订单 {public_id} 整单退款；积分 {order.total_points:.2f}；"
                    f"原因 {order.refund_reason}"
                ),
                created_at=order.refunded_at,
            ))
            fulfillment = db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_FULFILL.value
            ).one()
            fulfillment.created_at = order.shipped_at + timedelta(minutes=1)
        with self.assertRaisesRegex(RuntimeError, "发货时间早于"):
            self.inspect(public_id)

    def test_cancel_audit_and_supplier_settlement_are_rejected(self):
        public_id = self.refunded_order()
        with self.fixture.Session.begin() as db:
            order = db.query(Order).filter_by(order_public_id=public_id).one()
            db.add(AdminActionLog(
                admin_id=self.fixture.operator.id,
                action_type=MallAuditActionType.ORDER_CANCEL.value,
                target_type="mall_order", target_id=order.id,
                description="异常取消审计", created_at=fixtures.NOW,
            ))
        with self.assertRaisesRegex(RuntimeError, "取消审计"):
            self.inspect(public_id)
        with self.fixture.Session.begin() as db:
            db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ORDER_CANCEL.value
            ).delete()
            order = db.query(Order).filter_by(order_public_id=public_id).one()
            item = db.query(OrderItem).filter_by(order_id=order.id).first()
            supplier = self.fixture.supplier
            batch = SupplierSettlementBatch(
                settlement_public_id="SET-REFUND-PREFLIGHT-001",
                supplier_id=supplier.id,
                supplier_public_id_snapshot=supplier.supplier_public_id,
                supplier_name_snapshot=supplier.name,
                period_start=fixtures.NOW - timedelta(days=1),
                period_end=fixtures.NOW,
                order_count=1, item_count=1, total_quantity=item.quantity,
                total_cost_amount=item.line_cost_amount,
                generated_by_admin_id=self.fixture.operator.id,
                generated_at=fixtures.NOW + timedelta(minutes=1),
            )
            db.add(batch)
            db.flush()
            db.add(SupplierSettlementItem(
                settlement_batch_id=batch.id, supplier_id=supplier.id,
                order_id=order.id, order_item_id=item.id,
                order_public_id_snapshot=public_id,
                order_completed_at=order.completed_at,
                product_public_id_snapshot=item.product_public_id_snapshot,
                product_name_snapshot=item.product_name_snapshot,
                sku_code_snapshot=item.sku_code_snapshot,
                sku_name_snapshot=item.sku_name_snapshot,
                supplier_public_id_snapshot=item.supplier_public_id_snapshot,
                supplier_name_snapshot=item.supplier_name_snapshot,
                unit_cost_price=item.unit_cost_price, quantity=item.quantity,
                line_cost_amount=item.line_cost_amount,
            ))
        with self.assertRaisesRegex(RuntimeError, "供应商结算项"):
            self.inspect(public_id)


if __name__ == "__main__":
    unittest.main()
