"""Redemption leg of the isolated member activation rehearsal."""
from collections import Counter
from decimal import Decimal
import json
import secrets

from tools.rehearse_member_fulfillment import ADDRESS, admin_action, api

SKU = "NEW-MEMBER-SKU"


def prepare_catalog(profile):
    from app.database import SessionLocal
    from app.mall.catalog_service import (
        create_product, create_product_category, create_product_sku, create_supplier, publish_product,
    )
    from app.models import User

    with SessionLocal() as db:
        actor = db.query(User).filter_by(username="test-issuer").one().id
        category = create_product_category(db, actor_admin_id=actor, name="联调好物", slug="new-member").entity
        supplier = create_supplier(db, actor_admin_id=actor, name="测试代发方").entity
        product = create_product(db, actor_admin_id=actor, category_id=category.id, name="会员测试杯").entity
        sku = create_product_sku(db, actor_admin_id=actor, product_id=product.id, supplier_id=supplier.id,
                                 sku_code=SKU, name="标准款", points_price="8.00", cost_price="2.00").entity
        publish_product(db, actor_admin_id=actor, product_id=product.id)
        config = {"operator_name": "联调运营主体", "operator_contact": "help@example.invalid",
                  "suppliers": {supplier.supplier_public_id: {
                      "enabled": True, "agreement_ref": "test-only", "name": "联调发货方"}}}
        profile.write_text(json.dumps(config), encoding="utf-8")
        product_id, sku_id = product.product_public_id, sku.id
        db.commit()
        return product_id, sku_id


def redeem_and_cancel(admin, member, product_id, sku_id, public_member_id):
    from app.database import SessionLocal
    from app.mall.inventory_service import audit_inventory_balance
    from app.models import (InventoryBalance, InventoryMovement, Member, Order, PointsAccount,
                            PointsGrant, PointsLedgerEntry)

    admin_action(admin, "/mall-inventory", f"/mall-inventory/skus/{sku_id}/receive",
                 quantity=2, reason="同一会员兑换联调")
    history = []
    order_id = None

    def checkpoint(stage, status):
        reserved = 16 if status == "CREATED" else 0
        available = 20 - reserved
        assert api(member, "GET", "/auth/me")["member_public_id"] == public_member_id
        summary = api(member, "GET", "/points/summary")
        assert (Decimal(summary["available_points"]), Decimal(summary["reserved_points"])) == (available, reserved)
        ledger = api(member, "GET", "/points/ledger?available_only=true")
        expected = Counter({("GRANT", Decimal("12.50")): 1, ("GRANT", Decimal("7.50")): 1})
        listing = api(member, "GET", "/orders")
        assert listing["total"] == int(order_id is not None)
        with SessionLocal() as db:
            account = db.query(PointsAccount).join(Member).filter(Member.member_public_id == public_member_id).one()
            grants = db.query(PointsGrant).filter_by(account_id=account.id).order_by(PointsGrant.id).all()
            assert [g.granted_points for g in grants] == [Decimal("12.50"), Decimal("7.50")]
            assert sum(g.reserved_points for g in grants) == reserved
            assert all(g.available_points + g.reserved_points == g.granted_points for g in grants)
            if order_id:
                # The earlier 12.50 grant is used first; 3.50 comes from the second grant.
                for grant, amount in zip(grants, (Decimal("12.50"), Decimal("3.50"))):
                    assert grant.reserved_points == (amount if reserved else 0)
                    expected[("RESERVE", -amount)] += 1
                    changes = [("RESERVE", -amount, amount)]
                    if status == "CANCELLED":
                        expected[("RELEASE", amount)] += 1
                        changes.append(("RELEASE", amount, -amount))
                    entries = db.query(PointsLedgerEntry).filter_by(
                        grant_id=grant.id, reference_id=order_id).order_by(PointsLedgerEntry.id).all()
                    assert [(e.entry_type, e.available_points_delta, e.reserved_points_delta)
                            for e in entries] == changes, stage
            assert ledger["total"] == sum(expected.values())
            assert Counter((row["entry_type"], Decimal(row["available_points_delta"]))
                           for row in ledger["items"]) == expected
            balance = db.query(InventoryBalance).filter_by(sku_id=sku_id).one()
            held_units = 2 if reserved else 0
            assert (balance.on_hand_quantity, balance.reserved_quantity) == (2, held_units)
            assert audit_inventory_balance(db, sku_id=sku_id).is_consistent
            if order_id:
                order = db.query(Order).filter_by(order_public_id=order_id, member_id=account.member_id).one()
                assert order.status == status
                movements = db.query(InventoryMovement).filter_by(sku_id=sku_id, reference_id=order_id).all()
                assert sorted(m.reserved_quantity_delta for m in movements) == ([-2, 2] if status == "CANCELLED" else [2])
        if order_id:
            assert [(item["order_public_id"], item["status"]) for item in listing["items"]] == [(order_id, status)]
            detail = api(member, "GET", "/orders/" + order_id)
            assert detail["total_points"] == "16.00" and detail["address"] == ADDRESS
            admin_page = admin.get("/mall-orders/" + order_id)
            assert admin_page.status_code == 200 and order_id in admin_page.text and ADDRESS["detail"] in admin_page.text
            label = "已取消" if status == "CANCELLED" else "待处理"
            assert f"<dt>状态</dt><dd>{label}</dd>" in admin_page.text
        product = api(member, "GET", "/products/" + product_id)
        assert product["skus"][0]["in_stock"] is (reserved == 0)
        history.append(dict(stage=stage, available_points=available, reserved_points=reserved,
                            grant_available=[str(g.available_points) for g in grants],
                            order_count=listing["total"], order_status=status,
                            stock_on_hand=2, stock_reserved=held_units, visible_ledger_rows=ledger["total"]))

    checkpoint("activated_balance", None)
    product = api(member, "GET", "/products/" + product_id)
    sku = next(s for s in product["skus"] if s["sku_code"] == SKU)
    assert sku["points_price"] == "8.00"
    address = api(member, "POST", "/addresses", 201, json=ADDRESS)
    lines = [dict(product_public_id=product_id, sku_code=SKU, quantity=2, expected_points_price=sku["points_price"])]
    notice = api(member, "POST", "/orders/fulfillment-preview", json={"lines": lines})
    assert notice["ready"]
    body = dict(request_id=secrets.token_hex(16), address_public_id=address["address_public_id"],
                expected_address=ADDRESS, lines=lines, fulfillment_digest=notice["digest"])
    order_id = api(member, "POST", "/orders", 201, json=body)["order_public_id"]
    checkpoint("submitted", "CREATED")
    assert api(member, "POST", "/orders", json=body)["order_public_id"] == order_id
    checkpoint("submit_replayed", "CREATED")
    for stage in ("cancelled", "cancel_replayed"):
        cancelled = api(member, "POST", "/orders/" + order_id + "/cancel")
        assert cancelled["status"] == "CANCELLED" and cancelled["order_public_id"] == order_id
        checkpoint(stage, "CANCELLED")
    late_retry = api(member, "POST", "/orders", json=body)
    assert (late_retry["order_public_id"], late_retry["status"]) == (order_id, "CANCELLED")
    checkpoint("submit_replayed_after_cancel", "CANCELLED")
    return history
