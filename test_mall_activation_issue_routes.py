"""M7-24 后台首次签发入口的角色、事务和明文边界。"""

import unittest
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import business_record_detail_page, issue_business_activation_code
from app.mall.audit import MallAuditActionType
from app.models import AdminActionLog, MemberActivationCredential, User
from app.time_utils import utc8_now
import test_member_activation_service


def request(path, *, method="GET", csrf_cookie=""):
    headers = [(b"cookie", f"mall_activation_issue_csrf={csrf_cookie}".encode())] if csrf_cookie else []
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "root_path": "",
        "query_string": b"", "headers": headers,
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


class MallActivationIssueRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_member_activation_service.MemberActivationServiceTests(
            "test_issue_persists_only_versioned_hash"
        )
        self.fixture.setUp()
        self.fixture.batch.claim_deadline = utc8_now() + timedelta(days=10)
        admin = User(
            username="activation_admin", password_hash="test-only",
            role="admin", admin_level=SUPER_ADMIN, is_active=True,
        )
        self.fixture.db.add(admin)
        self.fixture.db.commit()
        self.admin_id = admin.id
        self.record_id = self.fixture.business.id

    def tearDown(self):
        self.fixture.tearDown()

    def actor(self, level=SUPER_ADMIN, *, active=True, role="admin"):
        return SimpleNamespace(
            id=self.admin_id, username="activation_admin",
            role=role, admin_level=level, is_active=active,
        )

    def detail(self, actor):
        with (
            patch("app.main.get_current_user", return_value=actor),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            return business_record_detail_page(
                request(f"/business-records/{self.record_id}"),
                self.record_id, "/business-records", "",
            )

    def issue(self, actor, token="x" * 48, cookie=None):
        with (
            patch("app.main.get_current_user", return_value=actor),
            patch("app.main.SessionLocal", side_effect=self.fixture.Session),
        ):
            return issue_business_activation_code(
                request(
                    f"/business-records/{self.record_id}/activation-issue",
                    method="POST", csrf_cookie=token if cookie is None else cookie,
                ),
                self.record_id, token,
            )

    def test_form_visible_only_to_active_mall_operators(self):
        for actor, visible in (
            (self.actor(SUPER_ADMIN), True),
            (self.actor(OPERATOR), True),
            (self.actor(PRIMARY_REVIEWER), False),
            (self.actor(OPERATOR, active=False), False),
            (self.actor(SUPER_ADMIN, role="partner"), False),
        ):
            with self.subTest(actor=actor):
                response = self.detail(actor)
                self.assertEqual(
                    b"/activation-issue" in response.body, visible,
                )
        with self.fixture.Session() as db:
            self.assertEqual(db.query(MemberActivationCredential).count(), 0)

    def test_post_rejects_unauthorized_and_bad_csrf_before_database(self):
        for actor in (
            None, self.actor(PRIMARY_REVIEWER),
            self.actor(OPERATOR, active=False),
            self.actor(SUPER_ADMIN, role="partner"),
        ):
            with self.subTest(actor=actor), patch("app.main.SessionLocal") as session:
                response = self.issue(actor)
                self.assertEqual(response.status_code, 302)
                session.assert_not_called()
        with patch("app.main.SessionLocal") as session:
            response = self.issue(self.actor(), cookie="different")
            self.assertEqual(response.status_code, 303)
            session.assert_not_called()

    def test_initial_issue_is_audited_once_and_never_replayed(self):
        response = self.issue(self.actor(OPERATOR))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn(b"/business-records/", response.body)
        code_match = re.search(
            rb"[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5}-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5}",
            response.body,
        )
        self.assertIsNotNone(code_match)
        displayed_code = code_match.group().decode()
        with self.fixture.Session() as db:
            credential = db.query(MemberActivationCredential).one()
            audit = db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ACTIVATION_ISSUE.value,
            ).one()
            self.assertEqual(credential.issue_version, 1)
            self.assertEqual(credential.status, "ACTIVE")
            self.assertNotIn(credential.secret_digest.encode(), response.body)
            self.assertNotIn(displayed_code, audit.description)
            self.assertNotIn(displayed_code.replace("-", ""), audit.description)
            digest = credential.secret_digest
        detail = self.detail(self.actor()).body
        self.assertNotIn(b"/activation-issue", detail)
        self.assertNotIn(displayed_code.encode(), detail)
        repeated = self.issue(self.actor(OPERATOR))
        self.assertEqual(repeated.status_code, 303)
        with self.fixture.Session() as db:
            credential = db.query(MemberActivationCredential).one()
            self.assertEqual(credential.issue_version, 1)
            self.assertEqual(credential.secret_digest, digest)
            self.assertEqual(db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ACTIVATION_ISSUE.value,
            ).count(), 1)

    def test_audit_failure_rolls_back_credential(self):
        with patch("app.main.AdminActionLog", side_effect=RuntimeError("audit failed")):
            with self.assertRaisesRegex(RuntimeError, "audit failed"):
                self.issue(self.actor())
        with self.fixture.Session() as db:
            self.assertEqual(db.query(MemberActivationCredential).count(), 0)

    def test_result_render_failure_rolls_back_credential_and_audit(self):
        with patch("app.main.templates.TemplateResponse", side_effect=RuntimeError("render failed")):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                self.issue(self.actor())
        with self.fixture.Session() as db:
            self.assertEqual(db.query(MemberActivationCredential).count(), 0)
            self.assertEqual(db.query(AdminActionLog).filter_by(
                action_type=MallAuditActionType.ACTIVATION_ISSUE.value,
            ).count(), 0)


if __name__ == "__main__":
    unittest.main()
