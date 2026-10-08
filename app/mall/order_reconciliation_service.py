"""已完成商城订单的积分、库存、订单与供应商结算只读核对。"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_

from .domain import SupplierSettlementStatus
from .inventory_service import assert_inventory_balance_consistent
from .order_lifecycle_service import (
    ORDER_COMPLETED_STATUS,
    _load_and_validate_fulfillment_evidence,
    _validate_completion_evidence,
    _validate_shipping_evidence,
)
from .points_ledger_service import assert_points_account_balance_consistent
from .supplier_settlement_reporting_service import get_supplier_settlement_detail


ZERO = Decimal("0.00")


@dataclass(frozen=True)
class CompletedOrderReconciliationLine:
    order_item_id: int
    sku_id: int
    quantity: int
    line_points: Decimal
    line_cost_amount: Decimal
    settlement_public_id: str | None
    settlement_status: str | None


@dataclass(frozen=True)
class CompletedOrderReconciliation:
    order_id: int
    order_public_id: str
    total_points: Decimal
    total_quantity: int
    total_cost_amount: Decimal
    pending_cost_amount: Decimal
    confirmed_cost_amount: Decimal
    unbatched_cost_amount: Decimal
    lines: tuple[CompletedOrderReconciliationLine, ...]


def reconcile_completed_order(db, *, order_public_id) -> CompletedOrderReconciliation:
    """核对四账；调用方应在一致的数据库读取快照中使用干净的 Session。"""
    from ..models import (
        Order,
        OrderItem,
        PointsAccount,
        SupplierSettlementBatch,
        SupplierSettlementItem,
    )

    if not isinstance(order_public_id, str) or not order_public_id.strip():
        raise ValueError("订单编号不能为空")
    normalized_id = order_public_id.strip()
    if len(normalized_id) > 32:
        raise ValueError("订单编号不能超过 32 个字符")
    if db.new or db.dirty or db.deleted:
        raise ValueError("订单核对需要无待写入变更的数据库会话")

    with db.no_autoflush:
        order = db.query(Order).filter(
            Order.order_public_id == normalized_id
        ).one_or_none()
        if order is None:
            raise ValueError("订单不存在")
        if order.status != ORDER_COMPLETED_STATUS:
            raise ValueError("仅支持已完成订单的四账核对")
        if order.refund_reason is not None or order.refunded_at is not None:
            raise RuntimeError("已完成订单含退款证据")

        items = tuple(
            db.query(OrderItem).filter(OrderItem.order_id == order.id)
            .order_by(OrderItem.id.asc()).all()
        )
        _load_and_validate_fulfillment_evidence(db, order=order)
        _validate_shipping_evidence(db, order=order, expect_shipped=True)
        _validate_completion_evidence(db, order=order, expect_completed=True)

        account = db.query(PointsAccount).filter(
            PointsAccount.member_id == order.member_id
        ).one_or_none()
        if account is None:
            raise RuntimeError("订单会员积分账户不存在")
        assert_points_account_balance_consistent(db, account_id=account.id)
        for sku_id in sorted({item.sku_id for item in items}):
            assert_inventory_balance_consistent(db, sku_id=sku_id)

        item_by_id = {item.id: item for item in items}
        settlement_rows = tuple(
            db.query(SupplierSettlementItem).filter(
                or_(
                    SupplierSettlementItem.order_id == order.id,
                    SupplierSettlementItem.order_item_id.in_(item_by_id),
                )
            ).order_by(SupplierSettlementItem.id.asc()).all()
        )
        if any(
            row.order_id != order.id or row.order_item_id not in item_by_id
            for row in settlement_rows
        ):
            raise RuntimeError("订单与供应商结算项的关联不一致")
        batch_ids = {row.settlement_batch_id for row in settlement_rows}
        batches = (
            db.query(SupplierSettlementBatch).filter(
                SupplierSettlementBatch.id.in_(batch_ids)
            ).all() if batch_ids else ()
        )
        batch_by_id = {batch.id: batch for batch in batches}
        if len(batch_by_id) != len(batch_ids):
            raise RuntimeError("供应商结算批次不存在")
        verified_ids = set()
        for batch in batches:
            detail = get_supplier_settlement_detail(
                db, settlement_public_id=batch.settlement_public_id
            )
            verified_ids.update(item.settlement_item_id for item in detail.items)
        if any(row.id not in verified_ids for row in settlement_rows):
            raise RuntimeError("供应商结算项缺少批次核对证据")

        row_by_item_id = {row.order_item_id: row for row in settlement_rows}
        if len(row_by_item_id) != len(settlement_rows):
            raise RuntimeError("订单项被重复计入供应商结算")
        pending = confirmed = unbatched = ZERO
        lines = []
        for item in items:
            row = row_by_item_id.get(item.id)
            batch = batch_by_id[row.settlement_batch_id] if row else None
            if batch is None:
                unbatched += item.line_cost_amount
            elif batch.status == SupplierSettlementStatus.PENDING_CONFIRMATION.value:
                pending += item.line_cost_amount
            elif batch.status == SupplierSettlementStatus.CONFIRMED.value:
                confirmed += item.line_cost_amount
            else:
                raise RuntimeError("供应商结算状态异常")
            lines.append(CompletedOrderReconciliationLine(
                order_item_id=item.id,
                sku_id=item.sku_id,
                quantity=item.quantity,
                line_points=item.line_points,
                line_cost_amount=item.line_cost_amount,
                settlement_public_id=batch.settlement_public_id if batch else None,
                settlement_status=batch.status if batch else None,
            ))
        if pending + confirmed + unbatched != order.total_cost_amount:
            raise RuntimeError("订单成本与供应商结算分布不一致")
        return CompletedOrderReconciliation(
            order_id=order.id,
            order_public_id=order.order_public_id,
            total_points=order.total_points,
            total_quantity=order.total_quantity,
            total_cost_amount=order.total_cost_amount,
            pending_cost_amount=pending,
            confirmed_cost_amount=confirmed,
            unbatched_cost_amount=unbatched,
            lines=tuple(lines),
        )
