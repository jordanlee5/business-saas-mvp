"""Check per-supplier packet preparation inside the isolated activation rehearsal."""
import json
import secrets
from urllib.parse import urlsplit, parse_qs

import httpx

from tools.activated_order_journey import SKU
from tools.rehearse_member_fulfillment import ADDRESS, FormFields, admin_action, api


def check_supplier_packets(admin, member, profile, first_product):
    from app.database import SessionLocal
    from app.mall.catalog_service import (create_product, create_product_sku, create_supplier,
                                          publish_product, update_product_sku, update_supplier)
    from app.mall.fulfillment_notice import supplier_packet
    from app.models import Order, Product, User

    config = json.loads(profile.read_text("utf-8"))
    first_id, = config["suppliers"]
    with SessionLocal() as db:
        actor = db.query(User).filter_by(username="test-issuer").one().id
        category_id = db.query(Product.category_id).filter_by(product_public_id=first_product).scalar()
        second = create_supplier(db, actor_admin_id=actor, name="联调第二供应商").entity
        outsider = create_supplier(db, actor_admin_id=actor, name="联调第三供应商").entity
        product = create_product(db, actor_admin_id=actor, category_id=category_id, name="测试茶包").entity
        sku = create_product_sku(db, actor_admin_id=actor, product_id=product.id, supplier_id=second.id,
                                 sku_code="TEA-TEST", name="独立装", points_price="4.00", cost_price="1.00").entity
        publish_product(db, actor_admin_id=actor, product_id=product.id)
        for supplier in (second, outsider):
            config["suppliers"][supplier.supplier_public_id] = dict(enabled=True, name=supplier.name,
                                                                    agreement_ref="test-only")
        second_id, outside_id = second.supplier_public_id, outsider.supplier_public_id
        second_pk, outside_pk = second.id, outsider.id
        sku_pk, product_public_id = sku.id, product.product_public_id
        db.commit()
    profile.write_text(json.dumps(config), encoding="utf-8")
    admin_action(admin, "/mall-inventory", f"/mall-inventory/skus/{sku_pk}/receive",
                 quantity=2, reason="隔离代发测试入库")
    lines = [dict(product_public_id=first_product, sku_code=SKU, quantity=1, expected_points_price="8.00"),
             dict(product_public_id=product_public_id, sku_code="TEA-TEST", quantity=2, expected_points_price="4.00")]
    address = api(member, "POST", "/addresses", 201, json=ADDRESS)
    notice = api(member, "POST", "/orders/fulfillment-preview", json={"lines": lines})
    assert notice["ready"] and {g["supplier_public_id"] for g in notice["suppliers"]} == {first_id, second_id}
    order = api(member, "POST", "/orders", 201, json=dict(request_id=secrets.token_hex(16), lines=lines,
                address_public_id=address["address_public_id"], expected_address=ADDRESS, fulfillment_digest=notice["digest"]))
    oid = order["order_public_id"]
    assert order["total_points"] == "16.00"
    api(member, "PUT", "/addresses/" + address["address_public_id"], json={**ADDRESS, "detail": "下单后修改的地址"})
    path = "/mall-orders/" + oid

    def packet(public_id, cached=None):
        with SessionLocal() as db:
            target = cached if cached is not None else db.query(Order).filter_by(order_public_id=oid).one()
            return supplier_packet(db, target, public_id)

    def denied(public_id, cached=None):
        try:
            packet(public_id, cached)
        except ValueError:
            return
        raise AssertionError("Unexpected eligibility for a supplier packet")

    def verify(public_id, item):
        expected = dict(order_public_id=oid, address=ADDRESS, items=[item])
        assert packet(public_id) == expected, "Supplier packet fields or line ownership differ"

    cup = dict(product_name="会员测试杯", sku_name="标准款", quantity=1)
    tea = dict(product_name="测试茶包", sku_name="独立装", quantity=2)
    denied(first_id)
    admin_action(admin, path, path + "/fulfill")
    verify(first_id, cup)
    verify(second_id, tea)
    denied(outside_id)

    prepare = f"{path}/supplier-packets/{first_id}/prepare"
    confirm = f"{path}/supplier-packets/{first_id}/confirm"
    def form(action):
        page = admin.get(path)
        assert page.status_code == 200
        return FormFields(page.text, action).values
    def denied_post(action, data):
        result = admin.post(action, data=data)
        assert result.status_code == 303 and "error" in parse_qs(urlsplit(result.headers["location"]).query)
    denied_post(prepare, {})
    fields = form(prepare)
    denied_post(f"{path}/supplier-packets/{outside_id}/prepare", fields)
    view = admin.post(prepare, data=fields)
    assert view.status_code == 200 and "no-store" in view.headers["cache-control"]
    assert cup["product_name"] in view.text and tea["product_name"] not in view.text
    assert "尚无交付登记" in admin.get(path).text
    fields = form(confirm)
    # A second authorized operator can prepare their own packet, but cannot
    # attest to what the first operator did, even with a valid form token.
    from app.admin_permissions import OPERATOR
    from app.auth import get_password_hash
    password = secrets.token_urlsafe(24)
    with SessionLocal() as db:
        db.add(User(username="test-handoff-peer", role="admin", admin_level=OPERATOR,
                    password_hash=get_password_hash(password), is_active=True))
        db.commit()
    with httpx.Client(base_url=admin.base_url, trust_env=False) as peer:
        guest = peer.post(prepare, data={})
        assert guest.status_code == 302 and guest.headers["location"] == "/login"
        login = peer.post("/login", data=dict(username="test-handoff-peer", password=password))
        assert login.status_code == 302 and login.headers["location"] == "/dashboard"
        peer_page = peer.get(path)
        assert not FormFields(peer_page.text, confirm).values
        peer_token = FormFields(peer_page.text, prepare).values["csrf_token"]
        forged = peer.post(confirm, data=dict(fields, csrf_token=peer_token, confirmed="yes"))
        assert forged.status_code == 303 and "error" in parse_qs(urlsplit(forged.headers["location"]).query)
    denied_post(confirm, fields)
    denied_post(f"{path}/supplier-packets/{second_id}/confirm", dict(fields, confirmed="yes"))
    for _ in range(2):
        result = admin.post(confirm, data=dict(fields, confirmed="yes"))
        assert result.status_code == 303 and result.headers["location"] == path
    assert admin.get(path).text.count("经办人已登记线下交付") == 1

    # A current catalog reassignment must not change who fulfills this order.
    with SessionLocal() as db:
        update_product_sku(db, actor_admin_id=actor, sku_id=sku_pk, supplier_id=outside_pk,
                           name="后续规格")
        db.commit()
    verify(second_id, tea)
    denied(outside_id)

    config["suppliers"][first_id]["enabled"] = False
    profile.write_text(json.dumps(config), encoding="utf-8")
    denied(first_id)
    verify(second_id, tea)
    config["suppliers"][first_id]["enabled"] = True
    profile.write_text(json.dumps(config), encoding="utf-8")

    with SessionLocal() as db:
        update_supplier(db, actor_admin_id=actor, supplier_id=second_pk, is_active=False)
        db.commit()
        cached = db.query(Order).filter_by(order_public_id=oid).one()
    denied(second_id)
    verify(first_id, cup)
    admin_action(admin, path, path + "/ship", shipping_carrier="联调物流", tracking_number="SPLIT-TEST-01")
    assert api(member, "GET", "/orders/" + oid)["status"] == "SHIPPED"
    denied(first_id, cached)
    return dict(participating_suppliers=2, authorized_quantities=[1, 2], order_address_snapshot=True,
                outsider_denied=True, catalog_reassignment_isolated=True, revoked_profile_denied=True,
                inactive_supplier_denied=True, stale_order_denied=True, other_operator_confirmation_denied=True, operator_prepare_and_handoff_checked=True, external_dispatch=False)
