"""Probe the real refund form on the disposable two-parcel rehearsal database."""
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tools.rehearse_member_fulfillment import FormFields, api


def database_snapshot():
    from app.database import engine
    from app.order_delivery_migration_check import connect_readonly, snapshot

    with closing(connect_readonly(Path(engine.url.database))) as connection:
        return snapshot(connection)


def check_refund(admin, member, oid, *, blocked):
    from app.database import SessionLocal
    from app.models import (AdminActionLog, InventoryBalance, InventoryMovement, Order, OrderItem,
                            PointsGrant, PointsLedgerEntry)

    page = "/mall-orders/" + oid
    action = page + "/refund"
    fields = FormFields(admin.get(page).text, action).values
    assert fields.get("csrf_token")
    original = api(member, "GET", "/orders/" + oid)
    assert original["status"] == "COMPLETED" and len(original["parcels"]) == 2
    before = database_snapshot()
    for attempt in range(2):
        response = admin.post(action, data=dict(fields, reason="虚拟双包裹整单退款"))
        target = urlsplit(response.headers["location"])
        assert response.status_code == 303 and target.path == page
        expected = {"error": ["订单已进入供应商结算，不能自动退款"]} if blocked else {"message": ["整单退款已完成"]}
        assert parse_qs(target.query) == expected
        after = database_snapshot()
        if blocked or attempt == 1:
            assert after == before, "Rejected or replayed refund changed the database"
        before = after
    detail = api(member, "GET", "/orders/" + oid)
    assert detail["parcels"] == original["parcels"]
    if blocked:
        assert detail == original
        return

    assert detail["status"] == "REFUNDED"
    summary = api(member, "GET", "/points/summary")
    assert (Decimal(summary["available_points"]), Decimal(summary["reserved_points"])) == (20, 0)
    ledger = api(member, "GET", "/points/ledger?available_only=true")
    visible_refunds = [Decimal(row["available_points_delta"]) for row in ledger["items"] if row["entry_type"] == "REFUND"]
    assert sorted(visible_refunds) == [Decimal("3.50"), Decimal("12.50")]
    with SessionLocal() as db:
        entries = (db.query(PointsLedgerEntry, PointsGrant).join(PointsGrant, PointsGrant.id == PointsLedgerEntry.grant_id)
                   .filter(PointsLedgerEntry.reference_type == "ORDER", PointsLedgerEntry.reference_id == oid)
                   .all())
        charges = {g.id: -e.reserved_points_delta for e, g in entries if e.entry_type == "CONSUME"}
        refunds = [(g.id, e.available_points_delta) for e, g in entries if e.entry_type == "REFUND"]
        assert len(refunds) == 2 and dict(refunds) == charges
        assert sorted(charges.values()) == [Decimal("3.50"), Decimal("12.50")]
        assert all(g.available_points == g.granted_points and g.reserved_points == 0 for _, g in entries)
        order = db.query(Order).filter_by(order_public_id=oid).one()
        items = db.query(OrderItem).filter_by(order_id=order.id).all()
        returns = db.query(InventoryMovement).filter_by(reference_type="ORDER", reference_id=oid, movement_type="RETURN").all()
        assert len(returns) == len(items) == 2
        assert {m.sku_id: m.quantity_delta for m in returns} == {i.sku_id: i.quantity for i in items}
        for item in items:
            stock = db.query(InventoryBalance).filter_by(sku_id=item.sku_id).one()
            assert (stock.on_hand_quantity, stock.reserved_quantity) == (2, 0)
        assert db.query(AdminActionLog).filter_by(action_type="mall_order_refund", target_id=order.id,
                                                   target_type="mall_order").count() == 1
