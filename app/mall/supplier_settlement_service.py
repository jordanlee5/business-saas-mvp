"""供应商结算批次及订单项成本快照原子生成服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import secrets

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import SupplierSettlementStatus


ORDER_COMPLETED_STATUS = "COMPLETED"
SETTLEMENT_GENERATE_PERMISSION_MESSAGE = "当前账号无权生成供应商结算"
SETTLEMENT_PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SETTLEMENT_PUBLIC_ID_RANDOM_LENGTH = 16
SETTLEMENT_PUBLIC_ID_GENERATION_ATTEMPTS = 16
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class SupplierSettlementGenerationResult:
    """一次成功生成的待确认供应商结算批次。"""

    batch_id: int
    settlement_public_id: str
    supplier_id: int
    supplier_public_id_snapshot: str
    supplier_name_snapshot: str
    status: str
    period_start: datetime
    period_end: datetime
    order_count: int
    item_count: int
    total_quantity: int
    total_cost_amount: Decimal
    generated_by_admin_id: int
    generated_at: datetime
    settlement_item_ids: tuple[int, ...]
    action_log_id: int


def _normalize_positive_integer(value, *, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name}必须是正整数")
    return value


def _database_time(db, value):
    if db.get_bind().dialect.name == "postgresql":
        return value.replace(tzinfo=UTC8_TIMEZONE)
    return value


def _normalize_time(db, value, *, field_name):
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name}无效")
    if value.tzinfo is not None and value.utcoffset() is not None:
        value = value.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, value)


def _current_time(db, value):
    current = utc8_now() if value is None else value
    return _normalize_time(db, current, field_name="结算生成时间")


def _time_key(value):
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return value


def _require_actor(db, *, actor_admin_id):
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import User

    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(SETTLEMENT_GENERATE_PERMISSION_MESSAGE)
    actor = (
        db.query(User)
        .filter(User.id == actor_admin_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if (
        actor is None
        or actor.is_active is not True
        or not can_perform_mall_audit_action(
            actor,
            MallAuditActionType.SUPPLIER_SETTLEMENT_GENERATE,
        )
    ):
        raise PermissionError(SETTLEMENT_GENERATE_PERMISSION_MESSAGE)
    return actor


def _generate_settlement_public_id(db) -> str:
    from ..models import SupplierSettlementBatch

    for _attempt in range(SETTLEMENT_PUBLIC_ID_GENERATION_ATTEMPTS):
        random_part = "".join(
            secrets.choice(SETTLEMENT_PUBLIC_ID_ALPHABET)
            for _ in range(SETTLEMENT_PUBLIC_ID_RANDOM_LENGTH)
        )
        value = f"STL-{random_part}"
        with db.no_autoflush:
            exists = db.query(SupplierSettlementBatch.id).filter(
                SupplierSettlementBatch.settlement_public_id == value
            ).first()
        if exists is None:
            return value
    raise RuntimeError("无法生成唯一的供应商结算编号")


def _validate_source_row(*, order, item, supplier_id, period_start, period_end):
    if (
        order.status != ORDER_COMPLETED_STATUS
        or order.completed_at is None
        or order.refund_reason is not None
        or order.refunded_at is not None
    ):
        raise RuntimeError("供应商结算来源订单状态或退款证据异常")
    if not (
        _time_key(period_start)
        <= _time_key(order.completed_at)
        < _time_key(period_end)
    ):
        raise RuntimeError("供应商结算来源订单完成时间越界")
    if item.supplier_id != supplier_id:
        raise RuntimeError("供应商结算来源订单项归属异常")
    required_snapshots = (
        item.product_public_id_snapshot,
        item.product_name_snapshot,
        item.sku_code_snapshot,
        item.sku_name_snapshot,
        item.supplier_public_id_snapshot,
        item.supplier_name_snapshot,
    )
    if any(
        not isinstance(value, str) or not value.strip()
        for value in required_snapshots
    ):
        raise RuntimeError("供应商结算来源订单项快照不完整")
    unit_cost = Decimal(item.unit_cost_price)
    line_cost = Decimal(item.line_cost_amount)
    if (
        isinstance(item.quantity, bool)
        or not isinstance(item.quantity, int)
        or item.quantity <= 0
        or unit_cost < ZERO
        or line_cost != unit_cost * item.quantity
    ):
        raise RuntimeError("供应商结算来源订单项成本快照异常")


def _generation_description(batch):
    return (
        f"生成供应商结算 {batch.settlement_public_id}；"
        f"供应商 {batch.supplier_public_id_snapshot}；"
        f"订单 {batch.order_count}；明细 {batch.item_count}；"
        f"数量 {batch.total_quantity}；"
        f"成本 {Decimal(batch.total_cost_amount):.2f}"
    )


def _validate_generation_evidence(db, *, batch):
    from ..models import AdminActionLog, SupplierSettlementItem

    items = tuple(
        db.query(SupplierSettlementItem)
        .filter(SupplierSettlementItem.settlement_batch_id == batch.id)
        .order_by(SupplierSettlementItem.id.asc())
        .all()
    )
    order_ids = {item.order_id for item in items}
    total_quantity = sum(item.quantity for item in items)
    total_cost = sum(
        (Decimal(item.line_cost_amount) for item in items),
        ZERO,
    )
    if (
        batch.status
        != SupplierSettlementStatus.PENDING_CONFIRMATION.value
        or batch.confirmed_by_admin_id is not None
        or batch.confirmed_at is not None
        or len(order_ids) != batch.order_count
        or len(items) != batch.item_count
        or total_quantity != batch.total_quantity
        or total_cost != Decimal(batch.total_cost_amount)
        or any(item.supplier_id != batch.supplier_id for item in items)
    ):
        raise RuntimeError("供应商结算批次合计或状态证据不完整")

    action_log = (
        db.query(AdminActionLog)
        .filter(
            AdminActionLog.action_type
            == MallAuditActionType.SUPPLIER_SETTLEMENT_GENERATE.value,
            AdminActionLog.target_type == "supplier_settlement_batch",
            AdminActionLog.target_id == batch.id,
        )
        .one_or_none()
    )
    if (
        action_log is None
        or action_log.admin_id != batch.generated_by_admin_id
        or action_log.description != _generation_description(batch)
    ):
        raise RuntimeError("供应商结算生成审计证据不完整")
    return items, action_log


def generate_supplier_settlement(
    db,
    *,
    actor_admin_id: int,
    supplier_id: int,
    period_start,
    period_end,
    now=None,
) -> SupplierSettlementGenerationResult:
    """生成单一供应商的待确认结算批次；调用方负责提交。"""
    from ..models import (
        AdminActionLog,
        Order,
        OrderItem,
        Supplier,
        SupplierSettlementBatch,
        SupplierSettlementItem,
    )

    normalized_supplier_id = _normalize_positive_integer(
        supplier_id,
        field_name="供应商",
    )
    normalized_start = _normalize_time(
        db,
        period_start,
        field_name="结算开始时间",
    )
    normalized_end = _normalize_time(
        db,
        period_end,
        field_name="结算结束时间",
    )
    operation_time = _current_time(db, now)
    if _time_key(normalized_end) <= _time_key(normalized_start):
        raise ValueError("结算结束时间必须晚于开始时间")
    if _time_key(operation_time) < _time_key(normalized_end):
        raise ValueError("结算生成时间不能早于结算结束时间")

    actor = _require_actor(db, actor_admin_id=actor_admin_id)
    supplier = (
        db.query(Supplier)
        .filter(Supplier.id == normalized_supplier_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if supplier is None:
        raise ValueError("供应商不存在")

    already_settled = db.query(SupplierSettlementItem.id).filter(
        SupplierSettlementItem.order_item_id == OrderItem.id
    ).exists()
    source_rows = tuple(
        db.query(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            OrderItem.supplier_id == supplier.id,
            Order.status == ORDER_COMPLETED_STATUS,
            Order.completed_at.is_not(None),
            Order.completed_at >= normalized_start,
            Order.completed_at < normalized_end,
            Order.refund_reason.is_(None),
            Order.refunded_at.is_(None),
            ~already_settled,
        )
        .order_by(Order.completed_at.asc(), Order.id.asc(), OrderItem.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    if not source_rows:
        raise ValueError("结算区间内没有未结算的已完成订单项")

    order_ids = set()
    total_quantity = 0
    total_cost = ZERO
    for item, order in source_rows:
        _validate_source_row(
            order=order,
            item=item,
            supplier_id=supplier.id,
            period_start=normalized_start,
            period_end=normalized_end,
        )
        order_ids.add(order.id)
        total_quantity += item.quantity
        total_cost += Decimal(item.line_cost_amount)

    batch = SupplierSettlementBatch(
        settlement_public_id=_generate_settlement_public_id(db),
        supplier_id=supplier.id,
        supplier_public_id_snapshot=supplier.supplier_public_id,
        supplier_name_snapshot=supplier.name,
        period_start=normalized_start,
        period_end=normalized_end,
        status=SupplierSettlementStatus.PENDING_CONFIRMATION.value,
        order_count=len(order_ids),
        item_count=len(source_rows),
        total_quantity=total_quantity,
        total_cost_amount=total_cost,
        generated_by_admin_id=actor.id,
        generated_at=operation_time,
        confirmed_by_admin_id=None,
        confirmed_at=None,
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(batch)
    db.flush()

    for item, order in source_rows:
        db.add(SupplierSettlementItem(
            settlement_batch_id=batch.id,
            supplier_id=supplier.id,
            order_id=order.id,
            order_item_id=item.id,
            order_public_id_snapshot=order.order_public_id,
            order_completed_at=order.completed_at,
            product_public_id_snapshot=item.product_public_id_snapshot,
            product_name_snapshot=item.product_name_snapshot,
            sku_code_snapshot=item.sku_code_snapshot,
            sku_name_snapshot=item.sku_name_snapshot,
            supplier_public_id_snapshot=(
                item.supplier_public_id_snapshot
            ),
            supplier_name_snapshot=item.supplier_name_snapshot,
            supplier_sku_code_snapshot=item.supplier_sku_code_snapshot,
            unit_cost_price=item.unit_cost_price,
            quantity=item.quantity,
            line_cost_amount=item.line_cost_amount,
            created_at=operation_time,
        ))
    db.flush()

    action_log = AdminActionLog(
        admin_id=actor.id,
        action_type=(
            MallAuditActionType.SUPPLIER_SETTLEMENT_GENERATE.value
        ),
        target_type="supplier_settlement_batch",
        target_id=batch.id,
        description=_generation_description(batch),
        created_at=operation_time,
    )
    db.add(action_log)
    db.flush()

    verified_items, verified_log = _validate_generation_evidence(
        db,
        batch=batch,
    )
    return SupplierSettlementGenerationResult(
        batch_id=batch.id,
        settlement_public_id=batch.settlement_public_id,
        supplier_id=batch.supplier_id,
        supplier_public_id_snapshot=batch.supplier_public_id_snapshot,
        supplier_name_snapshot=batch.supplier_name_snapshot,
        status=batch.status,
        period_start=batch.period_start,
        period_end=batch.period_end,
        order_count=batch.order_count,
        item_count=batch.item_count,
        total_quantity=batch.total_quantity,
        total_cost_amount=Decimal(batch.total_cost_amount),
        generated_by_admin_id=batch.generated_by_admin_id,
        generated_at=batch.generated_at,
        settlement_item_ids=tuple(item.id for item in verified_items),
        action_log_id=verified_log.id,
    )


def execute_supplier_settlement_generation(
    engine,
    **request,
) -> SupplierSettlementGenerationResult:
    """在独立事务中生成结算批次；异常时自动整体回滚。"""
    from sqlalchemy.orm import Session

    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        try:
            with Session(
                bind=connection,
                autoflush=False,
                expire_on_commit=False,
            ) as db:
                result = generate_supplier_settlement(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
