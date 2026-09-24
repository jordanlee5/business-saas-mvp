"""商城订单后台只读页面、查询边界与角色隔离。"""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import (
    admin_navigation_context, mall_order_detail_page, mall_orders_page,
)
from app.mall.order_reporting_service import (
    get_mall_order_detail, list_mall_orders,
)
import test_order_service


def request(path="/mall-orders"):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "root_path": "",
        "query_string": b"", "headers": [],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


def actor(level, *, active=True):
    return SimpleNamespace(
        id=11, username="operator", role="admin",
        admin_level=level, is_active=active,
    )


class MallOrderReportingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_order_service.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_real_order_page_detail_snapshot_and_totals(self):
        placed = self.fixture.place()
        with self.fixture.Session() as db:
            page = list_mall_orders(db)
            self.assertEqual(page.total, 1)
            self.assertEqual(page.items[0].order_public_id, placed.order_public_id)
            self.assertEqual(page.items[0].status_label, "待处理")
            self.assertEqual(page.items[0].member_public_id, "MEM-ORDER-001")
            self.assertEqual(list_mall_orders(db, keyword="missing").total, 0)
            self.assertEqual(list_mall_orders(db, status="SHIPPED").total, 0)
            self.assertEqual(list_mall_orders(db, status="CREATED").total, 1)
            self.assertEqual(list_mall_orders(db, page=20).page, 1)
            detail = get_mall_order_detail(db, order_public_id=placed.order_public_id)
            self.assertEqual(detail.order.total_points, Decimal("105.00"))
            self.assertEqual(sum(i.line_points for i in detail.items), Decimal("105.00"))
            self.assertEqual(len(detail.items), 2)
            self.assertEqual(detail.shipping_carrier, None)
            with self.assertRaisesRegex(ValueError, "状态无效"):
                list_mall_orders(db, status="INVALID")
            with self.assertRaisesRegex(ValueError, "分页"):
                list_mall_orders(db, page_size=101)
            with self.assertRaisesRegex(ValueError, "不存在"):
                get_mall_order_detail(db, order_public_id="MISSING")

    def test_tampered_totals_fail_closed(self):
        placed = self.fixture.place()
        with self.fixture.Session() as db:
            from app.models import OrderItem
            item = db.query(OrderItem).filter_by(order_id=placed.order_id).first()
            item.unit_points_price += Decimal("1.00")
            item.line_points += Decimal(item.quantity)
            db.flush()
            with self.assertRaisesRegex(RuntimeError, "证据不完整"):
                get_mall_order_detail(db, order_public_id=placed.order_public_id)

    def test_lifecycle_status_and_logistics_are_readable(self):
        placed = self.fixture.place()
        self.fixture.fulfill(placed.order_public_id)
        self.fixture.ship(placed.order_public_id)
        with self.fixture.Session() as db:
            detail = get_mall_order_detail(db, order_public_id=placed.order_public_id)
            self.assertEqual(detail.order.status_label, "已发货")
            self.assertEqual(detail.shipping_carrier, "顺丰速运")
            self.assertTrue(detail.tracking_number)
            self.assertEqual(list_mall_orders(db, status="SHIPPED").total, 1)
        self.fixture.complete(placed.order_public_id)
        self.fixture.refund(placed.order_public_id)
        with self.fixture.Session() as db:
            detail = get_mall_order_detail(db, order_public_id=placed.order_public_id)
            self.assertEqual(detail.order.status_label, "已退款")
            self.assertTrue(detail.refund_reason)
            self.assertIsNotNone(detail.refunded_at)

    def test_routes_guard_before_db_and_render_empty_state(self):
        for user, allowed in (
            (actor(SUPER_ADMIN), True), (actor(OPERATOR), True),
            (actor(PRIMARY_REVIEWER), False),
            (actor(OPERATOR, active=False), False), (None, False),
            (SimpleNamespace(role="partner", is_active=True,
                             admin_level=SUPER_ADMIN), False),
        ):
            with self.subTest(user=user):
                with patch("app.main.get_current_user", return_value=user):
                    self.assertEqual(
                        admin_navigation_context(request())["can_view_mall_orders"],
                        allowed,
                    )
                    if not allowed:
                        with patch("app.main.SessionLocal") as session:
                            page = mall_orders_page(request(), "", "ALL", 1, 20, "")
                            detail = mall_order_detail_page(request(), "ORD-001")
                        expected = "/login" if user is None else "/dashboard"
                        self.assertEqual(page.headers["location"], expected)
                        self.assertEqual(detail.headers["location"], expected)
                        session.assert_not_called()
        with (
            patch("app.main.get_current_user", return_value=actor(OPERATOR)),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            response = mall_orders_page(request(), "", "ALL", 1, 20, "")
            self.assertEqual(response.status_code, 200)
            self.assertIn("暂无符合条件".encode(), response.body)
            self.assertNotIn(b"/mall-orders/ORD-", response.body)

    def test_routes_render_real_detail_and_reject_bad_filter(self):
        placed = self.fixture.place()
        with (
            patch("app.main.get_current_user", return_value=actor(OPERATOR)),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            page = mall_orders_page(request(), "", "CREATED", 1, 20, "")
            self.assertIn(placed.order_public_id.encode(), page.body)
            detail = mall_order_detail_page(
                request(f"/mall-orders/{placed.order_public_id}"),
                placed.order_public_id,
            )
            self.assertEqual(detail.status_code, 200)
            self.assertIn("105.00".encode(), detail.body)
            invalid = mall_orders_page(request(), "", "UNKNOWN", 1, 20, "")
            self.assertIn("状态无效".encode(), invalid.body)


if __name__ == "__main__":
    unittest.main()
