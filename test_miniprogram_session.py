"""Offline tests for bound-member sessions; never contact WeChat."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.miniprogram_v1 import (
    WechatLoginRequest, create_member_session, get_current_member,
)
from app.database import Base
from app.mall.wechat_identity_service import WechatIdentity, WechatCodeError
from app.mall.miniprogram_session_service import (
    InvalidSession, SessionConfigurationError, SESSION_TTL_SECONDS,
    issue_session, verify_session,
)
from app.models import Member, MemberWechatBinding


APP_ID = "wx1234567890abcdef"
SIGNING_KEY = "test-only-32-byte-session-signing-key-2026"


class MiniprogramSessionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "WECHAT_MINIPROGRAM_APP_ID": APP_ID,
            "MINIPROGRAM_SESSION_SIGNING_KEY": SIGNING_KEY,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "sessions.db"))
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.member = Member(member_public_id="MEMBER-SESSION-001", is_active=True)
        self.db.add(self.member)
        self.db.flush()
        self.binding = MemberWechatBinding(member_id=self.member.id,
                                           wechat_app_id=APP_ID,
                                           openid="openid-never-in-token")
        self.db.add(self.binding)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _login(self, openid="openid-never-in-token"):
        with patch("app.api.miniprogram_v1.exchange_wechat_code",
                   return_value=WechatIdentity(APP_ID, openid)):
            return create_member_session(WechatLoginRequest(code="fake-code"),
                                         Response(), self.db)

    def test_bound_member_login_and_private_identity(self):
        result = self._login()
        self.assertEqual(result.token_type, "Bearer")
        self.assertEqual(result.expires_in, SESSION_TTL_SECONDS)
        self.assertNotIn("openid-never-in-token", result.token)
        current = get_current_member(Response(), "Bearer " + result.token, self.db)
        self.assertEqual(current.member_public_id, "MEMBER-SESSION-001")

    def test_unbound_or_inactive_member_cannot_get_token(self):
        with self.assertRaises(HTTPException) as unbound:
            self._login("other-openid")
        self.assertEqual(unbound.exception.status_code, 403)
        self.member.is_active = False
        self.db.commit()
        with self.assertRaises(HTTPException) as inactive:
            self._login()
        self.assertEqual(inactive.exception.status_code, 403)

    def test_revoking_member_or_binding_blocks_existing_token(self):
        token = self._login().token
        self.member.is_active = False
        self.db.commit()
        with self.assertRaises(HTTPException) as inactive:
            get_current_member(Response(), "Bearer " + token, self.db)
        self.assertEqual(inactive.exception.status_code, 401)
        self.member.is_active = True
        self.db.delete(self.binding)
        self.db.commit()
        with self.assertRaises(HTTPException) as missing:
            get_current_member(Response(), "Bearer " + token, self.db)
        self.assertEqual(missing.exception.status_code, 401)

    def test_changed_openid_on_same_binding_id_invalidates_token(self):
        token = self._login().token
        self.binding.openid = "different-openid"
        self.db.commit()
        with self.assertRaises(HTTPException) as changed:
            get_current_member(Response(), "Bearer " + token, self.db)
        self.assertEqual(changed.exception.status_code, 401)

    def test_invalid_bearer_or_tampered_token_is_rejected(self):
        token = self._login().token
        for header in (None, "Basic " + token, "Bearer " + token + "x"):
            with self.subTest(header=header), self.assertRaises(HTTPException) as rejected:
                get_current_member(Response(), header, self.db)
            self.assertEqual(rejected.exception.status_code, 401)

    def test_token_expires_and_future_token_is_rejected(self):
        token, ttl = issue_session(self.binding.id, APP_ID, self.binding.openid,
                                   now=1_000_000)
        self.assertEqual(verify_session(token, now=1_000_000).binding_id, self.binding.id)
        with self.assertRaises(InvalidSession):
            verify_session(token, now=1_000_000 + ttl)
        with self.assertRaises(InvalidSession):
            verify_session(token, now=999_969)

    def test_secret_rotation_or_app_change_rejects_session(self):
        token = self._login().token
        with patch.dict(os.environ, {"MINIPROGRAM_SESSION_SIGNING_KEY": "other-32-byte-signing-key-2026-safe"}):
            with self.assertRaises(InvalidSession):
                verify_session(token)
        with patch.dict(os.environ, {"WECHAT_MINIPROGRAM_APP_ID": "wxaaaaaaaaaaaaaaaa"}):
            with self.assertRaises(HTTPException) as wrong_app:
                get_current_member(Response(), "Bearer " + token, self.db)
        self.assertEqual(wrong_app.exception.status_code, 401)

    def test_missing_signing_key_fails_closed(self):
        with patch.dict(os.environ, {"MINIPROGRAM_SESSION_SIGNING_KEY": ""}):
            with self.assertRaises(SessionConfigurationError):
                issue_session(self.binding.id, APP_ID, self.binding.openid)
            with patch("app.api.miniprogram_v1.exchange_wechat_code") as exchange:
                with self.assertRaises(HTTPException) as unavailable:
                    create_member_session(WechatLoginRequest(code="fake-code"),
                                          Response(), self.db)
                exchange.assert_not_called()
            self.assertEqual(unavailable.exception.status_code, 503)

    def test_wechat_code_rejection_is_redacted(self):
        with patch("app.api.miniprogram_v1.exchange_wechat_code", side_effect=WechatCodeError()):
            with self.assertRaises(HTTPException) as rejected:
                create_member_session(WechatLoginRequest(code="bad"), Response(), self.db)
        self.assertEqual(rejected.exception.status_code, 401)
        self.assertNotIn("openid", str(rejected.exception.detail))

    def test_server_does_not_use_legacy_admin_cookie_for_identity(self):
        token = self._login().token
        with self.assertRaises(HTTPException) as missing:
            get_current_member(Response(), None, self.db)
        self.assertEqual(missing.exception.status_code, 401)
        self.assertEqual(verify_session(token, now=int(time.time())).app_id, APP_ID)


if __name__ == "__main__":
    unittest.main()
