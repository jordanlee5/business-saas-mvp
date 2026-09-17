"""纯积分订单发货及完成的原子生命周期领域服务。"""

from dataclasses import dataclass
from datetime import datetime

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .order_fulfillment_service import (
    _validate_fulfillment_audit,
    _validate_inventory_evidence,
    _validate_order_totals,
    _validate_points_evidence,
)


ORDER_FULFILLING_STATUS = "FULFILLING"
ORDER_SHIPPED_STATUS = "SHIPPED"
ORDER_COMPLETED_STATUS = "COMPLETED"
ORDER_SHIP_PERMISSION_MESSAGE = "当前账号无权执行商城订单发货"
ORDER_COMPLETE_PERMISSION_MESSAGE = "当前账号无权确认商城订单完成"


@dataclass(frozen=True)
class OrderShippingResult:
    """订单首次发货或幂等重放后的稳定结果。"""

    order_id: int
    order_public_id: str
    status: str
    shipping_carrier: str
    tracking_number: str
    shipped_at: datetime
    action_log_id: int
    replayed: bool


@dataclass(frozen=True)
class OrderCompletionResult:
    """订单首次完成或幂等重放后的稳定结果。"""

    order_id: int
    order_public_id: str
    status: str
    shipping_carrier: str
    tracking_number: str
    shipped_at: datetime
    completed_at: datetime
    shipping_action_log_id: int
    completion_action_log_id: int
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


def _current_time(db, value, *, field_name):
    current = utc8_now() if value is None else value
    if not isinstance(current, datetime):
        raise ValueError(f"{field_name}无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


def _time_key(value):
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return value


def _require_actor(db, *, actor_admin_id, action_type, message):
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import User

    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(message)
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
        or not can_perform_mall_audit_action(actor, action_type)
    ):
        raise PermissionError(message)
    return actor


