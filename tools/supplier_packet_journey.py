"""Check per-supplier packet preparation inside the isolated activation rehearsal."""
import json
import secrets
from urllib.parse import urlsplit, parse_qs

import httpx

from tools.activated_order_journey import SKU
from tools.parcel_settlement_journey import check_settlements, verify_resources
from tools.rehearse_member_fulfillment import ADDRESS, FormFields, admin_action, api


def check_order_waybill(admin, member, oid, expected_items):
    """Register one cup and two tea bags under the same redemption order."""
    from app.database import SessionLocal
    from app.models import Order, OrderItem

    path = "/mall-orders/" + oid
    page = admin.get(path)
    assert page.status_code == 200
    csrf = FormFields(page.text, path + "/parcel").values["csrf_token"]
    with SessionLocal() as db:
        order = db.query(Order).filter_by(order_public_id=oid).one()
        lines = {line.product_name_snapshot: line.id for line in db.query(OrderItem).filter_by(order_id=order.id)}
    cup, tea = lines["会员测试杯"], lines["测试茶包"]

    def post(suffix, **data):
        res = admin.post(path + suffix, data=dict(csrf_token=csrf, **data))
        assert res.status_code == 303
        return parse_qs(urlsplit(res.headers["location"]).query)

    def ship(number, ids, amounts):
        return post("/parcel", shipping_carrier="联调物流", tracking_number=number, item_ids=ids, amounts=amounts)

    assert "error" in post("/ship", shipping_carrier="联调物流", tracking_number="ALL")
    assert "error" in ship("MIXED", [cup, tea], [1, 1])
    assert "message" in ship("PARCEL-A", [cup], [1])
    assert "message" in ship("PARCEL-A", [cup], [1])
    assert "error" in ship("OVER", [cup], [1])
    assert "error" in ship("PARCEL-A", [tea], [2])
    partial = api(member, "GET", "/orders/" + oid)
    listing = api(member, "GET", "/orders")
    assert next(i for i in listing["items"] if i["order_public_id"] == oid)["status"] == "PARTIALLY_SHIPPED"
    assert partial["status"] == "PARTIALLY_SHIPPED" and len(partial["parcels"]) == 1
    verify_resources(member, oid)
    assert partial["parcels"][0]["items"] == [dict(product_name="会员测试杯", sku_name="标准款", quantity=1)]
    assert "部分发货" in admin.get(path).text
    # Reload after rendering the page so its form token matches the current cookie.
    csrf = FormFields(admin.get(path).text, path + "/parcel").values["csrf_token"]
    assert "message" in ship("PARCEL-B", [tea], [2])
    assert "message" in ship("PARCEL-A", [cup], [1])
    shipped = api(member, "GET", "/orders/" + oid)
    assert shipped["status"] == "SHIPPED" and shipped["order_public_id"] == oid
    verify_resources(member, oid)
    assert [p["tracking_number"] for p in shipped["parcels"]] == ["PARCEL-A", "PARCEL-B"]
    assert shipped["parcels"][1]["items"] == [dict(product_name="测试茶包", sku_name="独立装", quantity=2)]
    assert all("supplier" not in str(parcel) for parcel in shipped["parcels"])
    assert {i["product_name"]: i["quantity"] for i in shipped["items"]} == expected_items
    admin_action(admin, path, path + "/complete")
    assert api(member, "GET", "/orders/" + oid)["status"] == "COMPLETED"
    return dict(order_count=1, parcel_count=2, quantities=[1, 2], first_status=partial["status"],
                final_shipping_status=shipped["status"], duplicate_replay=True, mixed_suppliers_rejected=True,
                overshipment_rejected=True, completion_verified=True)


