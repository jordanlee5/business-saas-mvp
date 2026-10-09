"""待处理与已取消订单的只读证据预检。"""

import unittest

import test_order_service as fixtures

from app.mall import execute_order_cancellation
from app.mall.audit import MallAuditActionType
from app.mall.order_state_preflight_service import inspect_unfulfilled_order_state
from app.models import AdminActionLog, Order, PointsLedgerEntry


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


if __name__ == "__main__":
    unittest.main()
