"""已完成纯积分订单退款及积分、库存恢复领域服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import PointsGrantStatus, PointsLedgerEntryType
from .inventory_service import return_outbound_inventory_for_order
from .order_fulfillment_service import (
    ORDER_REFERENCE_TYPE,
    POINTS_CONSUME_REASON,
    _load_locked_resources,
    _validate_fulfillment_audit,
    _validate_inventory_evidence,
    _validate_points_evidence,
)
from .order_lifecycle_service import (
    _validate_completion_evidence,
    _validate_shipping_evidence,
)
from .points_ledger_service import assert_points_account_balance_consistent


ORDER_COMPLETED_STATUS = "COMPLETED"
ORDER_REFUNDED_STATUS = "REFUNDED"
ORDER_REFUND_PERMISSION_MESSAGE = "当前账号无权执行商城订单退款"
POINTS_REFUND_REASON = "订单退款退回积分"
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class OrderRefundResult:
    """订单首次退款或幂等重放后的稳定结果。"""

    order_id: int
    order_public_id: str
    status: str
    refunded_points: Decimal
    refunded_at: datetime
    refund_reason: str
    points_refund_entry_ids: tuple[int, ...]
    inventory_return_movement_ids: tuple[int, ...]
    action_log_id: int
    replayed: bool


def _normalize_required_text(value, *, field_name, maximum_length):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}不能为空")
    normalized = value.strip()
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name}不能超过 {maximum_length} 个字符")
    return normalized


def _database_time(db, value):
    if db.get_bind().dialect.name == "postgresql":
        return value.replace(tzinfo=UTC8_TIMEZONE)
    return value


def _current_time(db, value):
    current = utc8_now() if value is None else value
    if not isinstance(current, datetime):
        raise ValueError("退款时间无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


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
        raise PermissionError(ORDER_REFUND_PERMISSION_MESSAGE)
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
            MallAuditActionType.ORDER_REFUND,
        )
    ):
        raise PermissionError(ORDER_REFUND_PERMISSION_MESSAGE)
    return actor


def _refund_description(order):
    return (
        f"订单 {order.order_public_id} 整单退款；"
        f"积分 {Decimal(order.total_points):.2f}；"
        f"原因 {order.refund_reason}"
    )


def _validate_refund_evidence(
    db,
    *,
    order,
    expect_refunded,
    expected_reason=None,
):
    from ..models import AdminActionLog

    logs = tuple(
        db.query(AdminActionLog)
        .filter(
            AdminActionLog.action_type
            == MallAuditActionType.ORDER_REFUND.value,
            AdminActionLog.target_type == "mall_order",
            AdminActionLog.target_id == order.id,
        )
        .order_by(AdminActionLog.id.asc())
        .all()
    )
    evidence_values = (order.refund_reason, order.refunded_at)
    if not expect_refunded:
        if logs or any(value is not None for value in evidence_values):
            raise RuntimeError("订单状态与退款证据不一致")
        return None
    if any(value is None for value in evidence_values):
        raise RuntimeError("订单退款证据不完整")
    if expected_reason is not None and order.refund_reason != expected_reason:
        raise ValueError("订单已使用其他退款原因完成退款")
    if (
        len(logs) != 1
        or logs[0].admin_id is None
        or logs[0].description != _refund_description(order)
        or _time_key(logs[0].created_at) != _time_key(order.refunded_at)
    ):
        raise RuntimeError("订单退款审计证据不完整")
    if _time_key(order.refunded_at) < _time_key(order.completed_at):
        raise RuntimeError("订单退款时间早于完成时间")
    return logs[0]


def _validate_refunded_inventory_evidence(
    db,
    *,
    order,
    items,
    fulfillment_actor_admin_id,
    refund_actor_admin_id,
):
    from ..models import InventoryMovement

    movements = tuple(
        db.query(InventoryMovement)
        .filter(
            InventoryMovement.reference_type == ORDER_REFERENCE_TYPE,
            InventoryMovement.reference_id == order.order_public_id,
        )
        .order_by(InventoryMovement.id.asc())
        .all()
    )
    if len(movements) != len(items) * 3:
        raise RuntimeError("订单库存预占、出库或退回证据不完整")
    by_key = {movement.idempotency_key: movement for movement in movements}
    if len(by_key) != len(movements):
        raise RuntimeError("订单库存退款证据冲突")

    return_ids = []
    for item in items:
        prefix = f"order:{order.order_public_id}:sku:{item.sku_id}"
        reserve = by_key.get(f"{prefix}:reserve")
        outbound = by_key.get(f"{prefix}:outbound")
        returned = by_key.get(f"{prefix}:return")
        if not (
            reserve is not None
            and reserve.sku_id == item.sku_id
            and reserve.movement_type == "RESERVE"
            and reserve.quantity_delta == 0
            and reserve.reserved_quantity_delta == item.quantity
            and reserve.actor_admin_id is None
            and reserve.actor_member_id == order.member_id
            and reserve.reason
            == f"订单 {order.order_public_id} 预占库存"
        ):
            raise RuntimeError("订单库存预占证据不完整")
        if not (
            outbound is not None
            and outbound.sku_id == item.sku_id
            and outbound.movement_type == "OUTBOUND"
            and outbound.quantity_delta == -item.quantity
            and outbound.reserved_quantity_delta == -item.quantity
            and outbound.actor_admin_id == fulfillment_actor_admin_id
            and outbound.actor_member_id is None
            and outbound.reason
            == f"订单 {order.order_public_id} 确认履约出库"
        ):
            raise RuntimeError("订单库存出库证据不完整")
        if not (
            returned is not None
            and returned.sku_id == item.sku_id
            and returned.movement_type == "RETURN"
            and returned.quantity_delta == item.quantity
            and returned.reserved_quantity_delta == 0
            and returned.actor_admin_id == refund_actor_admin_id
            and returned.actor_member_id is None
            and returned.reason
            == f"订单 {order.order_public_id} 退款退回库存"
        ):
            raise RuntimeError("订单库存退回证据不完整")
        return_ids.append(returned.id)
    return tuple(return_ids)


def _validate_refunded_points_evidence(
    db,
    *,
    order,
    allocations,
    fulfillment_actor_admin_id,
    refund_actor_admin_id,
):
    from ..models import PointsLedgerEntry

    entries = tuple(
        db.query(PointsLedgerEntry)
        .filter(
            PointsLedgerEntry.reference_type == ORDER_REFERENCE_TYPE,
            PointsLedgerEntry.reference_id == order.order_public_id,
        )
        .order_by(PointsLedgerEntry.id.asc())
        .all()
    )
    if len(entries) != len(allocations) * 3:
        raise RuntimeError("订单积分预占、消费或退款证据不完整")
    by_key = {entry.idempotency_key: entry for entry in entries}
    if len(by_key) != len(entries):
        raise RuntimeError("订单积分退款证据冲突")

    refund_ids = []
    for allocation in allocations:
        points = Decimal(allocation.allocated_points)
        prefix = (
            f"order:{order.order_public_id}:grant:"
            f"{allocation.points_grant_id}"
        )
        reserve = by_key.get(f"{prefix}:reserve")
        consume = by_key.get(f"{prefix}:consume")
        refund = by_key.get(f"{prefix}:refund")
        if not (
            reserve is not None
            and reserve.grant_id == allocation.points_grant_id
            and reserve.entry_type == PointsLedgerEntryType.RESERVE.value
            and Decimal(reserve.available_points_delta) == -points
            and Decimal(reserve.reserved_points_delta) == points
            and reserve.actor_admin_id is None
            and reserve.reason is None
        ):
            raise RuntimeError("订单积分预占证据不完整")
        if not (
            consume is not None
            and consume.grant_id == allocation.points_grant_id
            and consume.entry_type == PointsLedgerEntryType.CONSUME.value
            and Decimal(consume.available_points_delta) == ZERO
            and Decimal(consume.reserved_points_delta) == -points
            and consume.actor_admin_id == fulfillment_actor_admin_id
            and consume.reason == POINTS_CONSUME_REASON
        ):
            raise RuntimeError("订单积分消费证据不完整")
        if not (
            refund is not None
            and refund.grant_id == allocation.points_grant_id
            and refund.entry_type == PointsLedgerEntryType.REFUND.value
            and Decimal(refund.available_points_delta) == points
            and Decimal(refund.reserved_points_delta) == ZERO
            and refund.actor_admin_id == refund_actor_admin_id
            and refund.reason == POINTS_REFUND_REASON
        ):
            raise RuntimeError("订单积分退款证据不完整")
        refund_ids.append(refund.id)
    return tuple(refund_ids)


def refund_completed_order(
    db,
    *,
    actor_admin_id: int,
    order_public_id,
    reason,
    now=None,
) -> OrderRefundResult:
    """把 COMPLETED 订单整单退款并原子恢复积分和库存。"""
    from ..models import AdminActionLog, Order, PointsLedgerEntry

    normalized_order_public_id = _normalize_required_text(
        order_public_id,
        field_name="订单编号",
        maximum_length=32,
    )
    normalized_reason = _normalize_required_text(
        reason,
        field_name="退款原因",
        maximum_length=500,
    )
    operation_time = _current_time(db, now)
    order = (
        db.query(Order)
        .filter(Order.order_public_id == normalized_order_public_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if order is None:
        raise ValueError("订单不存在")
    if order.status not in (ORDER_COMPLETED_STATUS, ORDER_REFUNDED_STATUS):
        raise ValueError("当前订单状态不允许退款")

    actor = _require_actor(db, actor_admin_id=actor_admin_id)
    items, allocations, account, grants = _load_locked_resources(
        db,
        order=order,
    )
    fulfillment_log = _validate_fulfillment_audit(
        db,
        order=order,
        expect_fulfilled=True,
    )
    _validate_shipping_evidence(db, order=order, expect_shipped=True)
    _validate_completion_evidence(db, order=order, expect_completed=True)

    if order.status == ORDER_REFUNDED_STATUS:
        refund_log = _validate_refund_evidence(
            db,
            order=order,
            expect_refunded=True,
            expected_reason=normalized_reason,
        )
        inventory_return_ids = _validate_refunded_inventory_evidence(
            db,
            order=order,
            items=items,
            fulfillment_actor_admin_id=fulfillment_log.admin_id,
            refund_actor_admin_id=refund_log.admin_id,
        )
        points_refund_ids = _validate_refunded_points_evidence(
            db,
            order=order,
            allocations=allocations,
            fulfillment_actor_admin_id=fulfillment_log.admin_id,
            refund_actor_admin_id=refund_log.admin_id,
        )
        return OrderRefundResult(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            refunded_points=Decimal(order.total_points),
            refunded_at=order.refunded_at,
            refund_reason=order.refund_reason,
            points_refund_entry_ids=points_refund_ids,
            inventory_return_movement_ids=inventory_return_ids,
            action_log_id=refund_log.id,
            replayed=True,
        )

    _validate_refund_evidence(
        db,
        order=order,
        expect_refunded=False,
    )
    _validate_inventory_evidence(
        db,
        order=order,
        items=items,
        expect_fulfilled=True,
        fulfillment_actor_admin_id=fulfillment_log.admin_id,
    )
    _validate_points_evidence(
        db,
        order=order,
        allocations=allocations,
        expect_fulfilled=True,
        fulfillment_actor_admin_id=fulfillment_log.admin_id,
    )
    if _time_key(operation_time) < _time_key(order.completed_at):
        raise ValueError("退款时间不能早于订单完成时间")

    grants_by_id = {grant.id: grant for grant in grants}
    for allocation in allocations:
        grant = grants_by_id[allocation.points_grant_id]
        if (
            grant.status == PointsGrantStatus.EXPIRED.value
            or _time_key(operation_time) >= _time_key(grant.expires_at)
        ):
            raise ValueError("原积分批次已到期，不能自动退款")
        if grant.status not in (
            PointsGrantStatus.ACTIVE.value,
            PointsGrantStatus.EXHAUSTED.value,
            PointsGrantStatus.FROZEN.value,
        ):
            raise ValueError("原积分批次状态不允许自动退款")
        points = Decimal(allocation.allocated_points)
        if (
            Decimal(grant.available_points)
            + Decimal(grant.reserved_points)
            + points
            > Decimal(grant.granted_points)
        ):
            raise RuntimeError("积分退款将超过原批次授予额度")

    inventory_return_ids = []
    for item in items:
        result = return_outbound_inventory_for_order(
            db,
            actor_admin_id=actor.id,
            member_id=order.member_id,
            sku_id=item.sku_id,
            quantity=item.quantity,
            order_public_id=order.order_public_id,
            idempotency_key=(
                f"order:{order.order_public_id}:sku:{item.sku_id}:return"
            ),
            now=operation_time,
        )
        if result.replayed:
            raise RuntimeError("订单状态与库存退回证据不一致")
        inventory_return_ids.append(result.movement.id)

    points_refund_ids = []
    for allocation in allocations:
        grant = grants_by_id[allocation.points_grant_id]
        points = Decimal(allocation.allocated_points)
        grant.available_points = Decimal(grant.available_points) + points
        if grant.status != PointsGrantStatus.FROZEN.value:
            grant.status = PointsGrantStatus.ACTIVE.value
        grant.updated_at = operation_time
        entry = PointsLedgerEntry(
            grant_id=grant.id,
            entry_type=PointsLedgerEntryType.REFUND.value,
            available_points_delta=points,
            reserved_points_delta=ZERO,
            idempotency_key=(
                f"order:{order.order_public_id}:grant:{grant.id}:refund"
            ),
            reference_type=ORDER_REFERENCE_TYPE,
            reference_id=order.order_public_id,
            actor_admin_id=actor.id,
            reason=POINTS_REFUND_REASON,
            created_at=operation_time,
        )
        db.add(entry)
        db.flush()
        points_refund_ids.append(entry.id)

    account.available_points = (
        Decimal(account.available_points) + Decimal(order.total_points)
    )
    account.version = (account.version or 0) + 1
    account.updated_at = operation_time
    order.status = ORDER_REFUNDED_STATUS
    order.refund_reason = normalized_reason
    order.refunded_at = operation_time
    order.updated_at = operation_time
    db.flush()
    refund_log = AdminActionLog(
        admin_id=actor.id,
        action_type=MallAuditActionType.ORDER_REFUND.value,
        target_type="mall_order",
        target_id=order.id,
        description=_refund_description(order),
        created_at=operation_time,
    )
    db.add(refund_log)
    db.flush()
    assert_points_account_balance_consistent(db, account_id=account.id)

    verified_inventory_ids = _validate_refunded_inventory_evidence(
        db,
        order=order,
        items=items,
        fulfillment_actor_admin_id=fulfillment_log.admin_id,
        refund_actor_admin_id=actor.id,
    )
    verified_points_ids = _validate_refunded_points_evidence(
        db,
        order=order,
        allocations=allocations,
        fulfillment_actor_admin_id=fulfillment_log.admin_id,
        refund_actor_admin_id=actor.id,
    )
    verified_log = _validate_refund_evidence(
        db,
        order=order,
        expect_refunded=True,
        expected_reason=normalized_reason,
    )
    if (
        verified_inventory_ids != tuple(inventory_return_ids)
        or verified_points_ids != tuple(points_refund_ids)
        or verified_log.id != refund_log.id
    ):
        raise RuntimeError("订单退款证据不完整")
    return OrderRefundResult(
        order_id=order.id,
        order_public_id=order.order_public_id,
        status=order.status,
        refunded_points=Decimal(order.total_points),
        refunded_at=order.refunded_at,
        refund_reason=order.refund_reason,
        points_refund_entry_ids=verified_points_ids,
        inventory_return_movement_ids=verified_inventory_ids,
        action_log_id=verified_log.id,
        replayed=False,
    )


def execute_order_refund(engine, **request) -> OrderRefundResult:
    """在独立事务中整单退款；异常时自动整体回滚。"""
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
                result = refund_completed_order(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