def _load_and_validate_fulfillment_evidence(db, *, order):
    from ..models import OrderItem, OrderPointsGrantAllocation

    items = tuple(
        db.query(OrderItem)
        .filter(OrderItem.order_id == order.id)
        .order_by(OrderItem.sku_id.asc())
        .all()
    )
    allocations = tuple(
        db.query(OrderPointsGrantAllocation)
        .filter(OrderPointsGrantAllocation.order_id == order.id)
        .order_by(OrderPointsGrantAllocation.points_grant_id.asc())
        .all()
    )
    _validate_order_totals(order, items, allocations)
    fulfillment_log = _validate_fulfillment_audit(
        db,
        order=order,
        expect_fulfilled=True,
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
    return fulfillment_log


def _shipping_description(order):
    return (
        f"订单 {order.order_public_id} 发货；"
        f"物流公司 {order.shipping_carrier}；"
        f"运单号 {order.tracking_number}"
    )


def _completion_description(order):
    return f"订单 {order.order_public_id} 确认完成"


def _action_logs(db, *, order, action_type):
    from ..models import AdminActionLog

    return tuple(
        db.query(AdminActionLog)
        .filter(
            AdminActionLog.action_type == action_type.value,
            AdminActionLog.target_type == "mall_order",
            AdminActionLog.target_id == order.id,
        )
        .order_by(AdminActionLog.id.asc())
        .all()
    )


def _validate_shipping_evidence(
    db,
    *,
    order,
    expect_shipped,
    expected_carrier=None,
    expected_tracking_number=None,
):
    logs = _action_logs(
        db,
        order=order,
        action_type=MallAuditActionType.ORDER_SHIP,
    )
    evidence_values = (
        order.shipping_carrier,
        order.tracking_number,
        order.shipped_at,
    )
    if not expect_shipped:
        if logs or any(value is not None for value in evidence_values):
            raise RuntimeError("订单状态与发货证据不一致")
        return None
    if any(value is None for value in evidence_values):
        raise RuntimeError("订单发货证据不完整")
    if (
        expected_carrier is not None
        and order.shipping_carrier != expected_carrier
    ) or (
        expected_tracking_number is not None
        and order.tracking_number != expected_tracking_number
    ):
        raise ValueError("订单已使用其他物流信息发货")
    if (
        len(logs) != 1
        or logs[0].admin_id is None
        or logs[0].description != _shipping_description(order)
        or _time_key(logs[0].created_at) != _time_key(order.shipped_at)
    ):
        raise RuntimeError("订单发货审计证据不完整")
    return logs[0]


def _validate_completion_evidence(db, *, order, expect_completed):
    logs = _action_logs(
        db,
        order=order,
        action_type=MallAuditActionType.ORDER_COMPLETE,
    )
    if not expect_completed:
        if logs or order.completed_at is not None:
            raise RuntimeError("订单状态与完成证据不一致")
        return None
    if (
        order.completed_at is None
        or len(logs) != 1
        or logs[0].admin_id is None
        or logs[0].description != _completion_description(order)
        or _time_key(logs[0].created_at) != _time_key(order.completed_at)
    ):
        raise RuntimeError("订单完成审计证据不完整")
    if _time_key(order.completed_at) < _time_key(order.shipped_at):
        raise RuntimeError("订单完成时间早于发货时间")
    return logs[0]


def ship_fulfilling_order(
    db,
    *,
    actor_admin_id: int,
    order_public_id,
    shipping_carrier,
    tracking_number,
    now=None,
) -> OrderShippingResult:
    """把 FULFILLING 订单原子推进到 SHIPPED；调用方负责提交。"""
    from ..models import AdminActionLog, Order

    normalized_order_public_id = _normalize_required_text(
        order_public_id,
        field_name="订单编号",
        maximum_length=32,
    )
    normalized_carrier = _normalize_required_text(
        shipping_carrier,
        field_name="物流公司",
        maximum_length=100,
    )
    normalized_tracking_number = _normalize_required_text(
        tracking_number,
        field_name="运单号",
        maximum_length=100,
    )
    operation_time = _current_time(db, now, field_name="发货时间")
    order = (
        db.query(Order)
        .filter(Order.order_public_id == normalized_order_public_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if order is None:
        raise ValueError("订单不存在")
    if order.status not in (ORDER_FULFILLING_STATUS, ORDER_SHIPPED_STATUS):
        raise ValueError("当前订单状态不允许发货")
    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.ORDER_SHIP,
        message=ORDER_SHIP_PERMISSION_MESSAGE,
    )
    fulfillment_log = _load_and_validate_fulfillment_evidence(
        db,
        order=order,
    )
    _validate_completion_evidence(
        db,
        order=order,
        expect_completed=False,
    )
    if order.status == ORDER_SHIPPED_STATUS:
        shipping_log = _validate_shipping_evidence(
            db,
            order=order,
            expect_shipped=True,
            expected_carrier=normalized_carrier,
            expected_tracking_number=normalized_tracking_number,
        )
        return OrderShippingResult(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            shipping_carrier=order.shipping_carrier,
            tracking_number=order.tracking_number,
            shipped_at=order.shipped_at,
            action_log_id=shipping_log.id,
            replayed=True,
        )

    _validate_shipping_evidence(
        db,
        order=order,
        expect_shipped=False,
    )
    if _time_key(operation_time) < _time_key(fulfillment_log.created_at):
        raise ValueError("发货时间不能早于确认履约时间")
    conflicting_order = (
        db.query(Order.id)
        .filter(
            Order.id != order.id,
            Order.shipping_carrier == normalized_carrier,
            Order.tracking_number == normalized_tracking_number,
        )
        .one_or_none()
    )
    if conflicting_order is not None:
        raise ValueError("物流公司与运单号已用于其他订单")

    order.shipping_carrier = normalized_carrier
    order.tracking_number = normalized_tracking_number
    order.shipped_at = operation_time
    order.status = ORDER_SHIPPED_STATUS
    order.updated_at = operation_time
    db.flush()
    shipping_log = AdminActionLog(
        admin_id=actor.id,
        action_type=MallAuditActionType.ORDER_SHIP.value,
        target_type="mall_order",
        target_id=order.id,
        description=_shipping_description(order),
        created_at=operation_time,
    )
    db.add(shipping_log)
    db.flush()
    verified_log = _validate_shipping_evidence(
        db,
        order=order,
        expect_shipped=True,
        expected_carrier=normalized_carrier,
        expected_tracking_number=normalized_tracking_number,
    )
    if verified_log.id != shipping_log.id:
        raise RuntimeError("订单发货证据不完整")
    return OrderShippingResult(
        order_id=order.id,
        order_public_id=order.order_public_id,
        status=order.status,
        shipping_carrier=order.shipping_carrier,
        tracking_number=order.tracking_number,
        shipped_at=order.shipped_at,
        action_log_id=verified_log.id,
        replayed=False,
    )


def complete_shipped_order(
    db,
    *,
    actor_admin_id: int,
    order_public_id,
    now=None,
) -> OrderCompletionResult:
    """把 SHIPPED 订单原子推进到 COMPLETED；调用方负责提交。"""
    from ..models import AdminActionLog, Order

    normalized_order_public_id = _normalize_required_text(
        order_public_id,
        field_name="订单编号",
        maximum_length=32,
    )
    operation_time = _current_time(db, now, field_name="完成时间")
    order = (
        db.query(Order)
        .filter(Order.order_public_id == normalized_order_public_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if order is None:
        raise ValueError("订单不存在")
    if order.status not in (ORDER_SHIPPED_STATUS, ORDER_COMPLETED_STATUS):
        raise ValueError("当前订单状态不允许确认完成")
    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.ORDER_COMPLETE,
        message=ORDER_COMPLETE_PERMISSION_MESSAGE,
    )
    _load_and_validate_fulfillment_evidence(db, order=order)
    shipping_log = _validate_shipping_evidence(
        db,
        order=order,
        expect_shipped=True,
    )
    if order.status == ORDER_COMPLETED_STATUS:
        completion_log = _validate_completion_evidence(
            db,
            order=order,
            expect_completed=True,
        )
        return OrderCompletionResult(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            shipping_carrier=order.shipping_carrier,
            tracking_number=order.tracking_number,
            shipped_at=order.shipped_at,
            completed_at=order.completed_at,
            shipping_action_log_id=shipping_log.id,
            completion_action_log_id=completion_log.id,
            replayed=True,
        )

    _validate_completion_evidence(
        db,
        order=order,
        expect_completed=False,
    )
    if _time_key(operation_time) < _time_key(order.shipped_at):
        raise ValueError("完成时间不能早于发货时间")
    order.completed_at = operation_time
    order.status = ORDER_COMPLETED_STATUS
    order.updated_at = operation_time
    db.flush()
    completion_log = AdminActionLog(
        admin_id=actor.id,
        action_type=MallAuditActionType.ORDER_COMPLETE.value,
        target_type="mall_order",
        target_id=order.id,
        description=_completion_description(order),
        created_at=operation_time,
    )
    db.add(completion_log)
    db.flush()
    verified_log = _validate_completion_evidence(
        db,
        order=order,
        expect_completed=True,
    )
    if verified_log.id != completion_log.id:
        raise RuntimeError("订单完成证据不完整")
    return OrderCompletionResult(
        order_id=order.id,
        order_public_id=order.order_public_id,
        status=order.status,
        shipping_carrier=order.shipping_carrier,
        tracking_number=order.tracking_number,
        shipped_at=order.shipped_at,
        completed_at=order.completed_at,
        shipping_action_log_id=shipping_log.id,
        completion_action_log_id=verified_log.id,
        replayed=False,
    )


def _execute_lifecycle_operation(engine, operation, request):
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
                result = operation(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise


def execute_order_shipping(engine, **request) -> OrderShippingResult:
    """在独立事务中发货；异常时自动整体回滚。"""
    return _execute_lifecycle_operation(engine, ship_fulfilling_order, request)


def execute_order_completion(engine, **request) -> OrderCompletionResult:
    """在独立事务中确认完成；异常时自动整体回滚。"""
    return _execute_lifecycle_operation(engine, complete_shipped_order, request)
