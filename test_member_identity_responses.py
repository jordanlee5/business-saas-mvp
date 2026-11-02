"""Exercise validation and authentication errors through the actual HTTP routes."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import miniprogram_v1_router
from app.api.miniprogram_v1 import public_catalog_session
from app.mall.wechat_identity_service import WechatCodeError


class IdentityResponseTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(miniprogram_v1_router)
        app.dependency_overrides[public_catalog_session] = lambda: None
        self.client = TestClient(app, base_url="http://testserver/api/miniprogram/v1/")
        self.addCleanup(self.client.close)

    def test_incomplete_activation_never_reflects_submitted_fields(self):
        body = dict(phone="private-phone", plate_number="private-plate",
                    activation_code="private-credential", wechat_code="private-login")
        with patch("app.api.miniprogram_v1.exchange_wechat_code") as exchange:
            response = self.client.post("activation/redeem", json=body)
            exchange.assert_not_called()
        self.assertEqual(response.status_code, 422)
        self.assertTrue(all(value not in response.text for value in body.values()))
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_invalid_login_shape_never_reflects_provided_code(self):
        response = self.client.post("auth/wechat-session", json={"code": ["private-login"]})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-login", response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_identity_rejections_preserve_status_and_prevent_caching(self):
        with patch("app.api.miniprogram_v1.assert_session_configured"), patch(
            "app.api.miniprogram_v1.exchange_wechat_code", side_effect=WechatCodeError()
        ):
            response = self.client.post("auth/wechat-session", json={"code": "unused"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        me = self.client.get("auth/me")
        self.assertEqual(me.status_code, 401)
        self.assertEqual(me.headers.get("cache-control"), "no-store")
