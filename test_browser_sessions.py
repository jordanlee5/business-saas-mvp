import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.admin_permissions import OPERATOR
from app.auth import get_password_hash
from app.browser_sessions import BrowserSessions, COOKIE_NAME, LIFETIME
from app.database import Base
from app.models import User


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.database = sessionmaker(bind=engine)
        self.password = "test-password-only"
        with self.database() as db:
            user = User(username="operator", password_hash=get_password_hash(self.password),
                        role="admin", admin_level=OPERATOR, is_active=True)
            db.add(user)
            db.commit()
            self.user_id = user.id
        self.sessions = BrowserSessions()
        for name, value in (("SessionLocal", self.database), ("engine", engine),
                            ("browser_sessions", self.sessions)):
            change = patch.object(main, name, value)
            change.start()
            self.addCleanup(change.stop)
        self.client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
        self.addCleanup(self.client.close)

    def login(self):
        return self.client.post("/login", data={"username": "operator", "password": self.password})

    def assert_blocked(self, cookie):
        response = self.client.get("/mall-orders", headers={"Cookie": cookie})
        self.assertEqual((response.status_code, response.headers.get("location")), (302, "/login"))

    def test_legacy_id_and_fabricated_or_modified_token_denied(self):
        self.assert_blocked(f"user_id={self.user_id}")
        self.assert_blocked(f"{COOKIE_NAME}={'0' * 64}; user_id={self.user_id}")
        token = self.login().cookies[COOKIE_NAME]
        changed = ("0" if token[0] != "0" else "1") + token[1:]
        self.assert_blocked(f"{COOKIE_NAME}={changed}; user_id={self.user_id}")
        response = self.client.post("/mall-orders/ORD-0000000000000001/fulfill",
                                    headers={"Cookie": f"user_id={self.user_id}"})
        self.assertEqual(response.headers.get("location"), "/login")

    def test_success_rotation_and_logout_invalidate_previous_tokens(self):
        self.assertEqual(self.client.get("/login").status_code, 200)
        bad = self.client.post("/login", data={"username": "operator", "password": "incorrect"})
        self.assertNotIn(COOKIE_NAME, bad.cookies)
        first = self.login()
        first_token = first.cookies[COOKIE_NAME]
        cookie = next(x for x in first.headers.get_list("set-cookie") if x.startswith(COOKIE_NAME + "="))
        for flag in ("httponly", "secure", "samesite=lax", "max-age=28800"):
            self.assertIn(flag, cookie.lower())
        self.assertEqual(self.client.get("/mall-orders").status_code, 200)
        next_token = self.login().cookies[COOKIE_NAME]
        self.assertNotEqual(next_token, first_token)
        self.assert_blocked(f"{COOKIE_NAME}={first_token}")
        self.client.get("/logout", headers={"Cookie": f"{COOKIE_NAME}={next_token}"})
        self.assert_blocked(f"{COOKIE_NAME}={next_token}")

    def test_active_role_and_password_rechecked_against_database(self):
        token = self.login().cookies[COOKIE_NAME]
        with self.database() as db:
            db.get(User, self.user_id).role = "partner"
            db.commit()
        self.assertEqual(self.client.get("/mall-orders").headers.get("location"), "/dashboard")
        with self.database() as db:
            db.get(User, self.user_id).password_hash = get_password_hash("reset-password-only")
            db.commit()
        self.assert_blocked(f"{COOKIE_NAME}={token}")

    def test_disabled_account_loses_its_session(self):
        token = self.login().cookies[COOKIE_NAME]
        with self.database() as db:
            db.get(User, self.user_id).is_active = False
            db.commit()
        self.assert_blocked(f"{COOKIE_NAME}={token}")
        self.assertIsNone(self.sessions.resolve(token))

    def test_first_password_change_rotates_session_and_invalidates_other_device(self):
        with self.database() as db:
            db.get(User, self.user_id).must_change_password = True
            db.commit()
        first = self.login()
        self.assertEqual(first.headers.get("location"), "/change-password")
        other_device = first.cookies[COOKIE_NAME]
        self.client.cookies.clear()
        current = self.login().cookies[COOKIE_NAME]
        self.assertEqual(self.client.get("/mall-orders").headers.get("location"), "/change-password")
        self.assertEqual(self.client.get("/change-password").status_code, 200)
        change = self.client.post("/change-password", data={
            "current_password": self.password, "new_password": "new-password-only",
            "confirm_password": "new-password-only"})
        self.assertEqual(change.status_code, 303)
        self.assertNotEqual(change.cookies[COOKIE_NAME], current)
        self.assertEqual(self.client.get("/mall-orders").status_code, 200)
        for token in (current, other_device):
            self.assert_blocked(f"{COOKIE_NAME}={token}")

    def test_server_expiry_and_capacity_bound(self):
        store = BrowserSessions(capacity=2)
        with patch("app.browser_sessions.time.monotonic", return_value=100):
            oldest, second, newest = [store.issue(1, "fixture") for _ in range(3)]
        with patch("app.browser_sessions.time.monotonic", return_value=100 + LIFETIME - 1):
            self.assertIsNone(store.resolve(oldest))
            self.assertIsNotNone(store.resolve(second))
        with patch("app.browser_sessions.time.monotonic", return_value=100 + LIFETIME):
            self.assertIsNone(store.resolve(newest))
