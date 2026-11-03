"""Reconcile the two-parcel fixture through the local back-office settlement forms."""
from datetime import timedelta
from decimal import Decimal
import secrets
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from tools.rehearse_member_fulfillment import FormFields, api
from tools.parcel_refund_journey import check_refund


def verify_resources(member, oid):
    from app.database import SessionLocal
    from app.models import InventoryBalance, InventoryMovement, Order, OrderItem, PointsLedgerEntry

    summary = api(member, "GET", "/points/summary")
    assert (Decimal(summary["available_points"]), Decimal(summary["reserved_points"])) == (4, 0)
    with SessionLocal() as db:
        entries = db.query(PointsLedgerEntry).filter_by(reference_type="ORDER", reference_id=oid)
        assert sorted((e.entry_type, e.available_points_delta, e.reserved_points_delta) for e in entries) == [
            ("CONSUME", 0, Decimal("-12.50")), ("CONSUME", 0, Decimal("-3.50")),
            ("RESERVE", Decimal("-12.50"), Decimal("12.50")), ("RESERVE", Decimal("-3.50"), Decimal("3.50"))]
        items = db.query(OrderItem).join(Order).filter(Order.order_public_id == oid).all()
        assert len(items) == 2
        expected = {"会员测试杯": (1, 1), "测试茶包": (2, 0)}
        for item in items:
            quantity, remaining = expected[item.product_name_snapshot]
            balance = db.query(InventoryBalance).filter_by(sku_id=item.sku_id).one()
            assert (item.quantity, balance.on_hand_quantity, balance.reserved_quantity) == (quantity, remaining, 0)
            movements = db.query(InventoryMovement).filter_by(sku_id=item.sku_id, reference_type="ORDER", reference_id=oid)
            assert sorted((m.movement_type, m.quantity_delta, m.reserved_quantity_delta) for m in movements) == [
                ("OUTBOUND", -quantity, -quantity), ("RESERVE", 0, quantity)]


def check_settlements(admin, member, oid, supplier_ids, *, refund_first=False):
    from app.admin_permissions import SUPER_ADMIN
    from app.auth import get_password_hash
    from app.database import SessionLocal
    from app.mall.order_reconciliation_service import reconcile_completed_order
    from app.models import Order, Supplier, SupplierSettlementBatch, SupplierSettlementItem, User

    def amounts(unbatched, pending, confirmed):
        with SessionLocal() as db:
            report = reconcile_completed_order(db, order_public_id=oid)
            assert (report.total_points, report.total_quantity, report.total_cost_amount) == (16, 3, 5)
            assert (report.unbatched_cost_amount, report.pending_cost_amount, report.confirmed_cost_amount) == (
                unbatched, pending, confirmed)

    password = secrets.token_urlsafe(24)
    with SessionLocal() as db:
        completed = db.query(Order.completed_at).filter_by(order_public_id=oid).scalar()
        supplier_pks = {s.supplier_public_id: s.id for s in db.query(Supplier)}
        db.add(User(username="test-settlement-reviewer", role="admin", admin_level=SUPER_ADMIN,
                    password_hash=get_password_hash(password), is_active=True))
        db.commit()
    start = completed.replace(hour=0, minute=0, second=0, microsecond=0)
    end = completed.replace(second=0, microsecond=0) + timedelta(minutes=1)

    def generate(supplier):
        action = "/mall-settlements/generate"
        fields = FormFields(admin.get("/mall-settlements").text, action).values
        result = admin.post(action, data=dict(fields, supplier_id=supplier_pks[supplier],
                            period_start=start.strftime("%Y-%m-%dT%H:%M"), period_end=end.strftime("%Y-%m-%dT%H:%M")))
        assert result.status_code == 303
        return urlsplit(result.headers["location"])

    amounts(5, 0, 0)
    cup, tea, outsider = supplier_ids
    # Move only the fixture's settlement clock beyond the minute-granularity cutoff.
    with patch("app.mall.supplier_settlement_service.utc8_now", return_value=end + timedelta(minutes=1)):
        if refund_first:
            check_refund(admin, member, oid, blocked=False)
            for supplier in supplier_ids:
                assert parse_qs(generate(supplier).query)["error"] == ["结算区间内没有未结算的已完成订单项"]
            with SessionLocal() as db:
                assert db.query(SupplierSettlementBatch).count() == db.query(SupplierSettlementItem).count() == 0
            return dict(refunded_points="16.00", original_grant_credits=["12.50", "3.50"], member_available="20.00",
                        returned_units=[1, 2], remaining_stock=[2, 2], parcel_count=2, settlement_batches=0,
                        duplicate_refund_unchanged=True)

        def generate_checked(supplier, name, quantity, cost):
            target = generate(supplier)
            assert target.path.startswith("/mall-settlements/STL-") and not target.query
            page = admin.get(target.path)
            assert page.status_code == 200 and oid in page.text and name in page.text
            with SessionLocal() as db:
                batch = db.query(SupplierSettlementBatch).filter_by(settlement_public_id=target.path.split("/")[-1]).one()
                assert (batch.supplier_id, batch.order_count, batch.item_count, batch.total_quantity,
                        batch.total_cost_amount) == (supplier_pks[supplier], 1, 1, quantity, cost)
                line = db.query(SupplierSettlementItem).filter_by(settlement_batch_id=batch.id).one()
                assert (line.order_public_id_snapshot, line.product_name_snapshot, line.line_cost_amount) == (oid, name, cost)
            return target.path
        with httpx.Client(base_url=admin.base_url, trust_env=False) as reviewer:
            login = reviewer.post("/login", data=dict(username="test-settlement-reviewer", password=password))
            assert login.status_code == 302 and login.headers["location"] == "/dashboard"
            for supplier, name, quantity, cost in [(cup, "会员测试杯", 1, 2), (tea, "测试茶包", 2, 3)]:
                path = generate_checked(supplier, name, quantity, cost)
                if supplier == cup:
                    amounts(3, 2, 0)
                    check_refund(admin, member, oid, blocked=True)
                else:
                    amounts(0, 3, 2)
                action = path + "/confirm"
                fields = FormFields(reviewer.get(path).text, action).values
                for _ in range(2):
                    response = reviewer.post(action, data=fields)
                    assert response.status_code == 303 and response.headers["location"] == path
                if supplier == cup:
                    amounts(3, 0, 2)
                    check_refund(admin, member, oid, blocked=True)
                else:
                    amounts(0, 0, 5)
        for supplier in supplier_ids:
            assert parse_qs(generate(supplier).query)["error"] == ["结算区间内没有未结算的已完成订单项"]
        with SessionLocal() as db:
            assert db.query(SupplierSettlementBatch).count() == 2
            assert db.query(SupplierSettlementBatch).filter_by(supplier_id=supplier_pks[outsider]).count() == 0
    verify_resources(member, oid)
    return dict(member_available="4.00", order_points="16.00", outbound_quantities=[1, 2],
                remaining_stock=[1, 0], confirmed_supplier_costs=["2.00", "3.00"], total_cost="5.00",
                duplicate_generation_blocked=True, unrelated_supplier_excluded=True,
                partial_pending_refund_blocked=True, partial_confirmed_refund_blocked=True,
                rejected_refunds_unchanged=True, payment_executed=False)
