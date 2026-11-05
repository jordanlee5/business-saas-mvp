"""Synthetic fixture and guarded app factory for rehearse_mixed_workers only."""
import json
import os
from pathlib import Path
import secrets

APP_ID = "wx0123456789abcdef"
STATE = Path("/data/mixed-private.json")


def guard():
    if os.environ.get("MIXED_LOAD_DISPOSABLE") != "fresh-docker-only" or not Path("/.dockerenv").exists():
        raise RuntimeError("Only the dedicated disposable Docker runner is supported.")


def configure():
    guard()
    from tools.staging_postgres import configure, preflight
    url = configure()
    preflight(url)
    return url


def create_app():
    configure()
    # Offline identity exchange only in this explicit test entry point.
    from urllib.parse import parse_qs, urlsplit
    from app.mall import wechat_identity_service
    def exchange(url):
        code = parse_qs(urlsplit(url).query)["js_code"][0]
        identity = code.split("_", 1)[0]
        if not identity.startswith("mixed-") or not identity[6:].isdigit():
            return b'{"errcode":40029}'
        return json.dumps({"openid": identity, "session_key": "synthetic-only"}).encode()
    wechat_identity_service._fetch_response = exchange
    os.environ["WECHAT_MINIPROGRAM_APP_SECRET"] = "synthetic-only"
    from app.main import app
    return app


def seed():
    guard()
    from tools.staging_postgres import configure
    configure()
    from app.database import SessionLocal
    from app.auth import get_password_hash
    from app.models import (User, Product, ProductSku, ProductMedia, MemberWechatBinding,
                            InventoryBalance)
    from app.mall.activation_service import activate_mall_business
    from app.mall.inventory_service import receive_inventory
    from tools.rehearse_concurrent_mall import seed as seed_catalog
    if STATE.exists():
        raise RuntimeError("Refuse to reseed an existing fixture.")
    with SessionLocal() as db:
        if db.query(User).count():
            raise RuntimeError("Fixture requires newly initialized empty business tables.")
    credentials, selected = seed_catalog(Path("/data/fulfillment.json"), 120, 12,
                                         products=400, skus_per_product=10)
    password = secrets.token_urlsafe(24)
    from PIL import Image
    import random
    folder = Path("/data/uploads/mall_products/mixed")
    folder.mkdir(parents=True)
    rng = random.Random(1001)
    pixels = rng.randbytes(768*768*3)
    Image.frombytes("RGB", (768,768), pixels).save(folder/"main.jpg", quality=70)
    with SessionLocal.begin() as db:
        actor = db.query(User).filter_by(username="load-operator").one()
        actor.password_hash = get_password_hash(password)
        actor.must_change_password = False
        # Existing OCR upload policy requires a reviewer or super administrator.
        actor.admin_level = "super_admin"
        # Inventory authorization deliberately reloads the actor from the DB.
        db.flush()
        for sku in db.query(ProductSku).order_by(ProductSku.id):
            if sku.id != selected:
                # Actual ledger-backed inventory across the entire catalog.
                receive_inventory(db, actor_admin_id=actor.id, sku_id=sku.id,
                    quantity=(sku.id % 4 + 1)*10, reason="synthetic mixed load",
                    idempotency_key="mixed-stock-"+str(sku.id))
        for product in db.query(Product):
            image_name=product.product_public_id+".jpg"
            (folder/image_name).write_bytes((folder/"main.jpg").read_bytes())
            db.add(ProductMedia(product_id=product.id,media_role="MAIN",uploaded_by_id=actor.id,
                   image_path="/uploads/mall_products/mixed/"+image_name))
        partner = db.query(User).filter_by(username="load-partner").one()
        partner_id = partner.id
    for i, (number, code) in enumerate(credentials[:24]):
        with SessionLocal.begin() as db:
            result = activate_mall_business(db, business_public_no=number, phone="13800000001",
                plate_number="京A00001", activation_code=code, wechat_app_id=APP_ID, openid="mixed-"+str(i))
            assert result.success
    STATE.write_text(json.dumps(dict(password=password,credentials=credentials,sku=selected,
                                    partner_id=partner_id)),encoding="utf8")
    STATE.chmod(0o600)
    from app.auth import verify_password
    with SessionLocal() as db:
        saved=db.query(User).filter_by(username="load-operator").one()
        assert saved.admin_level=="super_admin" and verify_password(password,saved.password_hash)
    print(json.dumps(dict(seed="PASS",products=400,skus=4000,inventory_rows=4000,
                          image_bytes=(folder/"main.jpg").stat().st_size)))


