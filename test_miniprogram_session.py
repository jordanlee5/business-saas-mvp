"""Offline tests for bound-member sessions; never contact WeChat."""

import os
import tempfile
import time
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.miniprogram_v1 import (
    WechatLoginRequest, create_member_session, get_current_member,
    get_member_points_summary, get_member_points_grants,
    get_member_points_ledger,
)
from app.database import Base
from app.mall.wechat_identity_service import WechatIdentity, WechatCodeError
from app.mall.miniprogram_session_service import (
    InvalidSession, SessionConfigurationError, SESSION_TTL_SECONDS,
    issue_session, verify_session,
)
from app.mall.points_ledger_service import record_initial_points_grant
from app.time_utils import utc8_now
from app.models import (
    BusinessRecord, Member, MemberWechatBinding, PointsAccount, PointsGrant,
    PointsLedgerEntry, UploadBatch, User,
)


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

    def _points_grant(self):
        partner = User(username="session-points-partner", password_hash="test",
                       role="partner", is_active=True)
        self.db.add(partner)
        self.db.flush()
        now = utc8_now()
        batch = UploadBatch(user_id=partner.id, filename="points.xlsx",
                            total_rows=1, success_rows=1, failed_rows=0,
                            acceptance_status="已承接",
                            redemption_mode="MALL_REDEMPTION",
                            claim_deadline=now + timedelta(days=40))
        self.db.add(batch)
        self.db.flush()
        business = BusinessRecord(
            user_id=partner.id, batch_id=batch.id, business_no="BR-SESSION-001",
            public_business_no="BR-SESSION-001", name="仅供服务端测试",
            phone="13800000000", plate_number="京A00001",
            points_amount=Decimal("12.50"), bank_card="",
            redemption_mode="MALL_REDEMPTION", claim_status="ACTIVATED",
        )
        self.db.add(business)
        self.db.flush()
        account = PointsAccount(member_id=self.member.id)
        self.db.add(account)
        self.db.flush()
        grant = PointsGrant(
            account_id=account.id, business_record_id=business.id,
            granted_points=Decimal("12.50"), available_points=Decimal("0.00"),
            reserved_points=Decimal("0.00"), activated_at=now,
            expires_at=now + timedelta(days=20), status="ACTIVE",
        )
        self.db.add(grant)
        self.db.flush()
        record_initial_points_grant(
            self.db, grant=grant, idempotency_key="session-points-grant",
            reference_type="BUSINESS_RECORD", reference_id="BR-SESSION-001",
            now=now,
        )
        self.db.commit()
        return account, grant

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

    def test_points_summary_requires_member_bearer(self):
        with self.assertRaises(HTTPException) as rejected:
            get_member_points_summary(Response(), None, self.db)
        self.assertEqual(rejected.exception.status_code, 401)

    def test_points_summary_missing_account_fails_closed(self):
        with self.assertRaises(HTTPException) as missing:
            get_member_points_summary(Response(), "Bearer " + self._login().token,
                                      self.db)
        self.assertEqual(missing.exception.status_code, 503)

    def test_points_summary_zero_balance_is_private_and_not_cached(self):
        self.db.add(PointsAccount(member_id=self.member.id))
        self.db.commit()
        response = Response()
        summary = get_member_points_summary(response,
                    "Bearer " + self._login().token, self.db)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(summary.model_dump(), {
            "available_points": "0.00", "reserved_points": "0.00",
            "grant_count": 0, "nearest_expires_at": None,
        })

    def test_points_summary_uses_ledger_checked_member_account(self):
        self._points_grant()
        result = get_member_points_summary(Response(),
                         "Bearer " + self._login().token, self.db)
        self.assertEqual(result.available_points, "12.50")
        self.assertEqual(result.reserved_points, "0.00")
        self.assertEqual(result.grant_count, 1)
        self.assertTrue(result.nearest_expires_at.endswith("+08:00"))
        self.assertNotIn("phone", result.model_dump())

    def test_points_summary_drift_fails_without_balance_disclosure(self):
        account, _grant = self._points_grant()
        account.available_points = Decimal("99.00")
        self.db.commit()
        with self.assertRaises(HTTPException) as drift:
            get_member_points_summary(Response(),
                    "Bearer " + self._login().token, self.db)
        self.assertEqual(drift.exception.status_code, 503)
        self.assertNotIn("99.00", str(drift.exception.detail))

    def test_points_summary_isolated_to_token_binding(self):
        self._points_grant()
        other = Member(member_public_id="MEMBER-SESSION-002", is_active=True)
        self.db.add(other)
        self.db.flush()
        other_binding = MemberWechatBinding(
            member_id=other.id, wechat_app_id=APP_ID, openid="other-openid",
        )
        self.db.add(other_binding)
        self.db.add(PointsAccount(member_id=other.id))
        self.db.commit()
        other_token, _ttl = issue_session(other_binding.id, APP_ID,
                                          other_binding.openid)
        other_result = get_member_points_summary(Response(),
                         "Bearer " + other_token, self.db)
        own_result = get_member_points_summary(Response(),
                         "Bearer " + self._login().token, self.db)
        self.assertEqual(other_result.available_points, "0.00")
        self.assertEqual(own_result.available_points, "12.50")

    def test_points_summary_rejects_revoked_member(self):
        self._points_grant()
        token = self._login().token
        self.member.is_active = False
        self.db.commit()
        with self.assertRaises(HTTPException) as revoked:
            get_member_points_summary(Response(), "Bearer " + token, self.db)
        self.assertEqual(revoked.exception.status_code, 401)

    def _grant_page(self, token=None, page=1, page_size=20):
        response = Response()
        result = get_member_points_grants(
            response, "Bearer " + (token or self._login().token),
            page=page, page_size=page_size, db=self.db,
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return result

    def test_grant_page_rejects_missing_token_and_account(self):
        with self.assertRaises(HTTPException) as missing_token:
            get_member_points_grants(Response(), None, page=1, page_size=20,
                                     db=self.db)
        self.assertEqual(missing_token.exception.status_code, 401)
        with self.assertRaises(HTTPException) as missing_account:
            self._grant_page()
        self.assertEqual(missing_account.exception.status_code, 503)

    def test_grant_page_empty_account_and_out_of_range_page(self):
        self.db.add(PointsAccount(member_id=self.member.id))
        self.db.commit()
        result = self._grant_page(page=4, page_size=2)
        self.assertEqual(result.model_dump(), {
            "items": [], "page": 4, "page_size": 2, "total": 0,
        })

    def test_grant_page_is_sorted_and_never_discloses_business_identity(self):
        account, first = self._points_grant()
        first_business = self.db.query(BusinessRecord).filter(
            BusinessRecord.id == first.business_record_id).one()
        later = first.activated_at + timedelta(minutes=1)
        business = BusinessRecord(
            user_id=first_business.user_id, batch_id=first_business.batch_id,
            business_no="BR-SESSION-002", public_business_no="BR-SESSION-002",
            name="敏感姓名", phone="13900000000", plate_number="京B00002",
            points_amount=Decimal("3.25"), bank_card="",
            redemption_mode="MALL_REDEMPTION", claim_status="ACTIVATED",
        )
        self.db.add(business)
        self.db.flush()
        second = PointsGrant(
            account_id=account.id, business_record_id=business.id,
            granted_points=Decimal("3.25"), available_points=Decimal("0.00"),
            reserved_points=Decimal("0.00"), activated_at=later,
            expires_at=later + timedelta(days=20), status="ACTIVE",
        )
        self.db.add(second)
        self.db.flush()
        record_initial_points_grant(
            self.db, grant=second, idempotency_key="session-points-grant-2",
            reference_type="BUSINESS_RECORD", reference_id="BR-SESSION-002",
            now=later,
        )
        self.db.commit()
        newest = self._grant_page(page=1, page_size=1)
        oldest = self._grant_page(page=2, page_size=1)
        self.assertEqual((newest.total, oldest.total), (2, 2))
        self.assertEqual(newest.items[0].granted_points, "3.25")
        self.assertEqual(oldest.items[0].granted_points, "12.50")
        self.assertEqual(newest.items[0].status, "ACTIVE")
        self.assertTrue(newest.items[0].expires_at.endswith("+08:00"))
        self.assertEqual(self._grant_page(page=3, page_size=1).items, [])
        self.assertEqual(set(newest.items[0].model_dump()), {
            "granted_points", "available_points", "reserved_points",
            "activated_at", "expires_at", "status",
        })

    def test_grant_page_isolated_and_rejects_balance_drift(self):
        account, _grant = self._points_grant()
        other = Member(member_public_id="MEMBER-SESSION-002", is_active=True)
        self.db.add(other)
        self.db.flush()
        binding = MemberWechatBinding(member_id=other.id,
                                      wechat_app_id=APP_ID, openid="other-openid")
        self.db.add(binding)
        self.db.add(PointsAccount(member_id=other.id))
        self.db.commit()
        other_token, _ = issue_session(binding.id, APP_ID, binding.openid)
        self.assertEqual(self._grant_page(other_token).total, 0)
        self.assertEqual(self._grant_page().total, 1)
        account.available_points = Decimal("99.00")
        self.db.commit()
        with self.assertRaises(HTTPException) as drift:
            self._grant_page()
        self.assertEqual(drift.exception.status_code, 503)
        self.assertNotIn("99.00", str(drift.exception.detail))

    def test_grant_page_rejects_revoked_member(self):
        self._points_grant()
        token = self._login().token
        self.member.is_active = False
        self.db.commit()
        with self.assertRaises(HTTPException) as revoked:
            self._grant_page(token)
        self.assertEqual(revoked.exception.status_code, 401)

    def _ledger_page(self, token=None, page=1, page_size=20):
        response = Response()
        result = get_member_points_ledger(
            response, "Bearer " + (token or self._login().token),
            page=page, page_size=page_size, db=self.db,
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return result

    def test_ledger_page_requires_bound_member_and_account(self):
        with self.assertRaises(HTTPException) as missing_token:
            get_member_points_ledger(Response(), None, page=1, page_size=20,
                                     db=self.db)
        self.assertEqual(missing_token.exception.status_code, 401)
        with self.assertRaises(HTTPException) as missing_account:
            self._ledger_page()
        self.assertEqual(missing_account.exception.status_code, 503)

    def test_ledger_page_empty_and_out_of_range(self):
        self.db.add(PointsAccount(member_id=self.member.id))
        self.db.commit()
        self.assertEqual(self._ledger_page(page=3, page_size=2).model_dump(), {
            "items": [], "page": 3, "page_size": 2, "total": 0,
        })

    def test_ledger_page_stable_pagination_and_restricted_fields(self):
        account, grant = self._points_grant()
        first = self.db.query(PointsLedgerEntry).filter(
            PointsLedgerEntry.grant_id == grant.id,
        ).one()
        second = PointsLedgerEntry(
            grant_id=grant.id, entry_type="ADJUST",
            available_points_delta=Decimal("-2.25"),
            reserved_points_delta=Decimal("0.00"),
            idempotency_key="session-ledger-adjust-2",
            actor_admin_id=self.db.query(User.id).first().id,
            reason="私密运营备注", reference_type="SENSITIVE",
            reference_id="PRIVATE-123", created_at=first.created_at,
        )
        self.db.add(second)
        grant.available_points = Decimal("10.25")
        account.available_points = Decimal("10.25")
        self.db.commit()
        newer = self._ledger_page(page=1, page_size=1)
        older = self._ledger_page(page=2, page_size=1)
        self.assertEqual((newer.total, older.total), (2, 2))
        self.assertEqual(newer.items[0].entry_type, "ADJUST")
        self.assertEqual(newer.items[0].available_points_delta, "-2.25")
        self.assertEqual(older.items[0].entry_type, "GRANT")
        self.assertEqual(older.items[0].available_points_delta, "12.50")
        self.assertEqual(self._ledger_page(page=3, page_size=1).items, [])
        self.assertTrue(newer.items[0].created_at.endswith("+08:00"))
        self.assertEqual(set(newer.items[0].model_dump()), {
            "entry_type", "available_points_delta", "reserved_points_delta",
            "created_at",
        })
        self.assertNotIn("私密运营备注", str(newer.model_dump()))
        self.assertNotIn("PRIVATE-123", str(newer.model_dump()))
        self.assertEqual(self.db.query(PointsLedgerEntry).count(), 2)

    def test_ledger_page_isolated_and_rejects_drift(self):
        account, _grant = self._points_grant()
        other = Member(member_public_id="MEMBER-SESSION-002", is_active=True)
        self.db.add(other)
        self.db.flush()
        binding = MemberWechatBinding(member_id=other.id,
                                      wechat_app_id=APP_ID, openid="other-openid")
        self.db.add(binding)
        self.db.add(PointsAccount(member_id=other.id))
        self.db.commit()
        other_token, _ = issue_session(binding.id, APP_ID, binding.openid)
        self.assertEqual(self._ledger_page(other_token).total, 0)
        self.assertEqual(self._ledger_page().total, 1)
        account.available_points = Decimal("99.00")
        self.db.commit()
        with self.assertRaises(HTTPException) as drift:
            self._ledger_page()
        self.assertEqual(drift.exception.status_code, 503)
        self.assertNotIn("99.00", str(drift.exception.detail))

    def test_ledger_page_rejects_revoked_member(self):
        self._points_grant()
        token = self._login().token
        self.member.is_active = False
        self.db.commit()
        with self.assertRaises(HTTPException) as revoked:
            self._ledger_page(token)
        self.assertEqual(revoked.exception.status_code, 401)


    def _consumer_ledger_fixture(self):
        account, grant = self._points_grant()
        now = utc8_now()
        movements = [("RESERVE", "-4.00", "4.00"), ("CONSUME", "0.00", "-4.00"),
                     ("REFUND", "4.00", "0.00"), ("RESERVE", "-2.00", "2.00"),
                     ("RELEASE", "2.00", "-2.00"), ("EXPIRE", "-1.00", "0.00")]
        for i, (kind, available, reserved) in enumerate(movements):
            self.db.add(PointsLedgerEntry(grant_id=grant.id, entry_type=kind,
                available_points_delta=Decimal(available), reserved_points_delta=Decimal(reserved),
                idempotency_key=f"consumer-{i}", created_at=now,
                reference_type="ORDER", reference_id="PRIVATE-ORDER-REFERENCE"))
        grant.available_points = account.available_points = Decimal("11.50")
        self.db.commit()
        return account

    def test_consumer_ledger_filters_before_count_and_pagination(self):
        self._consumer_ledger_fixture()
        token = self._login().token
        pages = [get_member_points_ledger(Response(), "Bearer " + token,
            page=i, page_size=1, db=self.db, available_only=True) for i in range(1, 8)]
        self.assertEqual([p.total for p in pages], [6] * 7)
        self.assertEqual([p.items[0].entry_type for p in pages[:-1]],
                         ["EXPIRE", "RELEASE", "RESERVE", "REFUND", "RESERVE", "GRANT"])
        self.assertEqual(pages[-1].items, [])
        self.assertEqual(sum(Decimal(p.items[0].available_points_delta) for p in pages[:-1]), Decimal("11.50"))
        self.assertEqual(self._ledger_page().total, 7)  # Complete legacy view remains available.
        self.assertNotIn("PRIVATE-ORDER-REFERENCE", str([p.model_dump() for p in pages]))

    def test_consumer_ledger_keeps_member_isolation_revocation_and_balance_audit(self):
        account = self._consumer_ledger_fixture()
        other = Member(member_public_id="MEMBER-CONSUMER-OTHER", is_active=True)
        self.db.add(other); self.db.flush()
        binding = MemberWechatBinding(member_id=other.id, wechat_app_id=APP_ID, openid="consumer-other")
        self.db.add_all([binding, PointsAccount(member_id=other.id)]); self.db.commit()
        other_token, _ = issue_session(binding.id, APP_ID, binding.openid)
        self.assertEqual(get_member_points_ledger(Response(), "Bearer " + other_token,
            page=1, page_size=20, db=self.db, available_only=True).total, 0)
        token = self._login().token
        account.available_points = Decimal("99.00"); self.db.commit()
        with self.assertRaises(HTTPException) as drift:
            get_member_points_ledger(Response(), "Bearer " + token,
                page=1, page_size=20, db=self.db, available_only=True)
        self.assertEqual(drift.exception.status_code, 503)
        self.member.is_active = False; self.db.commit()
        with self.assertRaises(HTTPException) as revoked:
            get_member_points_ledger(Response(), "Bearer " + token,
                page=1, page_size=20, db=self.db, available_only=True)
        self.assertEqual(revoked.exception.status_code, 401)

    def test_consumer_ledger_http_query_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.miniprogram_v1 import miniprogram_v1_router, public_catalog_session
        self._consumer_ledger_fixture()
        app = FastAPI()
        app.include_router(miniprogram_v1_router)
        app.dependency_overrides[public_catalog_session] = lambda: self.db
        token = self._login().token
        with TestClient(app) as client:
            path = "/api/miniprogram/v1/points/ledger"
            headers = {"Authorization": "Bearer " + token}
            response = client.get(path + "?available_only=true&page_size=1", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 6)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(client.get(path, headers=headers).json()["total"], 7)
            self.assertEqual(client.get(path + "?available_only=invalid", headers=headers).status_code, 422)
            self.assertEqual(client.get(path + "?available_only=true").status_code, 401)


if __name__ == "__main__":
    unittest.main()
