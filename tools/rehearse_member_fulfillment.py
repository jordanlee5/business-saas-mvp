"""Run from the repo root: python -m tools.rehearse_member_fulfillment.

Exercises the member API and admin forms over loopback, using a new migrated
test database, a synthetic member session and a randomly generated operator password.
"""
import json
import os
import secrets
import socket
import sys
import tempfile
import time
from contextlib import contextmanager
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from alembic import command
from alembic.config import Config

PREFIX = "/api/miniprogram/v1"
PRODUCT = "PRD-ORDER-001"
SKU = "ORDER-SKU-001"
ADDRESS = dict(recipient_name="联调收件人", phone="13800000001", province="测试省",
               city="测试市", district="测试区", detail="隔离联调地址")


class FormFields(HTMLParser):
    """Read the hidden inputs actually issued for a specific admin form."""

    def __init__(self, html, action):
        super().__init__()
        self.action = action
        self.inside = False
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "form":
            self.inside = fields.get("action") == self.action
        elif self.inside and tag == "input" and fields.get("type") == "hidden":
            self.values[fields["name"]] = fields["value"]

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


@contextmanager
def serve(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Isolated service failed to start")
                time.sleep(0.05)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("Isolated service failed to stop")


def seed(profile, app_id, password):
    # Imports occur only after selecting the temporary database.
    import test_order_service as fixtures
    from app.auth import get_password_hash
    from app.database import SessionLocal
    from app.mall.miniprogram_session_service import issue_session
    from app.models import MemberWechatBinding
    from app.time_utils import utc8_now

    with SessionLocal() as db:
        fixture = fixtures.OrderServiceTests()
        fixture.db = db
        with patch.object(fixtures, "NOW", utc8_now()):
            fixture.seed_order_fixture()
        fixture.operator.password_hash = get_password_hash(password)
        fixture.operator.must_change_password = False
        binding = MemberWechatBinding(member_id=fixture.member.id, wechat_app_id=app_id,
                                      openid="isolated-member-only")
        db.add(binding)
        db.commit()
        token, _ = issue_session(binding.id, app_id, binding.openid)
        config = {"operator_name": "联调运营主体", "operator_contact": "help@example.invalid",
                  "suppliers": {fixture.supplier.supplier_public_id: {
                      "enabled": True, "name": "联调发货方", "agreement_ref": "journey-fixture"}}}
        profile.write_text(json.dumps(config), encoding="utf-8")
        return token, fixture.operator.username, fixture.sku_one.id


def api(client, method, path, status=200, **kwargs):
    response = client.request(method, PREFIX + path, **kwargs)
    assert response.status_code == status, (method, path, response.status_code)
    return response.json()


def admin_action(admin, page, action, **fields):
    document = admin.get(page)
    assert document.status_code == 200
    form = FormFields(document.text, action)
    assert form.values, f"Missing form {action}"
    response = admin.post(action, data={**form.values, **fields})
    query = parse_qs(urlsplit(response.headers.get("location", "")).query)
    assert response.status_code in (302, 303) and "message" in query and "error" not in query, (action, query)


def journey(admin, member, username, password, sku_id):
    from app.database import SessionLocal
    from app.mall.inventory_service import audit_inventory_balance
    from app.models import InventoryBalance

    history = []

    def checkpoint(stage, available, reserved, stock, stock_reserved):
        points = api(member, "GET", "/points/summary")
        assert (Decimal(points["available_points"]), Decimal(points["reserved_points"])) == (available, reserved), stage
        ledger = api(member, "GET", "/points/ledger?available_only=true")
        assert ledger["total"] == len(ledger["items"])
        assert sum(Decimal(item["available_points_delta"]) for item in ledger["items"]) == available, stage
        with SessionLocal() as db:
            balance = db.query(InventoryBalance).filter_by(sku_id=sku_id).one()
            assert (balance.on_hand_quantity, balance.reserved_quantity) == (stock, stock_reserved), stage
            assert audit_inventory_balance(db, sku_id=sku_id).is_consistent, stage
        history.append(dict(stage=stage, available_points=available, reserved_points=reserved,
                            stock_on_hand=stock, stock_reserved=stock_reserved))

    login = admin.post("/login", data={"username": username, "password": password})
    assert login.status_code == 302 and login.headers["location"] == "/dashboard"
    checkpoint("seeded", 170, 0, 5, 0)
    admin_action(admin, "/mall-inventory", f"/mall-inventory/skus/{sku_id}/receive",
                 quantity=1, reason="隔离验收入库")
    checkpoint("admin_received", 170, 0, 6, 0)
    product = api(member, "GET", "/products/" + PRODUCT)
    sku = next(row for row in product["skus"] if row["sku_code"] == SKU)
    assert sku["in_stock"] and sku["points_price"] == "40.00"
    address = api(member, "POST", "/addresses", 201, json=ADDRESS)
    lines = [dict(product_public_id=PRODUCT, sku_code=SKU, quantity=2,
                  expected_points_price=sku["points_price"])]
    notice = api(member, "POST", "/orders/fulfillment-preview", json={"lines": lines})
    assert notice["ready"]
    checkout = dict(request_id=secrets.token_hex(16), address_public_id=address["address_public_id"],
                    expected_address=ADDRESS, lines=lines, fulfillment_digest=notice["digest"])
    order = api(member, "POST", "/orders", 201, json=checkout)
    oid = order["order_public_id"]
    assert order["status"] == "CREATED" and order["total_points"] == "80.00"
    assert api(member, "POST", "/orders", json=checkout)["order_public_id"] == oid
    listing = api(member, "GET", "/orders")
    assert listing["total"] == 1 and listing["items"][0]["order_public_id"] == oid
    path = "/mall-orders/" + oid
    assert oid in admin.get("/mall-orders").text
    assert ADDRESS["detail"] in admin.get(path).text
    checkpoint("created_and_replayed", 90, 80, 6, 2)
    tracking = "LOCAL-ONLY-0001"
    actions = [
        ("fulfill", "FULFILLING", {}, (90, 0, 4, 0)),
        ("ship", "SHIPPED", dict(shipping_carrier="联调物流", tracking_number=tracking), (90, 0, 4, 0)),
        ("complete", "COMPLETED", {}, (90, 0, 4, 0)),
        ("refund", "REFUNDED", dict(reason="隔离退款验收"), (170, 0, 6, 0)),
    ]
    for action, status, fields, balances in actions:
        admin_action(admin, path, path + "/" + action, **fields)
        detail = api(member, "GET", "/orders/" + oid)
        assert detail["status"] == status and detail["address"] == ADDRESS
        if action != "fulfill":
            assert detail["tracking_number"] == tracking and detail["shipping_carrier"] == "联调物流"
        checkpoint(status, *balances)
    checkout["request_id"] = secrets.token_hex(16)
    cancelled = api(member, "POST", "/orders", 201, json=checkout)["order_public_id"]
    checkpoint("second_created", 90, 80, 6, 2)
    for _ in range(2):
        assert api(member, "POST", "/orders/" + cancelled + "/cancel")["status"] == "CANCELLED"
    admin_cancelled = admin.get("/mall-orders/" + cancelled)
    assert admin_cancelled.status_code == 200 and cancelled in admin_cancelled.text and "已取消" in admin_cancelled.text
    listing = api(member, "GET", "/orders")
    assert {item["order_public_id"]: item["status"] for item in listing["items"]} == {
        oid: "REFUNDED", cancelled: "CANCELLED"}
    checkpoint("cancelled_and_replayed", 170, 0, 6, 0)
    return history


def run():
    if not __debug__ or "app.database" in sys.modules:
        raise RuntimeError("Run in a fresh Python process with assertions enabled")
    app_id = json.loads(Path("miniprogram/project.config.json").read_text("utf-8"))["appid"]
    with tempfile.TemporaryDirectory(prefix="member-admin-journey-") as work:
        root = Path(work).resolve()
        db_file = root / "journey.db"
        profile = root / "fulfillment.json"
        env = dict(DATABASE_URL="sqlite:///" + db_file.as_posix(), WECHAT_MINIPROGRAM_APP_ID=app_id,
                   WECHAT_MINIPROGRAM_APP_SECRET="", MINIPROGRAM_SESSION_SIGNING_KEY=secrets.token_hex(32),
                   MALL_FULFILLMENT_SETTINGS=str(profile))
        with patch.dict(os.environ, env):
            from app.database import engine
            assert Path(engine.url.database).resolve() == db_file and not db_file.exists()
            try:
                command.upgrade(Config("alembic.ini"), "head")
                password = secrets.token_urlsafe(20)
                token, username, sku_id = seed(profile, app_id, password)
                from app.main import app
                with serve(app) as url, httpx.Client(base_url=url, trust_env=False) as admin, httpx.Client(
                    base_url=url, trust_env=False, headers={"Authorization": "Bearer " + token}
                ) as member:
                    history = journey(admin, member, username, password, sku_id)
            finally:
                engine.dispose()
    return dict(result="PASS", transport="loopback_http", member_identity="synthetic_session",
                logistics="synthetic_manual_waybill", checkpoints=history)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
