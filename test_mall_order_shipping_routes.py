"""商城订单后台手工发货入口：权限、令牌与原子证据。"""

import unittest
from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import mall_order_detail_page, ship_mall_order_route
from app.models import AdminActionLog, Order
import test_order_service


def request(path, *, method="GET", cookie=""):
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "root_path": "",
        "query_string": b"",
        "headers": [(b"cookie", cookie.encode())] if cookie else [],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


class MallOrderShippingRouteTests(unittest.TestCase):
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
            return mall_order_detail_page(
                request(self.path), self.placed.order_public_id,
            )

    def _submit(self, *, token, cookie, carrier="顺丰速运",
                tracking="SF-M6-10-0001", user=None, order_public_id=None):
        public_id = order_public_id or self.placed.order_public_id
        with (
            patch("app.main.get_current_user", return_value=(
                self.fixture.operator if user is None else user
            )),
            patch("app.main.engine", self.fixture.engine),
            patch("app.mall.order_lifecycle_service.utc8_now", return_value=(
                test_order_service.NOW + timedelta(minutes=1)
            )),
        ):
            return ship_mall_order_route(
                request(f"/mall-orders/{public_id}/ship", method="POST", cookie=cookie),
                public_id, carrier, tracking, token,
            )

    def _form_token(self):
        response = self._detail()
        self.assertEqual(response.status_code, 200)
        self.assertIn("录入物流并发货".encode(), response.body)
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        token = cookie["mall_order_ship_csrf"].value
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(cookie["mall_order_ship_csrf"]["httponly"])
        self.assertEqual(cookie["mall_order_ship_csrf"]["samesite"].lower(), "strict")
        return token

    def test_only_fulfilling_order_shows_shipping_form(self):
        created = self._detail()
        self.assertNotIn("录入物流并发货".encode(), created.body)
        self.assertNotIn("mall_order_ship_csrf", created.headers.get("set-cookie", ""))
        self.fixture.fulfill(self.placed.order_public_id)
        token = self._form_token()
        self.assertTrue(token)

    def test_real_shipping_and_replay_create_one_audit(self):
        self.fixture.fulfill(self.placed.order_public_id)
        token = self._form_token()
        cookie = f"mall_order_ship_csrf={token}"
        first = self._submit(token=token, cookie=cookie)
        self.assertEqual(first.status_code, 303)
        self.assertIn("message", parse_qs(urlparse(first.headers["location"]).query))
        second = self._submit(token=token, cookie=cookie)
        self.assertEqual(second.status_code, 303)
        with self.fixture.Session() as db:
            order = db.get(Order, self.placed.order_id)
            logs = db.query(AdminActionLog).filter_by(
                target_type="mall_order", target_id=order.id,
                action_type="mall_order_ship",
            ).all()
            self.assertEqual(order.status, "SHIPPED")
            self.assertEqual(order.shipping_carrier, "顺丰速运")
            self.assertEqual(order.tracking_number, "SF-M6-10-0001")
            self.assertEqual(len(logs), 1)
        shipped = self._detail()
        self.assertNotIn("录入物流并发货".encode(), shipped.body)
        self.assertNotIn("mall_order_ship_csrf", shipped.headers.get("set-cookie", ""))

    def test_invalid_token_or_fields_do_not_ship(self):
        self.fixture.fulfill(self.placed.order_public_id)
        token = self._form_token()
        for posted, cookie in (
            ("", ""), (token, ""),
            ("incorrect", f"mall_order_ship_csrf={token}"),
            (token, "mall_order_fulfill_csrf=" + token),
        ):
            result = self._submit(token=posted, cookie=cookie)
            self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))
        result = self._submit(
            token=token, cookie=f"mall_order_ship_csrf={token}", carrier="  ",
        )
        self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))
        with self.fixture.Session() as db:
            self.assertEqual(db.get(Order, self.placed.order_id).status, "FULFILLING")

    def test_unauthorized_accounts_rejected_before_service(self):
        reviewer = SimpleNamespace(
            id=22, username="reviewer", role="admin",
            admin_level=PRIMARY_REVIEWER, is_active=True,
        )
        inactive = SimpleNamespace(
            id=23, username="inactive", role="admin",
            admin_level=SUPER_ADMIN, is_active=False,
        )
        partner = SimpleNamespace(
            id=24, username="partner", role="partner",
            admin_level=OPERATOR, is_active=True,
        )
        for user, destination in (
            (reviewer, "/dashboard"), (inactive, "/dashboard"),
            (partner, "/dashboard"), (None, "/login"),
        ):
            with (
                patch("app.main.get_current_user", return_value=user),
                patch("app.main.execute_order_shipping") as service,
            ):
                result = ship_mall_order_route(
                    request(self.path + "/ship", method="POST"),
                    self.placed.order_public_id, "顺丰速运", "SF-1", "",
                )
                self.assertEqual(result.headers["location"], destination)
                service.assert_not_called()

    def test_created_and_missing_orders_fail_closed(self):
        token = "a" * 40
        cookie = f"mall_order_ship_csrf={token}"
        for public_id in (self.placed.order_public_id, "ORD-NOT-FOUND"):
            result = self._submit(
                token=token, cookie=cookie, order_public_id=public_id,
            )
            self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))
        with self.fixture.Session() as db:
            self.assertEqual(db.get(Order, self.placed.order_id).status, "CREATED")


if __name__ == "__main__":
    unittest.main()
