import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.database import Base
from app.main import (
    admin_navigation_context,
    export_member_points,
    member_points_detail_page,
    member_points_page,
)
from app.mall import record_initial_points_grant
from app.models import (
    AdminActionLog,
    BusinessRecord,
    Member,
    PointsAccount,
    PointsGrant,
    UploadBatch,
    User,
)


NOW = datetime(2026, 9, 8, 14, 0, 0)


def make_request(path="/member-points"):
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    })


class MemberPointsRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "member-points-routes.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        with self.Session() as db:
            self.super_admin = self.add_user(
                db, "route-super", SUPER_ADMIN
            )
            self.operator = self.add_user(
                db, "route-operator", OPERATOR
            )
            self.reviewer = self.add_user(
                db, "route-reviewer", PRIMARY_REVIEWER
            )
            partner = User(
                username="route-partner",
                password_hash="test-only",
                role="partner",
                is_active=True,
            )
            db.add(partner)
            db.flush()
            batch = UploadBatch(
                user_id=partner.id,
                filename="route.xlsx",
                total_rows=1,
                success_rows=1,
                failed_rows=0,
                acceptance_status="已承接",
                redemption_mode="MALL_REDEMPTION",
                claim_deadline=NOW + timedelta(days=30),
            )
            db.add(batch)
            db.flush()
            member = Member(member_public_id="MEMBER-ROUTE-001")
            db.add(member)
            db.flush()
            self.member_id = member.id
            account = PointsAccount(member_id=member.id)
            db.add(account)
            db.flush()
            business = BusinessRecord(
                user_id=partner.id,
                batch_id=batch.id,
                business_no="BR-ROUTE-001",
                public_business_no="BR-ROUTE-001",
                name="路由客户",
                phone="13812345678",
                plate_number="桂A12345",
                points_amount=Decimal("88.00"),
                bank_card="",
                redemption_mode="MALL_REDEMPTION",
                claim_status="ACTIVATED",
            )
            db.add(business)
            db.flush()
            grant = PointsGrant(
                account_id=account.id,
                business_record_id=business.id,
                granted_points=Decimal("88.00"),
                available_points=Decimal("0.00"),
                reserved_points=Decimal("0.00"),
                activated_at=NOW,
                expires_at=NOW + timedelta(days=365),
                status="ACTIVE",
            )
            db.add(grant)
            db.flush()
            record_initial_points_grant(
                db,
                grant=grant,
                idempotency_key="fixture-member-route",
                reference_type="BUSINESS_RECORD",
                reference_id=business.public_business_no,
                now=NOW,
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def add_user(db, username, admin_level):
        user = User(
            username=username,
            password_hash="test-only",
            role="admin",
            admin_level=admin_level,
            is_active=True,
        )
        db.add(user)
        db.flush()
        return SimpleNamespace(
            id=user.id,
            username=user.username,
            role=user.role,
            admin_level=user.admin_level,
            is_active=True,
        )

    def test_navigation_context_exposes_member_points_by_role(self):
        for user, allowed in (
            (self.super_admin, True),
            (self.operator, True),
            (self.reviewer, False),
            (None, False),
        ):
            with self.subTest(user=user):
                with patch("app.main.get_current_user", return_value=user):
                    context = admin_navigation_context(make_request())
                self.assertEqual(
                    context["can_view_mall_member_points"], allowed
                )
                self.assertEqual(
                    context["can_export_mall_member_points"], allowed
                )

    def test_list_route_redirects_unauthorized_and_renders_for_operator(self):
        with patch("app.main.get_current_user", return_value=None):
            response = member_points_page(
                make_request(), "", "ALL", 1, 10, ""
            )
        self.assertEqual(response.headers["location"], "/login")
        with patch("app.main.get_current_user", return_value=self.reviewer):
            response = member_points_page(
                make_request(), "", "ALL", 1, 10, ""
            )
        self.assertEqual(response.headers["location"], "/dashboard")
        with (
            patch("app.main.get_current_user", return_value=self.operator),
            patch("app.main.SessionLocal", side_effect=self.Session),
        ):
            response = member_points_page(
                make_request(), "", "ALL", 1, 10, ""
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MEMBER-ROUTE-001", response.body)

    def test_detail_renders_only_masked_source_customer_data(self):
        with (
            patch("app.main.get_current_user", return_value=self.operator),
            patch("app.main.SessionLocal", side_effect=self.Session),
        ):
            response = member_points_detail_page(
                make_request(f"/member-points/{self.member_id}"),
                self.member_id,
            )
        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("路**", body)
        self.assertIn("138****5678", body)
        self.assertNotIn("路由客户", body)
        self.assertNotIn("13812345678", body)

    def test_missing_detail_redirects_to_list_with_error(self):
        with (
            patch("app.main.get_current_user", return_value=self.operator),
            patch("app.main.SessionLocal", side_effect=self.Session),
        ):
            response = member_points_detail_page(
                make_request("/member-points/99999"), 99999
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["location"].startswith(
            "/member-points?error="
        ))

    def test_export_is_permission_guarded_and_commits_audit_log(self):
        with patch("app.main.get_current_user", return_value=self.reviewer):
            response = export_member_points(
                make_request(), self.member_id
            )
        self.assertEqual(response.headers["location"], "/dashboard")
        with (
            patch("app.main.get_current_user", return_value=self.operator),
            patch("app.main.SessionLocal", side_effect=self.Session),
            patch("app.main.utc8_now", return_value=NOW),
        ):
            response = export_member_points(
                make_request(), self.member_id
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument",
            response.media_type,
        )
        self.assertIn(
            f"member_points_{self.member_id}_20260908140000.xlsx",
            response.headers["content-disposition"],
        )
        with self.Session() as db:
            log = db.query(AdminActionLog).filter(
                AdminActionLog.action_type
                == "mall_member_points_export"
            ).one()
            self.assertEqual(log.admin_id, self.operator.id)
            self.assertEqual(log.target_id, self.member_id)


if __name__ == "__main__":
    unittest.main()
