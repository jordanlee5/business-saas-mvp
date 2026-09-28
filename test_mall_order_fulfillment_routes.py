"""商城订单后台确认履约：权限、表单令牌与原子服务集成。"""

import unittest
from datetime import datetime
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import fulfill_mall_order_route, mall_order_detail_page
from app.models import AdminActionLog, Order, PointsAccount
import test_order_service


def request(path, *, method="GET", cookie=""):
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "root_path": "",
        "query_string": b"",
        "headers": [(b"cookie", cookie.encode())] if cookie else [],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


class MallOrderFulfillmentRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_order_service.OrderServiceTests(
            "test_places_order_with_snapshots_fefo_points_and_stock"
        )
        self.fixture.setUp()
        self.placed = self.fixture.place()
        self.path = f"/mall-orders/{self.placed.order_public_id}"

    def tearDown(self):
        self.fixture.tearDown()

    def _form(self):
        with (
            patch("app.main.get_current_user", return_value=self.fixture.operator),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            response = mall_order_detail_page(request(self.path), self.placed.order_public_id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("确认订单履约".encode(), response.body)
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        token = cookie["mall_order_fulfill_csrf"].value
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(cookie["mall_order_fulfill_csrf"]["httponly"])
        return token

    def _submit(self, *, token, cookie, user=None, order_public_id=None):
        order_public_id = order_public_id or self.placed.order_public_id
        path = f"/mall-orders/{order_public_id}/fulfill"
        with (
            patch("app.main.get_current_user", return_value=(
                self.fixture.operator if user is None else user
            )),
            patch("app.main.engine", self.fixture.engine),
            patch("app.mall.order_fulfillment_service.utc8_now", return_value=test_order_service.NOW),
        ):
            return fulfill_mall_order_route(
                request(path, method="POST", cookie=cookie),
                order_public_id, token,
            )

    def test_real_order_fulfillment_and_replay_only_create_one_audit(self):
        token = self._form()
        cookie = f"mall_order_fulfill_csrf={token}"
        first = self._submit(token=token, cookie=cookie)
        self.assertEqual(first.status_code, 303)
        self.assertIn("message", parse_qs(urlparse(first.headers["location"]).query))
        second = self._submit(token=token, cookie=cookie)
        self.assertEqual(second.status_code, 303)
        with self.fixture.Session() as db:
            order = db.query(Order).filter_by(id=self.placed.order_id).one()
            account = db.query(PointsAccount).filter_by(member_id=self.fixture.member.id).one()
            logs = db.query(AdminActionLog).filter_by(
                target_type="mall_order", target_id=order.id,
                action_type="mall_order_fulfill",
            ).all()
            self.assertEqual(order.status, "FULFILLING")
            self.assertEqual(str(account.reserved_points), "0.00")
            self.assertEqual(len(logs), 1)
        with (
            patch("app.main.get_current_user", return_value=self.fixture.operator),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            detail = mall_order_detail_page(request(self.path), self.placed.order_public_id)
        self.assertNotIn("确认订单履约".encode(), detail.body)
        self.assertNotIn("mall_order_fulfill_csrf", detail.headers.get("set-cookie", ""))

    def test_missing_and_mismatched_form_tokens_cannot_mutate(self):
        token = self._form()
        for posted, cookie in (
            ("", ""), (token, ""),
            (token, "mall_order_fulfill_csrf=other-token"),
            ("incorrect", f"mall_order_fulfill_csrf={token}"),
        ):
            response = self._submit(token=posted, cookie=cookie)
            self.assertEqual(response.status_code, 303)
            self.assertIn("error", parse_qs(urlparse(response.headers["location"]).query))
        with self.fixture.Session() as db:
            self.assertEqual(db.get(Order, self.placed.order_id).status, "CREATED")

    def test_role_and_inactive_guards_run_before_service(self):
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
        for user, expected in (
            (reviewer, "/dashboard"), (inactive, "/dashboard"),
            (partner, "/dashboard"),
        ):
            with (
                patch("app.main.get_current_user", return_value=user),
                patch("app.main.execute_order_fulfillment") as service,
            ):
                response = fulfill_mall_order_route(
                    request(self.path + "/fulfill", method="POST"),
                    self.placed.order_public_id, "",
                )
                self.assertEqual(response.headers["location"], expected)
                service.assert_not_called()
        with (
            patch("app.main.get_current_user", return_value=None),
            patch("app.main.execute_order_fulfillment") as service,
        ):
            response = fulfill_mall_order_route(
                request(self.path + "/fulfill", method="POST"),
                self.placed.order_public_id, "",
            )
            self.assertEqual(response.headers["location"], "/login")
            service.assert_not_called()

    def test_missing_order_and_cancelled_order_fail_closed(self):
        token = self._form()
        cookie = f"mall_order_fulfill_csrf={token}"
        missing = self._submit(
            token=token, cookie=cookie, order_public_id="ORD-NOT-FOUND",
        )
        self.assertIn("error", parse_qs(urlparse(missing.headers["location"]).query))
        self.fixture.cancel(self.placed.order_public_id)
        cancelled = self._submit(token=token, cookie=cookie)
        self.assertIn("error", parse_qs(urlparse(cancelled.headers["location"]).query))
        with self.fixture.Session() as db:
            self.assertEqual(db.get(Order, self.placed.order_id).status, "CANCELLED")


if __name__ == "__main__":
    unittest.main()
