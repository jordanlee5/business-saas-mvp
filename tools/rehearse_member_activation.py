"""Run from the repo root: python -m tools.rehearse_member_activation.

Exercises issuance, activation, redemption and cancellation over loopback.
The member keeps the session obtained after exchanging a synthetic WeChat code.
"""
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from alembic import command
from alembic.config import Config

from tools.rehearse_member_fulfillment import FormFields, PREFIX, serve
from tools.activated_order_journey import prepare_catalog, redeem_and_cancel


class TestIdentities:
    def __init__(self, app_id):
        self.app_id = app_id
        self.codes = {}

    def fresh_code(self, identity):
        code = secrets.token_hex(16)
        self.codes[code] = identity
        return code

    def exchange(self, url):
        query = parse_qs(urlsplit(url).query)
        assert query["appid"] == [self.app_id]
        identity = self.codes.pop(query["js_code"][0], None)
        payload = ({"openid": identity, "session_key": "synthetic-key"}
                   if identity else {"errcode": 40029})
        return json.dumps(payload).encode()


def seed(password):
    from app.admin_permissions import OPERATOR
    from app.auth import get_password_hash
    from app.database import SessionLocal
    from app.models import BusinessRecord, UploadBatch, User
    from app.time_utils import utc8_now

    with SessionLocal() as db:
        operator = User(username="test-issuer", role="admin", admin_level=OPERATOR,
                        password_hash=get_password_hash(password), is_active=True)
        partner = User(username="test-partner", role="partner", is_active=True)
        db.add_all([operator, partner])
        db.flush()
        batch = UploadBatch(user_id=partner.id, filename="activation-test.xlsx", total_rows=2,
                            success_rows=2, failed_rows=0, acceptance_status="已承接",
                            redemption_mode="MALL_REDEMPTION", claim_deadline=utc8_now() + timedelta(days=30))
        db.add(batch)
        db.flush()
        records = []
        for number, points in [("BR-JOURNEY-01", "12.50"), ("BR-JOURNEY-02", "7.50")]:
            record = BusinessRecord(user_id=partner.id, batch_id=batch.id, business_no=number,
                                    public_business_no=number, name="联调会员", phone="13800000001",
                                    plate_number="京A00001", bank_card="", points_amount=Decimal(points),
                                    redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION")
            db.add(record)
            db.flush()
            records.append((record.id, number))
        db.commit()
        return records


def issue(admin, record_id):
    detail_path = f"/business-records/{record_id}"
    action = detail_path + "/activation-issue"
    page = admin.get(detail_path)
    assert page.status_code == 200
    fields = FormFields(page.text, action).values
    assert fields.get("csrf_token")
    response = admin.post(action, data=fields)
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    match = re.search(r"[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5}-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5}", response.text)
    assert match is not None, "Initial issue did not display a credential"
    return match.group()


def journey(admin, member, identities, records, password):
    from app.database import SessionLocal
    from app.models import (BusinessRecord, Member, MemberActivationCredential, MemberWechatBinding,
                            PointsAccount, PointsGrant, PointsLedgerEntry)
    login = admin.post("/login", data={"username": "test-issuer", "password": password})
    assert login.status_code == 302 and login.headers["location"] == "/dashboard"
    first, second = records
    owner, other = "owner-fixture", "other-fixture"
    history = []

    def request(path, expected, **kwargs):
        response = member.request("POST" if "json" in kwargs else "GET", PREFIX + path, **kwargs)
        assert response.status_code == expected, (path, response.status_code)
        assert "no-store" in response.headers["cache-control"], path
        return response.json()

    def sign_in(code, status):
        return request("/auth/wechat-session", status, json={"code": code})

    def submit(number, credential, code, status, plate="京A00001"):
        return request("/activation/redeem", status, json=dict(
            business_public_no=number, phone="13800000001", plate_number=plate,
            activation_code=credential, wechat_code=code))

    def checkpoint(stage, claimed):
        with SessionLocal() as db:
            expected_total = sum((points for _, points in claimed), Decimal(0))
            active = int(bool(claimed))
            assert db.query(Member).count() == db.query(MemberWechatBinding).count() == active
            assert db.query(PointsAccount).count() == active
            assert db.query(PointsGrant).count() == db.query(PointsLedgerEntry).count() == len(claimed)
            if claimed:
                binding = db.query(MemberWechatBinding).one()
                account = db.query(PointsAccount).one()
                assert (binding.wechat_app_id, binding.openid) == (identities.app_id, owner)
                assert binding.member_id == account.member_id and account.available_points == expected_total
                assert account.reserved_points == 0
                for record_id, points in claimed:
                    grant = db.query(PointsGrant).filter_by(business_record_id=record_id).one()
                    assert grant.account_id == account.id and grant.granted_points == points
                    assert db.get(BusinessRecord, record_id).claim_status == "ACTIVATED"
                    credential = db.query(MemberActivationCredential).filter_by(business_record_id=record_id).one()
                    assert credential.status == "USED"
        history.append(dict(stage=stage, members=active, grants=len(claimed), available_points=str(expected_total)))

    code = issue(admin, first[0])
    sign_in(identities.fresh_code(owner), 403)
    checkpoint("issued_before_activation", [])
    submit(first[1], code, identities.fresh_code(other), 400, plate="沪B00002")
    checkpoint("wrong_factor", [])
    used_login_code = identities.fresh_code(owner)
    assert submit(first[1], code, used_login_code, 200) == {"status": "activated"}
    first_claim = [(first[0], Decimal("12.50"))]
    checkpoint("owner_activated", first_claim)
    sign_in(used_login_code, 401)
    session = sign_in(identities.fresh_code(owner), 200)
    headers = {"Authorization": "Bearer " + session["token"]}
    identity = request("/auth/me", 200, headers=headers)
    assert request("/points/summary", 200, headers=headers)["available_points"] == "12.50"
    own_replay = submit(first[1], code, identities.fresh_code(owner), 400)
    other_replay = submit(first[1], code, identities.fresh_code(other), 400)
    assert own_replay == other_replay
    sign_in(identities.fresh_code(other), 403)
    checkpoint("same_and_other_identity_rejected", first_claim)
    second_code = issue(admin, second[0])
    submit(second[1], second_code, identities.fresh_code(owner), 200)
    checkpoint("second_business_same_member", first_claim + [(second[0], Decimal("7.50"))])
    assert request("/auth/me", 200, headers=headers) == identity
    assert request("/points/summary", 200, headers=headers)["available_points"] == "20.00"
    ledger = request("/points/ledger?available_only=true", 200, headers=headers)
    assert ledger["total"] == 2 and all(item["entry_type"] == "GRANT" for item in ledger["items"])
    assert sum(Decimal(item["available_points_delta"]) for item in ledger["items"]) == Decimal("20.00")
    # Continue the order flow using exactly the session obtained after activation.
    member.headers.update(headers)
    return history, identity["member_public_id"]


def run():
    if not __debug__ or "app.database" in sys.modules:
        raise RuntimeError("Run in a fresh Python process with assertions enabled")
    app_id = json.loads(Path("miniprogram/project.config.json").read_text("utf-8"))["appid"]
    with tempfile.TemporaryDirectory(prefix="activation-journey-") as work:
        database = Path(work).resolve() / "activation.db"
        profile = database.parent / "fulfillment.json"
        environment = dict(DATABASE_URL="sqlite:///" + database.as_posix(), WECHAT_MINIPROGRAM_APP_ID=app_id,
                           MALL_FULFILLMENT_SETTINGS=str(profile),
                           WECHAT_MINIPROGRAM_APP_SECRET="synthetic-local-only",
                           MINIPROGRAM_SESSION_SIGNING_KEY=secrets.token_hex(32))
        with patch.dict(os.environ, environment):
            from app.database import engine
            assert Path(engine.url.database).resolve() == database and not database.exists()
            try:
                command.upgrade(Config("alembic.ini"), "head")
                password = secrets.token_urlsafe(20)
                records = seed(password)
                product_id, sku_id = prepare_catalog(profile)
                from app.main import app
                identities = TestIdentities(app_id)
                with patch("app.mall.wechat_identity_service._fetch_response", identities.exchange), serve(app) as origin:
                    with httpx.Client(base_url=origin, trust_env=False) as admin, httpx.Client(
                        base_url=origin, trust_env=False
                    ) as member:
                        history, public_member_id = journey(admin, member, identities, records, password)
                        redemption = redeem_and_cancel(admin, member, product_id, sku_id, public_member_id)
            finally:
                engine.dispose()
    return dict(result="PASS", transport="loopback_http", wechat_exchange="synthetic_one_use_codes",
                checkpoints=history, redemption_checkpoints=redemption)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
