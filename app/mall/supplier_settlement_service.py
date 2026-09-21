"""供应商结算批次生成及确认领域服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import secrets

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import SupplierSettlementStatus


ORDER_COMPLETED_STATUS = "COMPLETED"
SETTLEMENT_GENERATE_PERMISSION_MESSAGE = "当前账号无权生成供应商结算"
SETTLEMENT_CONFIRM_PERMISSION_MESSAGE = "当前账号无权确认供应商结算"
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


@dataclass(frozen=True)
class SupplierSettlementConfirmationResult:
    """供应商结算首次确认或幂等重放后的稳定结果。"""

    batch_id: int
    settlement_public_id: str
    supplier_id: int
    status: str
    order_count: int
    item_count: int
    total_quantity: int
    total_cost_amount: Decimal
    confirmed_by_admin_id: int
    confirmed_at: datetime
    action_log_id: int
    replayed: bool


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


def _current_time(db, value, *, field_name="结算生成时间"):
    current = utc8_now() if value is None else value
    return _normalize_time(db, current, field_name=field_name)


def _time_key(value):
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return value


def _require_actor(
    db,
    *,
    actor_admin_id,
    action_type=MallAuditActionType.SUPPLIER_SETTLEMENT_GENERATE,
    message=SETTLEMENT_GENERATE_PERMISSION_MESSAGE,
    lock=True,
):
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import User

    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(message)
    actor_query = db.query(User).filter(User.id == actor_admin_id)
    if lock:
        actor_query = actor_query.with_for_update()
    actor = actor_query.populate_existing().one_or_none()
    if (
        actor is None
        or actor.is_active is not True
        or not can_perform_mall_audit_action(
            actor,
            action_type,
        )
    ):
        raise PermissionError(message)
    return actor


def _normalize_required_text(value, *, field_name, maximum_length):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}不能为空")
    normalized = value.strip()
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name}不能超过 {maximum_length} 个字符")
    return normalized


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


def _confirmation_description(batch):
    return (
        f"确认供应商结算 {batch.settlement_public_id}；"
        f"供应商 {batch.supplier_public_id_snapshot}；"
        f"订单 {batch.order_count}；明细 {batch.item_count}；"
        f"数量 {batch.total_quantity}；"
        f"成本 {Decimal(batch.total_cost_amount):.2f}"
    )


def _validate_source_evidence(db, *, batch, items):
    from ..models import Order, OrderItem

    order_item_ids = tuple(item.order_item_id for item in items)
    if len(set(order_item_ids)) != len(order_item_ids):
        raise RuntimeError("供应商结算明细来源重复")
    source_rows = tuple(
        db.query(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(OrderItem.id.in_(order_item_ids))
        .order_by(OrderItem.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    if len(source_rows) != len(items):
        raise RuntimeError("供应商结算来源订单项不完整")
    source_by_item_id = {
        source_item.id: (source_item, order)
        for source_item, order in source_rows
    }
    for item in items:
        source_item, order = source_by_item_id[item.order_item_id]
        _validate_source_row(
            order=order,
            item=source_item,
            supplier_id=batch.supplier_id,
            period_start=batch.period_start,
            period_end=batch.period_end,
        )
        if (
            item.order_id != order.id
            or item.supplier_id != source_item.supplier_id
            or item.order_public_id_snapshot != order.order_public_id
            or _time_key(item.order_completed_at)
            != _time_key(order.completed_at)
            or item.product_public_id_snapshot
            != source_item.product_public_id_snapshot
            or item.product_name_snapshot
            != source_item.product_name_snapshot
            or item.sku_code_snapshot != source_item.sku_code_snapshot
            or item.sku_name_snapshot != source_item.sku_name_snapshot
            or item.supplier_public_id_snapshot
            != source_item.supplier_public_id_snapshot
            or item.supplier_name_snapshot
            != source_item.supplier_name_snapshot
            or item.supplier_sku_code_snapshot
            != source_item.supplier_sku_code_snapshot
            or Decimal(item.unit_cost_price)
            != Decimal(source_item.unit_cost_price)
            or item.quantity != source_item.quantity
            or Decimal(item.line_cost_amount)
            != Decimal(source_item.line_cost_amount)
        ):
            raise RuntimeError("供应商结算明细与来源订单项不一致")


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
        batch.status not in (
            SupplierSettlementStatus.PENDING_CONFIRMATION.value,
            SupplierSettlementStatus.CONFIRMED.value,
        )
        or len(order_ids) != batch.order_count
        or len(items) != batch.item_count
        or total_quantity != batch.total_quantity
        or total_cost != Decimal(batch.total_cost_amount)
        or any(item.supplier_id != batch.supplier_id for item in items)
    ):
        raise RuntimeError("供应商结算批次合计或状态证据不完整")

    _validate_source_evidence(db, batch=batch, items=items)

    action_logs = tuple(
        db.query(AdminActionLog)
        .filter(
            AdminActionLog.action_type
            == MallAuditActionType.SUPPLIER_SETTLEMENT_GENERATE.value,
            AdminActionLog.target_type == "supplier_settlement_batch",
            AdminActionLog.target_id == batch.id,
        )
        .order_by(AdminActionLog.id.asc())
        .all()
    )
    if (
        len(action_logs) != 1
        or action_logs[0].admin_id is None
        or action_logs[0].admin_id != batch.generated_by_admin_id
        or action_logs[0].description != _generation_description(batch)
        or _time_key(action_logs[0].created_at)
        != _time_key(batch.generated_at)
    ):
        raise RuntimeError("供应商结算生成审计证据不完整")
    return items, action_logs[0]


def _validate_confirmation_evidence(db, *, batch, expect_confirmed):
    from ..models import AdminActionLog

    logs = tuple(
        db.query(AdminActionLog)
        .filter(
            AdminActionLog.action_type
            == MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM.value,
            AdminActionLog.target_type == "supplier_settlement_batch",
            AdminActionLog.target_id == batch.id,
        )
        .order_by(AdminActionLog.id.asc())
        .all()
    )
    evidence_values = (batch.confirmed_by_admin_id, batch.confirmed_at)
    if not expect_confirmed:
        if (
            batch.status
            != SupplierSettlementStatus.PENDING_CONFIRMATION.value
            or logs
            or any(value is not None for value in evidence_values)
        ):
            raise RuntimeError("供应商结算状态与确认事实不一致")
        return None
    if (
        batch.status != SupplierSettlementStatus.CONFIRMED.value
        or any(value is None for value in evidence_values)
        or len(logs) != 1
        or logs[0].admin_id != batch.confirmed_by_admin_id
        or logs[0].description != _confirmation_description(batch)
        or _time_key(logs[0].created_at) != _time_key(batch.confirmed_at)
        or _time_key(batch.confirmed_at) < _time_key(batch.generated_at)
    ):
        raise RuntimeError("供应商结算确认审计证据不完整")
    return logs[0]


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
    _validate_confirmation_evidence(
        db,
        batch=batch,
        expect_confirmed=False,
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


def confirm_supplier_settlement(
    db,
    *,
    actor_admin_id: int,
    settlement_public_id,
    now=None,
) -> SupplierSettlementConfirmationResult:
    """把待确认结算推进到已确认；调用方负责提交。"""
    from ..models import AdminActionLog, SupplierSettlementBatch

    normalized_public_id = _normalize_required_text(
        settlement_public_id,
        field_name="供应商结算编号",
        maximum_length=32,
    )
    operation_time = _current_time(
        db,
        now,
        field_name="结算确认时间",
    )
    batch = (
        db.query(SupplierSettlementBatch)
        .filter(
            SupplierSettlementBatch.settlement_public_id
            == normalized_public_id
        )
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if batch is None:
        raise ValueError("供应商结算批次不存在")
    if batch.status not in (
        SupplierSettlementStatus.PENDING_CONFIRMATION.value,
        SupplierSettlementStatus.CONFIRMED.value,
    ):
        raise ValueError("当前供应商结算状态不允许确认")
    _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM,
        message=SETTLEMENT_CONFIRM_PERMISSION_MESSAGE,
        lock=False,
    )
    _validate_generation_evidence(db, batch=batch)
    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM,
        message=SETTLEMENT_CONFIRM_PERMISSION_MESSAGE,
    )

    replayed = batch.status == SupplierSettlementStatus.CONFIRMED.value
    confirmation_log = _validate_confirmation_evidence(
        db,
        batch=batch,
        expect_confirmed=replayed,
    )
    if replayed:
        return SupplierSettlementConfirmationResult(
            batch_id=batch.id,
            settlement_public_id=batch.settlement_public_id,
            supplier_id=batch.supplier_id,
            status=batch.status,
            order_count=batch.order_count,
            item_count=batch.item_count,
            total_quantity=batch.total_quantity,
            total_cost_amount=Decimal(batch.total_cost_amount),
            confirmed_by_admin_id=batch.confirmed_by_admin_id,
            confirmed_at=batch.confirmed_at,
            action_log_id=confirmation_log.id,
            replayed=True,
        )

    if _time_key(operation_time) < _time_key(batch.generated_at):
        raise ValueError("结算确认时间不能早于生成时间")
    batch.status = SupplierSettlementStatus.CONFIRMED.value
    batch.confirmed_by_admin_id = actor.id
    batch.confirmed_at = operation_time
    batch.updated_at = operation_time
    db.flush()
    action_log = AdminActionLog(
        admin_id=actor.id,
        action_type=MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM.value,
        target_type="supplier_settlement_batch",
        target_id=batch.id,
        description=_confirmation_description(batch),
        created_at=operation_time,
    )
    db.add(action_log)
    db.flush()
    _validate_generation_evidence(db, batch=batch)
    verified_log = _validate_confirmation_evidence(
        db,
        batch=batch,
        expect_confirmed=True,
    )
    if verified_log.id != action_log.id:
        raise RuntimeError("供应商结算确认审计证据不完整")
    return SupplierSettlementConfirmationResult(
        batch_id=batch.id,
        settlement_public_id=batch.settlement_public_id,
        supplier_id=batch.supplier_id,
        status=batch.status,
        order_count=batch.order_count,
        item_count=batch.item_count,
        total_quantity=batch.total_quantity,
        total_cost_amount=Decimal(batch.total_cost_amount),
        confirmed_by_admin_id=batch.confirmed_by_admin_id,
        confirmed_at=batch.confirmed_at,
        action_log_id=verified_log.id,
        replayed=False,
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


def execute_supplier_settlement_confirmation(
    engine,
    **request,
) -> SupplierSettlementConfirmationResult:
    """在独立事务中确认结算批次；异常时自动整体回滚。"""
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
                result = confirm_supplier_settlement(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
