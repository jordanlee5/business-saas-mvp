"""商城订单后台确认完成：状态、权限、令牌和原子证据。"""

import unittest
from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import complete_mall_order_route, mall_order_detail_page
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


class MallOrderCompletionRouteTests(unittest.TestCase):
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

    def _submit(self, *, token, cookie, user=None, order_public_id=None):
        public_id = order_public_id or self.placed.order_public_id
        with (
            patch("app.main.get_current_user", return_value=(
                self.fixture.operator if user is None else user
            )),
            patch("app.main.engine", self.fixture.engine),
            patch("app.mall.order_lifecycle_service.utc8_now", return_value=(
                test_order_service.NOW + timedelta(minutes=2)
            )),
        ):
            return complete_mall_order_route(
                request(f"/mall-orders/{public_id}/complete", method="POST", cookie=cookie),
                public_id, token,
            )

    def _form_token(self):
        response = self._detail()
        self.assertEqual(response.status_code, 200)
        self.assertIn("确认订单完成".encode(), response.body)
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        token = cookie["mall_order_complete_csrf"].value
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(cookie["mall_order_complete_csrf"]["httponly"])
        self.assertEqual(cookie["mall_order_complete_csrf"]["samesite"].lower(), "strict")
        return token

    def test_only_shipped_order_shows_completion_form(self):
        self.assertNotIn("确认订单完成".encode(), self._detail().body)
        self.fixture.fulfill(self.placed.order_public_id)
        self.assertNotIn("确认订单完成".encode(), self._detail().body)
        self.fixture.ship(self.placed.order_public_id)
        self._form_token()

    def test_completion_and_replay_create_one_audit(self):
        self.fixture.fulfill(self.placed.order_public_id)
        self.fixture.ship(self.placed.order_public_id)
        token = self._form_token()
        cookie = f"mall_order_complete_csrf={token}"
        for _ in range(2):
            response = self._submit(token=token, cookie=cookie)
            self.assertEqual(response.status_code, 303)
            self.assertIn("message", parse_qs(urlparse(response.headers["location"]).query))
        with self.fixture.Session() as db:
            order = db.get(Order, self.placed.order_id)
            logs = db.query(AdminActionLog).filter_by(
                target_type="mall_order", target_id=order.id,
                action_type="mall_order_complete",
            ).all()
            self.assertEqual(order.status, "COMPLETED")
            self.assertEqual(len(logs), 1)
            self.assertEqual(order.completed_at, logs[0].created_at)
        detail = self._detail()
        self.assertNotIn("确认订单完成".encode(), detail.body)
        self.assertNotIn("mall_order_complete_csrf", detail.headers.get("set-cookie", ""))

    def test_missing_token_and_wrong_state_fail_closed(self):
        token = "a" * 40
        for posted, cookie in (
            ("", ""), (token, ""),
            ("different", f"mall_order_complete_csrf={token}"),
            (token, f"mall_order_ship_csrf={token}"),
        ):
            response = self._submit(token=posted, cookie=cookie)
            self.assertIn("error", parse_qs(urlparse(response.headers["location"]).query))
        self.fixture.fulfill(self.placed.order_public_id)
        for public_id in (self.placed.order_public_id, "ORD-NOT-FOUND"):
            response = self._submit(
                token=token, cookie=f"mall_order_complete_csrf={token}",
                order_public_id=public_id,
            )
            self.assertIn("error", parse_qs(urlparse(response.headers["location"]).query))
        with self.fixture.Session() as db:
            self.assertEqual(db.get(Order, self.placed.order_id).status, "FULFILLING")

    def test_unauthorized_users_rejected_before_service(self):
        reviewer = SimpleNamespace(id=22, role="admin", admin_level=PRIMARY_REVIEWER, is_active=True)
        inactive = SimpleNamespace(id=23, role="admin", admin_level=SUPER_ADMIN, is_active=False)
        partner = SimpleNamespace(id=24, role="partner", admin_level=OPERATOR, is_active=True)
        for user, destination in (
            (reviewer, "/dashboard"), (inactive, "/dashboard"),
            (partner, "/dashboard"), (None, "/login"),
        ):
            with (
                patch("app.main.get_current_user", return_value=user),
                patch("app.main.execute_order_completion") as service,
            ):
                response = complete_mall_order_route(
                    request(self.path + "/complete", method="POST"),
                    self.placed.order_public_id, "",
                )
                self.assertEqual(response.headers["location"], destination)
                service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
