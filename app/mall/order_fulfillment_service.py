"""纯积分订单确认履约及积分消费、库存出库的原子领域服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import PointsGrantStatus, PointsLedgerEntryType
from .inventory_service import (
    assert_inventory_balance_consistent,
    outbound_reserved_inventory_for_order,
)
from .points_ledger_service import assert_points_account_balance_consistent


ORDER_REFERENCE_TYPE = "ORDER"
ORDER_CREATED_STATUS = "CREATED"
ORDER_FULFILLING_STATUS = "FULFILLING"
ORDER_FULFILL_PERMISSION_MESSAGE = "当前账号无权确认商城订单履约"
POINTS_CONSUME_REASON = "订单确认履约消费积分"
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class OrderFulfillmentResult:
    """订单首次确认履约或幂等重放后的稳定结果。"""

    order_id: int
    order_public_id: str
    status: str
    consumed_points: Decimal
    points_consume_entry_ids: tuple[int, ...]
    inventory_outbound_movement_ids: tuple[int, ...]
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
        raise ValueError("履约时间无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


def _require_actor(db, *, actor_admin_id):
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import User

    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(ORDER_FULFILL_PERMISSION_MESSAGE)
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
            MallAuditActionType.ORDER_FULFILL,
        )
    ):
        raise PermissionError(ORDER_FULFILL_PERMISSION_MESSAGE)
    return actor


def _validate_order_totals(order, items, allocations):
    item_points = sum((Decimal(item.line_points) for item in items), ZERO)
    item_cost = sum((Decimal(item.line_cost_amount) for item in items), ZERO)
    item_quantity = sum(item.quantity for item in items)
    allocated_points = sum(
        (Decimal(allocation.allocated_points) for allocation in allocations),
        ZERO,
    )
    if (
        not items
        or not allocations
        or item_points != Decimal(order.total_points)
        or item_cost != Decimal(order.total_cost_amount)
        or allocated_points != Decimal(order.total_points)
        or item_quantity != order.total_quantity
    ):
        raise RuntimeError("订单资源预占证据不完整")


def _validate_inventory_evidence(
    db,
    *,
    order,
    items,
    expect_fulfilled,
    fulfillment_actor_admin_id=None,
):
    from ..models import InventoryMovement

    movements = (
        db.query(InventoryMovement)
        .filter(
            InventoryMovement.reference_type == ORDER_REFERENCE_TYPE,
            InventoryMovement.reference_id == order.order_public_id,
        )
        .order_by(InventoryMovement.id.asc())
        .all()
    )
    expected_count = len(items) * (2 if expect_fulfilled else 1)
    if len(movements) != expected_count:
        raise RuntimeError("订单库存预占或出库证据不完整")
    by_key = {movement.idempotency_key: movement for movement in movements}
    if len(by_key) != len(movements):
        raise RuntimeError("订单库存预占或出库证据冲突")

    outbound_ids = []
    for item in items:
        reserve_key = (
            f"order:{order.order_public_id}:sku:{item.sku_id}:reserve"
        )
        reserve = by_key.get(reserve_key)
        valid_reserve = reserve is not None and (
            reserve.sku_id == item.sku_id
            and reserve.movement_type == "RESERVE"
            and reserve.quantity_delta == 0
            and reserve.reserved_quantity_delta == item.quantity
            and reserve.actor_admin_id is None
            and reserve.actor_member_id == order.member_id
            and reserve.reason
            == f"订单 {order.order_public_id} 预占库存"
        )
        if not valid_reserve:
            raise RuntimeError("订单库存预占证据不完整")

        outbound_key = (
            f"order:{order.order_public_id}:sku:{item.sku_id}:outbound"
        )
        outbound = by_key.get(outbound_key)
        if not expect_fulfilled:
            if outbound is not None:
                raise RuntimeError("订单状态与库存出库证据不一致")
            continue
        valid_outbound = outbound is not None and (
            outbound.sku_id == item.sku_id
            and outbound.movement_type == "OUTBOUND"
            and outbound.quantity_delta == -item.quantity
            and outbound.reserved_quantity_delta == -item.quantity
            and outbound.actor_admin_id == fulfillment_actor_admin_id
            and outbound.actor_member_id is None
            and outbound.reason
            == f"订单 {order.order_public_id} 确认履约出库"
        )
        if not valid_outbound:
            raise RuntimeError("订单库存出库证据不完整")
        outbound_ids.append(outbound.id)
    return tuple(outbound_ids)


def _validate_points_evidence(
    db,
    *,
    order,
    allocations,
    expect_fulfilled,
    fulfillment_actor_admin_id=None,
):
    from ..models import PointsLedgerEntry

    entries = (
        db.query(PointsLedgerEntry)
        .filter(
            PointsLedgerEntry.reference_type == ORDER_REFERENCE_TYPE,
            PointsLedgerEntry.reference_id == order.order_public_id,
        )
        .order_by(PointsLedgerEntry.id.asc())
        .all()
    )
    expected_count = len(allocations) * (2 if expect_fulfilled else 1)
    if len(entries) != expected_count:
        raise RuntimeError("订单积分预占或消费证据不完整")
    by_key = {entry.idempotency_key: entry for entry in entries}
    if len(by_key) != len(entries):
        raise RuntimeError("订单积分预占或消费证据冲突")

    consume_ids = []
    for allocation in allocations:
        points = Decimal(allocation.allocated_points)
        reserve_key = (
            f"order:{order.order_public_id}:grant:"
            f"{allocation.points_grant_id}:reserve"
        )
        reserve = by_key.get(reserve_key)
        valid_reserve = reserve is not None and (
            reserve.grant_id == allocation.points_grant_id
            and reserve.entry_type == PointsLedgerEntryType.RESERVE.value
            and Decimal(reserve.available_points_delta) == -points
            and Decimal(reserve.reserved_points_delta) == points
            and reserve.actor_admin_id is None
            and reserve.reason is None
        )
        if not valid_reserve:
            raise RuntimeError("订单积分预占证据不完整")

        consume_key = (
            f"order:{order.order_public_id}:grant:"
            f"{allocation.points_grant_id}:consume"
        )
        consume = by_key.get(consume_key)
        if not expect_fulfilled:
            if consume is not None:
                raise RuntimeError("订单状态与积分消费证据不一致")
            continue
        valid_consume = consume is not None and (
            consume.grant_id == allocation.points_grant_id
            and consume.entry_type == PointsLedgerEntryType.CONSUME.value
            and Decimal(consume.available_points_delta) == ZERO
            and Decimal(consume.reserved_points_delta) == -points
            and consume.actor_admin_id == fulfillment_actor_admin_id
            and consume.reason == POINTS_CONSUME_REASON
        )
        if not valid_consume:
            raise RuntimeError("订单积分消费证据不完整")
        consume_ids.append(consume.id)
    return tuple(consume_ids)


def _audit_description(order):
    return (
        f"订单 {order.order_public_id} 确认履约；"
        f"积分 {Decimal(order.total_points):.2f}；"
        f"商品数量 {order.total_quantity}"
    )


def _validate_fulfillment_audit(db, *, order, expect_fulfilled):
    from ..models import AdminActionLog

    logs = (
        db.query(AdminActionLog)
        .filter(
            AdminActionLog.action_type
            == MallAuditActionType.ORDER_FULFILL.value,
            AdminActionLog.target_type == "mall_order",
            AdminActionLog.target_id == order.id,
        )
        .order_by(AdminActionLog.id.asc())
        .all()
    )
    if not expect_fulfilled:
        if logs:
            raise RuntimeError("订单状态与履约审计证据不一致")
        return None
    if (
        len(logs) != 1
        or logs[0].admin_id is None
        or logs[0].description != _audit_description(order)
    ):
        raise RuntimeError("订单履约审计证据不完整")
    return logs[0]


def _load_locked_resources(db, *, order):
    from ..models import (
        InventoryBalance,
        OrderItem,
        OrderPointsGrantAllocation,
        PointsAccount,
        PointsGrant,
        ProductSku,
    )

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

    account = (
        db.query(PointsAccount)
        .filter(PointsAccount.member_id == order.member_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if account is None:
        raise RuntimeError("订单会员积分账户不存在")
    sku_ids = tuple(item.sku_id for item in items)
    skus = tuple(
        db.query(ProductSku)
        .filter(ProductSku.id.in_(sku_ids))
        .order_by(ProductSku.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    balances = tuple(
        db.query(InventoryBalance)
        .filter(InventoryBalance.sku_id.in_(sku_ids))
        .order_by(InventoryBalance.sku_id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    if len(skus) != len(items) or len(balances) != len(items):
        raise RuntimeError("订单库存预占证据不完整")
    for sku_id in sku_ids:
        assert_inventory_balance_consistent(db, sku_id=sku_id)

    grant_ids = tuple(
        allocation.points_grant_id for allocation in allocations
    )
    grants = tuple(
        db.query(PointsGrant)
        .filter(PointsGrant.id.in_(grant_ids))
        .order_by(PointsGrant.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    if (
        len(grants) != len(allocations)
        or any(grant.account_id != account.id for grant in grants)
    ):
        raise RuntimeError("订单积分批次证据不完整")
    assert_points_account_balance_consistent(db, account_id=account.id)
    return items, allocations, account, grants


def fulfill_created_order(
    db,
    *,
    actor_admin_id: int,
    order_public_id,
    now=None,
) -> OrderFulfillmentResult:
    """确认 CREATED 订单履约并原子消费积分、出库。

    调用方负责提交或整体回滚。
    """
    from ..models import AdminActionLog, Order, PointsLedgerEntry

    normalized_order_public_id = _normalize_required_text(
        order_public_id,
        field_name="订单编号",
        maximum_length=32,
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
    if order.status not in (ORDER_CREATED_STATUS, ORDER_FULFILLING_STATUS):
        raise ValueError("当前订单状态不允许确认履约")

    actor = _require_actor(db, actor_admin_id=actor_admin_id)
    items, allocations, account, grants = _load_locked_resources(
        db,
        order=order,
    )
    replayed = order.status == ORDER_FULFILLING_STATUS
    fulfillment_log = _validate_fulfillment_audit(
        db,
        order=order,
        expect_fulfilled=replayed,
    )
    evidence_actor_id = None if fulfillment_log is None else fulfillment_log.admin_id
    inventory_outbound_ids = _validate_inventory_evidence(
        db,
        order=order,
        items=items,
        expect_fulfilled=replayed,
        fulfillment_actor_admin_id=evidence_actor_id,
    )
    points_consume_ids = _validate_points_evidence(
        db,
        order=order,
        allocations=allocations,
        expect_fulfilled=replayed,
        fulfillment_actor_admin_id=evidence_actor_id,
    )
    if replayed:
        return OrderFulfillmentResult(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            consumed_points=Decimal(order.total_points),
            points_consume_entry_ids=points_consume_ids,
            inventory_outbound_movement_ids=inventory_outbound_ids,
            action_log_id=fulfillment_log.id,
            replayed=True,
        )

    inventory_outbound_ids = []
    for item in items:
        result = outbound_reserved_inventory_for_order(
            db,
            actor_admin_id=actor.id,
            member_id=order.member_id,
            sku_id=item.sku_id,
            quantity=item.quantity,
            order_public_id=order.order_public_id,
            idempotency_key=(
                f"order:{order.order_public_id}:sku:{item.sku_id}:outbound"
            ),
            now=operation_time,
        )
        if result.replayed:
            raise RuntimeError("订单状态与库存出库证据不一致")
        inventory_outbound_ids.append(result.movement.id)

    if Decimal(account.reserved_points) < Decimal(order.total_points):
        raise RuntimeError("订单积分预占不足，无法消费")
    grants_by_id = {grant.id: grant for grant in grants}
    points_consume_ids = []
    for allocation in allocations:
        grant = grants_by_id[allocation.points_grant_id]
        points = Decimal(allocation.allocated_points)
        if grant.status != PointsGrantStatus.ACTIVE.value:
            raise RuntimeError("订单积分批次状态不允许消费")
        if Decimal(grant.reserved_points) < points:
            raise RuntimeError("订单积分预占不足，无法消费")
        grant.reserved_points = Decimal(grant.reserved_points) - points
        if (
            Decimal(grant.available_points) == ZERO
            and Decimal(grant.reserved_points) == ZERO
        ):
            grant.status = PointsGrantStatus.EXHAUSTED.value
        grant.updated_at = operation_time
        entry = PointsLedgerEntry(
            grant_id=grant.id,
            entry_type=PointsLedgerEntryType.CONSUME.value,
            available_points_delta=ZERO,
            reserved_points_delta=-points,
            idempotency_key=(
                f"order:{order.order_public_id}:grant:{grant.id}:consume"
            ),
            reference_type=ORDER_REFERENCE_TYPE,
            reference_id=order.order_public_id,
            actor_admin_id=actor.id,
            reason=POINTS_CONSUME_REASON,
            created_at=operation_time,
        )
        db.add(entry)
        db.flush()
        points_consume_ids.append(entry.id)

    account.reserved_points = (
        Decimal(account.reserved_points) - Decimal(order.total_points)
    )
    account.version = (account.version or 0) + 1
    account.updated_at = operation_time
    order.status = ORDER_FULFILLING_STATUS
    order.updated_at = operation_time
    db.flush()
    fulfillment_log = AdminActionLog(
        admin_id=actor.id,
        action_type=MallAuditActionType.ORDER_FULFILL.value,
        target_type="mall_order",
        target_id=order.id,
        description=_audit_description(order),
        created_at=operation_time,
    )
    db.add(fulfillment_log)
    db.flush()
    assert_points_account_balance_consistent(db, account_id=account.id)

    verified_inventory_ids = _validate_inventory_evidence(
        db,
        order=order,
        items=items,
        expect_fulfilled=True,
        fulfillment_actor_admin_id=actor.id,
    )
    verified_points_ids = _validate_points_evidence(
        db,
        order=order,
        allocations=allocations,
        expect_fulfilled=True,
        fulfillment_actor_admin_id=actor.id,
    )
    verified_log = _validate_fulfillment_audit(
        db,
        order=order,
        expect_fulfilled=True,
    )
    if (
        verified_inventory_ids != tuple(inventory_outbound_ids)
        or verified_points_ids != tuple(points_consume_ids)
        or verified_log.id != fulfillment_log.id
    ):
        raise RuntimeError("订单履约证据不完整")
    return OrderFulfillmentResult(
        order_id=order.id,
        order_public_id=order.order_public_id,
        status=order.status,
        consumed_points=Decimal(order.total_points),
        points_consume_entry_ids=verified_points_ids,
        inventory_outbound_movement_ids=verified_inventory_ids,
        action_log_id=verified_log.id,
        replayed=False,
    )


def execute_order_fulfillment(engine, **request) -> OrderFulfillmentResult:
    """在独立事务中确认履约；异常时自动整体回滚。"""
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
                result = fulfill_created_order(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
