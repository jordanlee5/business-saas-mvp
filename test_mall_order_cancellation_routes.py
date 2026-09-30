"""管理员取消待处理订单：页面权限、原子释放与幂等审计。"""

import unittest
from datetime import timedelta
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from app.admin_permissions import PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import cancel_mall_order_route, mall_order_detail_page
from app.models import AdminActionLog, InventoryBalance, InventoryMovement, Order, PointsAccount, PointsGrant, PointsLedgerEntry
import test_order_service


def request(path, *, method="GET", cookie=""):
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "root_path": "", "query_string": b"",
        "headers": [(b"cookie", cookie.encode())] if cookie else [],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


class MallOrderCancellationRouteTests(unittest.TestCase):
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

    def _token(self):
        response = self._detail()
        self.assertIn("确认取消订单".encode(), response.body)
        cookie = SimpleCookie()
        for header in response.headers.getlist("set-cookie"):
            cookie.load(header)
        token = cookie["mall_order_cancel_csrf"].value
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(cookie["mall_order_cancel_csrf"]["httponly"])
        self.assertEqual(cookie["mall_order_cancel_csrf"]["samesite"].lower(), "strict")
        return token

    def _submit(self, token, cookie, *, reason="客户申请取消", order_public_id=None):
        public_id = order_public_id or self.placed.order_public_id
        with (
            patch("app.main.get_current_user", return_value=self.fixture.operator),
            patch("app.main.engine", self.fixture.engine),
            patch("app.mall.order_cancellation_service.utc8_now", return_value=(
                test_order_service.NOW + timedelta(minutes=3)
            )),
        ):
            return cancel_mall_order_route(
                request(f"/mall-orders/{public_id}/cancel", method="POST", cookie=cookie),
                public_id, reason, token,
            )

    def test_cancel_releases_once_and_records_operator(self):
        token = self._token()
        cookie = f"mall_order_cancel_csrf={token}"
        with self.fixture.Session() as db:
            before_points = db.query(PointsAccount).first().reserved_points
            before_stock = tuple(row.reserved_quantity for row in db.query(InventoryBalance).order_by(InventoryBalance.id))
        first = self._submit(token, cookie)
        self.assertIn("message", parse_qs(urlparse(first.headers["location"]).query))
        replay = self._submit(token, cookie)
        self.assertIn("message", parse_qs(urlparse(replay.headers["location"]).query))
        with self.fixture.Session() as db:
            order = db.get(Order, self.placed.order_id)
            logs = db.query(AdminActionLog).filter_by(action_type="mall_order_cancel", target_id=order.id).all()
            releases = db.query(PointsLedgerEntry).filter_by(reference_id=order.order_public_id, entry_type="RELEASE").all()
            movements = db.query(InventoryMovement).filter_by(reference_id=order.order_public_id, movement_type="RELEASE").all()
            self.assertEqual(order.status, "CANCELLED")
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].admin_id, self.fixture.operator.id)
            self.assertIn("客户申请取消", logs[0].description)
            self.assertTrue(all(entry.actor_admin_id == self.fixture.operator.id for entry in releases))
            self.assertTrue(all(movement.actor_admin_id == self.fixture.operator.id and movement.actor_member_id is None for movement in movements))
            self.assertEqual(db.query(PointsAccount).first().reserved_points, 0)
            self.assertTrue(all(row.reserved_quantity == 0 for row in db.query(InventoryBalance)))
            self.assertEqual(len(releases), 2)
            self.assertEqual(len(movements), 2)
        self.assertGreater(before_points, 0)
        self.assertTrue(all(value > 0 for value in before_stock))
        self.assertNotIn("确认取消订单".encode(), self._detail().body)

    def test_invalid_token_reason_or_state_does_not_write(self):
        token = self._token()
        for posted, cookie in (("", ""), (token, ""), ("other", f"mall_order_cancel_csrf={token}")):
            result = self._submit(posted, cookie)
            self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))
        cookie = f"mall_order_cancel_csrf={token}"
        for reason, public_id in ((" ", None), ("x" * 501, None), ("ok", "ORD-NOT-FOUND")):
            result = self._submit(token, cookie, reason=reason, order_public_id=public_id)
            self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))
        self.fixture.fulfill(self.placed.order_public_id)
        self.assertNotIn("确认取消订单".encode(), self._detail().body)
        result = self._submit(token, cookie)
        self.assertIn("error", parse_qs(urlparse(result.headers["location"]).query))

    def test_conflicting_replay_or_member_replay_fails_closed(self):
        token = self._token()
        cookie = f"mall_order_cancel_csrf={token}"
        self._submit(token, cookie)
        conflict = self._submit(token, cookie, reason="其他原因")
        self.assertIn("error", parse_qs(urlparse(conflict.headers["location"]).query))
        with self.assertRaises(ValueError):
            self.fixture.cancel(self.placed.order_public_id)

    def test_failure_after_inventory_release_rolls_back_all_evidence(self):
        token = self._token()
        with self.fixture.Session() as db:
            grant = db.get(PointsGrant, self.fixture.early_grant.id)
            grant.status = "EXPIRED"
            db.commit()
        response = self._submit(token, f"mall_order_cancel_csrf={token}")
        self.assertIn("error", parse_qs(urlparse(response.headers["location"]).query))
        with self.fixture.Session() as db:
            self.assertEqual(db.get(Order, self.placed.order_id).status, "CREATED")
            self.assertEqual(db.query(InventoryMovement).filter_by(movement_type="RELEASE").count(), 0)
            self.assertEqual(db.query(PointsLedgerEntry).filter_by(entry_type="RELEASE").count(), 0)
            self.assertEqual(db.query(AdminActionLog).filter_by(action_type="mall_order_cancel").count(), 0)

    def test_member_cancellation_remains_available_without_admin_audit(self):
        self.fixture.cancel(self.placed.order_public_id)
        with self.fixture.Session() as db:
            releases = db.query(InventoryMovement).filter_by(movement_type="RELEASE").all()
            self.assertTrue(all(row.actor_member_id == self.fixture.member.id and row.actor_admin_id is None for row in releases))
            self.assertEqual(db.query(AdminActionLog).filter_by(action_type="mall_order_cancel").count(), 0)
        token = "a" * 40
        response = self._submit(token, f"mall_order_cancel_csrf={token}")
        self.assertIn("error", parse_qs(urlparse(response.headers["location"]).query))

    def test_unauthorized_rejected_before_service(self):
        reviewer = SimpleNamespace(id=22, role="admin", admin_level=PRIMARY_REVIEWER, is_active=True)
        inactive = SimpleNamespace(id=23, role="admin", admin_level=SUPER_ADMIN, is_active=False)
        partner = SimpleNamespace(id=24, role="partner", admin_level=SUPER_ADMIN, is_active=True)
        for user, destination in ((reviewer, "/dashboard"), (inactive, "/dashboard"), (partner, "/dashboard"), (None, "/login")):
            with (
                patch("app.main.get_current_user", return_value=user),
                patch("app.main.execute_order_cancellation") as service,
            ):
                response = cancel_mall_order_route(request(self.path + "/cancel", method="POST"), self.placed.order_public_id, "", "")
                self.assertEqual(response.headers["location"], destination)
                service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