def client_state():
    configure()
    from app.database import SessionLocal
    from app.models import MemberWechatBinding
    from app.mall.miniprogram_session_service import issue_session
    state = json.loads(STATE.read_text())
    with SessionLocal() as db:
        state["tokens"] = [issue_session(b.id, APP_ID, b.openid)[0] for i in range(24)
            for b in [db.query(MemberWechatBinding).filter_by(openid="mixed-"+str(i)).one()]]
    # Caller passes directly over stdin into a private load container, never into logs.
    print(json.dumps(state))


def audit():
    configure()
    from app.database import SessionLocal
    from app.models import (InventoryBalance, PointsAccount, Order, PointsGrant, PointsLedgerEntry,
                            VoucherRecord, VoucherUploadBatch, ProductCategory)
    from app.mall.inventory_service import audit_inventory_balance
    from app.mall.points_ledger_service import audit_points_account_balance
    with SessionLocal() as db:
        accounts = db.query(PointsAccount).all()
        assert all(a.available_points == 100 and a.reserved_points == 0 for a in accounts)
        assert all(audit_points_account_balance(db,account_id=a.id).is_consistent for a in accounts)
        stocks = db.query(InventoryBalance).all()
        assert len(stocks)==4000 and all(s.reserved_quantity==0 for s in stocks)
        selected=json.loads(STATE.read_text())["sku"]
        assert all(s.on_hand_quantity==(12 if s.sku_id==selected else (s.sku_id%4+1)*10) for s in stocks)
        assert all(audit_inventory_balance(db,sku_id=s.sku_id).is_consistent for s in stocks)
        orders = db.query(Order).count()
        assert db.query(Order).filter(Order.status!="CANCELLED").count()==0
        assert db.query(PointsLedgerEntry).filter_by(entry_type="RESERVE").count()==orders
        assert db.query(PointsLedgerEntry).filter_by(entry_type="RELEASE").count()==orders
        assert db.query(PointsGrant).count()==len(accounts)
        assert len(accounts)==72 and orders==24
        batches=db.query(VoucherUploadBatch).all()
        assert all(b.failed_files==0 and b.success_files==1 for b in batches)
        vouchers=db.query(VoucherRecord).all()
        assert len(vouchers)==len(batches) and all(v.ocr_text.strip() for v in vouchers)
        assert len(vouchers)==4
        print(json.dumps(dict(result="PASS",accounts=len(accounts),grants=len(accounts),orders=orders,
            all_orders_cancelled=True,all_4000_inventory_balances_consistent=True,
            all_points_balances_consistent=True,duplicate_reservations=False,
            ocr_uploads=len(vouchers),ocr_nonempty=len(vouchers),
            maintenance_rows=db.query(ProductCategory).filter(ProductCategory.slug.like("mixed-%")).count())))


if __name__ == "__main__":
    import sys
    if not __debug__:
        raise RuntimeError("Assertions must remain enabled.")
    action=sys.argv[1]
    if action=="serve":
        guard()
        import uvicorn
        uvicorn.run("tools.mixed_load_fixture:create_app",factory=True,host="0.0.0.0",port=8000,
                    workers=int(os.environ["STAGING_WEB_WORKERS"]),access_log=False)
    else:
        {"seed":seed,"state":client_state,"audit":audit}[action]()
