"""Offline contract tests; no real WeChat credential or HTTP call needed."""

import json
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from app.mall.wechat_identity_service import (
    WechatCodeError,
    WechatConfigurationError,
    WechatExchangeUnavailable,
    exchange_wechat_code,
)


APP_ID = "wx1234567890abcdef"
APP_SECRET = "server-only-test-secret"


class WechatIdentityServiceTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "WECHAT_MINIPROGRAM_APP_ID": APP_ID,
            "WECHAT_MINIPROGRAM_APP_SECRET": APP_SECRET,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_success_exchanges_fixed_endpoint_and_discards_session_key(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return json.dumps({"openid": "openid-a", "session_key": "secret-key",
                               "unionid": "unionid-a"}).encode()

        identity = exchange_wechat_code("code+/123", fetch=fetch)
        parsed = urlsplit(seen[0])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "api.weixin.qq.com", "/sns/jscode2session"))
        self.assertEqual(parse_qs(parsed.query), {
            "appid": [APP_ID], "secret": [APP_SECRET],
            "js_code": ["code+/123"], "grant_type": ["authorization_code"],
        })
        self.assertEqual((identity.app_id, identity.openid, identity.unionid),
                         (APP_ID, "openid-a", "unionid-a"))
        self.assertNotIn("secret-key", repr(identity))
        self.assertFalse(hasattr(identity, "session_key"))

    def test_missing_config_and_invalid_code_make_no_request(self):
        calls = []
        with patch.dict(os.environ, {"WECHAT_MINIPROGRAM_APP_SECRET": ""}):
            with self.assertRaises(WechatConfigurationError):
                exchange_wechat_code("validCode", fetch=calls.append)
        for code in ("", " white", "abc?x=1", "a" * 513, None):
            with self.subTest(code=code), self.assertRaises(WechatCodeError):
                exchange_wechat_code(code, fetch=calls.append)
        self.assertEqual(calls, [])

    def test_provider_errors_are_redacted(self):
        cases = [
            (b'{"errcode":40029,"errmsg":"invalid code secret"}', WechatCodeError),
            (b'{"errcode":"40029"}', WechatExchangeUnavailable),
            (b'{"openid":"id","session_key":""}', WechatExchangeUnavailable),
            (b'{"openid":"id","session_key":"key","unionid":123}', WechatExchangeUnavailable),
            (b'not json', WechatExchangeUnavailable),
            (b'x' * 8193, WechatExchangeUnavailable),
            (b'[]', WechatExchangeUnavailable),
        ]
        for payload, error_type in cases:
            with self.subTest(payload=payload[:20]), self.assertRaises(error_type) as caught:
                exchange_wechat_code("code", fetch=lambda url: payload)
            self.assertNotIn("secret", str(caught.exception))

    def test_transport_errors_are_redacted(self):
        def unavailable(url):
            raise URLError("secret must not escape")

        with self.assertRaises(WechatExchangeUnavailable) as caught:
            exchange_wechat_code("code", fetch=unavailable)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("secret", str(caught.exception))

    def test_redirects_are_disabled_and_timeout_is_bounded(self):
        from app.mall.wechat_identity_service import _RejectRedirect, _TIMEOUT_SECONDS
        self.assertIsNone(_RejectRedirect().redirect_request(None, None, 302, "", {},
                                                              "https://example.test"))
        self.assertLessEqual(_TIMEOUT_SECONDS, 5)


if __name__ == "__main__":
    unittest.main()
