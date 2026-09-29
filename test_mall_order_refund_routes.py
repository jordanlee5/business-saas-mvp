"""商城订单整单退款后台入口的权限、令牌和资源恢复。"""

import unittest
from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import mall_order_detail_page, refund_mall_order_route
from app.models import AdminActionLog, Order, PointsAccount, InventoryBalance
import test_order_service


def request(path, *, method="GET", cookie=""):
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "root_path": "", "query_string": b"",
        "headers": [(b"cookie", cookie.encode())] if cookie else [],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


class MallOrderRefundRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_order_service.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()
        self.placed = self.fixture.place()
        self.path = f"/mall-orders/{self.placed.order_public_id}"

    def tearDown(self):
        self.fixture.tearDown()

    def _detail(self):
        with (
            patch("app.main.get_current_user", return_value=self.fixture.operator),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            return mall_order_detail_page(request(self.path), self.placed.order_public_id)

    def _submit(self, *, token, cookie, reason="客户确认整单退货", order_public_id=None):
        public_id = order_public_id or self.placed.order_public_id
        with (
            patch("app.main.get_current_user", return_value=self.fixture.operator),
            patch("app.main.engine", self.fixture.engine),
            patch("app.mall.order_refund_service.utc8_now", return_value=(
                test_order_service.NOW + timedelta(minutes=3)
            )),
        ):
            return refund_mall_order_route(
                request(f"/mall-orders/{public_id}/refund", method="POST", cookie=cookie),
                public_id, reason, token,
            )

    def _completed_token(self):
        self.fixture.fulfill(self.placed.order_public_id)
        self.fixture.ship(self.placed.order_public_id)
        self.fixture.complete(self.placed.order_public_id)
        response = self._detail()
        self.assertIn("整单退款".encode(), response.body)
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        token = cookie["mall_order_refund_csrf"].value
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(cookie["mall_order_refund_csrf"]["httponly"])
        self.assertEqual(cookie["mall_order_refund_csrf"]["samesite"].lower(), "strict")
        return token

    def test_only_completed_order_shows_refund_form(self):
        self.assertNotIn("确认整单退款".encode(), self._detail().body)
        self.fixture.fulfill(self.placed.order_public_id)
        self.assertNotIn("确认整单退款".encode(), self._detail().body)
        self.fixture.ship(self.placed.order_public_id)
        self.assertNotIn("确认整单退款".encode(), self._detail().body)
        self.fixture.complete(self.placed.order_public_id)
        self.assertIn("确认整单退款".encode(), self._detail().body)

    def test_refund_and_replay_have_one_audit_and_no_second_resources(self):
        token = self._completed_token()
        cookie = f"mall_order_refund_csrf={token}"
        with self.fixture.Session() as db:
            before_points = db.query(PointsAccount).first().available_points
            before_stock = tuple(row.on_hand_quantity for row in db.query(InventoryBalance).order_by(InventoryBalance.id))
        first = self._submit(token=token, cookie=cookie)
        self.assertIn("message", parse_qs(urlparse(first.headers["location"]).query))
        with self.fixture.Session() as db:
            after_points = db.query(PointsAccount).first().available_points
            after_stock = tuple(row.on_hand_quantity for row in db.query(InventoryBalance).order_by(InventoryBalance.id))
        replay = self._submit(token=token, cookie=cookie)
        self.assertIn("message", parse_qs(urlparse(replay.headers["location"]).query))
        with self.fixture.Session() as db:
            order = db.get(Order, self.placed.order_id)
            logs = db.query(AdminActionLog).filter_by(
                target_type="mall_order", target_id=order.id,
                action_type="mall_order_refund",
            ).all()
            self.assertEqual(order.status, "REFUNDED")
            self.assertEqual(order.refund_reason, "客户确认整单退货")
            self.assertEqual(len(logs), 1)
            self.assertEqual(db.query(PointsAccount).first().available_points, after_points)
            self.assertEqual(tuple(row.on_hand_quantity for row in db.query(InventoryBalance).order_by(InventoryBalance.id)), after_stock)
        self.assertGreater(after_points, before_points)
        self.assertNotEqual(after_stock, before_stock)
        self.assertNotIn("确认整单退款".encode(), self._detail().body)

    def test_token_reason_state_and_replay_conflict_fail_closed(self):
        token = "a" * 40
        for posted, cookie in (("", ""), (token, ""), ("other", f"mall_order_refund_csrf={token}")):
            result = self._submit(token=posted, cookie=cookie)
            self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))
        valid_cookie = f"mall_order_refund_csrf={token}"
        self.assertIn("error", parse_qs(urlparse(self._submit(token=token, cookie=valid_cookie).headers["location"]).query))
        token = self._completed_token()
        valid_cookie = f"mall_order_refund_csrf={token}"
        for reason, public_id in ((" ", None), ("x" * 501, None), ("ok", "ORD-NOT-FOUND")):
            response = self._submit(token=token, cookie=valid_cookie, reason=reason, order_public_id=public_id)
            self.assertIn("error", parse_qs(urlparse(response.headers["location"]).query))
        self._submit(token=token, cookie=valid_cookie)
        conflict = self._submit(token=token, cookie=valid_cookie, reason="其他原因")
        self.assertIn("error", parse_qs(urlparse(conflict.headers["location"]).query))

    def test_unauthorized_users_rejected_before_service(self):
        reviewer = SimpleNamespace(id=22, role="admin", admin_level=PRIMARY_REVIEWER, is_active=True)
        inactive = SimpleNamespace(id=23, role="admin", admin_level=SUPER_ADMIN, is_active=False)
        partner = SimpleNamespace(id=24, role="partner", admin_level=OPERATOR, is_active=True)
        for user, destination in ((reviewer, "/dashboard"), (inactive, "/dashboard"), (partner, "/dashboard"), (None, "/login")):
            with (
                patch("app.main.get_current_user", return_value=user),
                patch("app.main.execute_order_refund") as service,
            ):
                response = refund_mall_order_route(request(self.path + "/refund", method="POST"), self.placed.order_public_id, "", "")
                self.assertEqual(response.headers["location"], destination)
                service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