def check_supplier_packets(admin, member, profile, first_product, *, refund_first=False):
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
                                 sku_code="TEA-TEST", name="独立装", points_price="4.00", cost_price="1.50").entity
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
    other_prepare = f"{path}/supplier-packets/{second_id}/prepare"
    other_confirm = f"{path}/supplier-packets/{second_id}/confirm"
    assert admin.post(other_prepare, data=form(other_prepare)).status_code == 200
    other_handoff = admin.post(other_confirm, data=dict(form(other_confirm), confirmed="yes"))
    assert other_handoff.status_code == 303 and other_handoff.headers["location"] == path

    # A current catalog reassignment must not change who fulfills this order.
    with SessionLocal() as db:
        update_product_sku(db, actor_admin_id=actor, sku_id=sku_pk, supplier_id=outside_pk,
                           name="后续规格", cost_price="9.99")
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
    # Restore eligibility before probing shipping so the second waybill failure
    # cannot be attributed to the supplier being disabled.
    with SessionLocal() as db:
        update_supplier(db, actor_admin_id=actor, supplier_id=second_pk, is_active=True)
        db.commit()
    verify(second_id, tea)
    shipping = check_order_waybill(admin, member, oid, {cup["product_name"]: 1, tea["product_name"]: 2})
    denied(first_id, cached)
    settlement = check_settlements(admin, member, oid, (first_id, second_id, outside_id), refund_first=refund_first)
    return_action = f"{path}/supplier-packets/{first_id}/receipt"
    delete_action = f"{path}/supplier-packets/{second_id}/receipt"
    with httpx.Client(base_url=admin.base_url, trust_env=False) as peer:
        assert peer.post(return_action, data={}).headers["location"] == "/login"
        assert peer.post("/login", data=dict(username="test-handoff-peer", password=password)).headers["location"] == "/dashboard"
        fields = FormFields(peer.get(path).text, return_action).values
        returned = dict(fields, confirmed="yes", outcome="returned", reference="DEMO-RETURN-01")
        for _ in range(2):
            result = peer.post(return_action, data=returned)
            assert result.status_code == 303 and "message" in parse_qs(urlsplit(result.headers["location"]).query)
    deleted = dict(form(delete_action), confirmed="yes", outcome="deleted", reference="DEMO-DELETE-02")
    assert deleted["handoff_id"] != returned["handoff_id"]
    denied_post(delete_action, dict(deleted, csrf_token="invalid"))
    denied_post(delete_action, dict(deleted, confirmed=""))
    denied_post(delete_action, dict(deleted, handoff_id=returned["handoff_id"]))
    for _ in range(2):
        result = admin.post(delete_action, data=deleted)
        assert result.status_code == 303 and "message" in parse_qs(urlsplit(result.headers["location"]).query)
    denied_post(delete_action, dict(deleted, reference="DEMO-CONFLICT"))
    page = admin.get(path)
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    for ref, operator in [("DEMO-RETURN-01","test-handoff-peer"),("DEMO-DELETE-02","test-issuer")]:
        assert page.text.count(ref) == 1 and f"{ref} · 登记人 {operator}" in page.text
    from app.mall.supplier_handoff import packet_history, receipts_by_handoff
    from app.time_utils import format_utc8
    disposition_trace = []
    with SessionLocal() as db:
        history = packet_history(db, oid)
        receipts = receipts_by_handoff(db, history)
        for supplier, outcome, fields in [(first_id, "returned", returned), (second_id, "deleted", deleted)]:
            handoff, = [row for row in history if row.supplier == supplier]
            group = receipts[handoff.handoff_id]
            assert handoff.handoff_id == int(fields["handoff_id"]) and set(group) == {outcome}
            receipt = group[outcome]
            assert receipt.description == fields["reference"]
            display = f"登记人 {receipt.username}（#{receipt.admin_id}） · 登记时间 {format_utc8(receipt.created_at)}"
            assert display in page.text
            disposition_trace.append(dict(order=oid, supplier=supplier, preparation_id=handoff.id,
                                          handoff_id=handoff.handoff_id, outcome=outcome, reference=receipt.description,
                                          operator=receipt.username, operator_id=receipt.admin_id,
                                          registered_at_beijing=format_utc8(receipt.created_at)))
    member_view = str(api(member, "GET", "/orders/" + oid))
    assert all(fields["reference"] not in member_view for fields in (returned, deleted))
    return dict(participating_suppliers=2, authorized_quantities=[1, 2], order_address_snapshot=True,
                disposition_receipts=dict(returned=1, deleted=1, supplier_handoff_actor_time_verified=True,
                                          duplicate_replays=True, conflicting_reference_rejected=True, member_view_isolated=True,
                                          trace=disposition_trace),
                shipping=shipping, settlement=settlement,
                outsider_denied=True, catalog_reassignment_isolated=True, revoked_profile_denied=True,
                inactive_supplier_denied=True, stale_order_denied=True, other_operator_confirmation_denied=True, operator_prepare_and_handoff_checked=True, external_dispatch=False)
