"""已创建纯积分订单的原子取消及积分、库存预占释放服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .domain import PointsGrantStatus, PointsLedgerEntryType
from .inventory_service import (
    assert_inventory_balance_consistent,
    release_inventory_for_order,
)
from .points_ledger_service import assert_points_account_balance_consistent


ORDER_REFERENCE_TYPE = "ORDER"
ORDER_CREATED_STATUS = "CREATED"
ORDER_CANCELLED_STATUS = "CANCELLED"
POINTS_RELEASE_REASON = "订单取消释放积分"
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class OrderCancellationResult:
    """订单首次取消或幂等重放后的稳定结果。"""

    order_id: int
    order_public_id: str
    status: str
    released_points: Decimal
    points_release_entry_ids: tuple[int, ...]
    inventory_release_movement_ids: tuple[int, ...]
    replayed: bool


def _normalize_positive_integer(value, *, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name}必须是正整数")
    return value


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
        raise ValueError("取消时间无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


def _validate_order_totals(order, items, allocations):
    item_points = sum((Decimal(item.line_points) for item in items), ZERO)
    item_quantity = sum(item.quantity for item in items)
    allocated_points = sum(
        (Decimal(allocation.allocated_points) for allocation in allocations),
        ZERO,
    )
    if (
        not items
        or not allocations
        or item_points != Decimal(order.total_points)
        or allocated_points != Decimal(order.total_points)
        or item_quantity != order.total_quantity
    ):
        raise RuntimeError("订单资源预占证据不完整")


def _validate_inventory_evidence(
    db,
    *,
    order,
    items,
    expect_released,
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
    expected_count = len(items) * (2 if expect_released else 1)
    if len(movements) != expected_count:
        raise RuntimeError("订单库存预占或释放证据不完整")

    by_key = {movement.idempotency_key: movement for movement in movements}
    if len(by_key) != len(movements):
        raise RuntimeError("订单库存预占或释放证据冲突")

    release_ids = []
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

        release_key = (
            f"order:{order.order_public_id}:sku:{item.sku_id}:release"
        )
        release = by_key.get(release_key)
        if not expect_released:
            if release is not None:
                raise RuntimeError("订单状态与库存释放证据不一致")
            continue
        valid_release = release is not None and (
            release.sku_id == item.sku_id
            and release.movement_type == "RELEASE"
            and release.quantity_delta == 0
            and release.reserved_quantity_delta == -item.quantity
            and release.actor_admin_id is None
            and release.actor_member_id == order.member_id
            and release.reason
            == f"订单 {order.order_public_id} 取消释放库存"
        )
        if not valid_release:
            raise RuntimeError("订单库存释放证据不完整")
        release_ids.append(release.id)
    return tuple(release_ids)


def _validate_points_evidence(
    db,
    *,
    order,
    allocations,
    expect_released,
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
    expected_count = len(allocations) * (2 if expect_released else 1)
    if len(entries) != expected_count:
        raise RuntimeError("订单积分预占或释放证据不完整")

    by_key = {entry.idempotency_key: entry for entry in entries}
    if len(by_key) != len(entries):
        raise RuntimeError("订单积分预占或释放证据冲突")

    release_ids = []
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

        release_key = (
            f"order:{order.order_public_id}:grant:"
            f"{allocation.points_grant_id}:release"
        )
        release = by_key.get(release_key)
        if not expect_released:
            if release is not None:
                raise RuntimeError("订单状态与积分释放证据不一致")
            continue
        valid_release = release is not None and (
            release.grant_id == allocation.points_grant_id
            and release.entry_type == PointsLedgerEntryType.RELEASE.value
            and Decimal(release.available_points_delta) == points
            and Decimal(release.reserved_points_delta) == -points
            and release.actor_admin_id is None
            and release.reason == POINTS_RELEASE_REASON
        )
        if not valid_release:
            raise RuntimeError("订单积分释放证据不完整")
        release_ids.append(release.id)
    return tuple(release_ids)


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
    grants = tuple(
        db.query(PointsGrant)
        .filter(
            PointsGrant.id.in_(
                allocation.points_grant_id for allocation in allocations
            )
        )
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


def cancel_created_order(
    db,
    *,
    member_id: int,
    order_public_id,
    now=None,
) -> OrderCancellationResult:
    """取消 CREATED 订单并原子释放库存、积分预占；调用方负责提交。"""
    from ..models import Member, Order, PointsLedgerEntry

    normalized_member_id = _normalize_positive_integer(
        member_id,
        field_name="会员",
    )
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
    if order is None or order.member_id != normalized_member_id:
        raise ValueError("订单不存在")
    if order.status not in (ORDER_CREATED_STATUS, ORDER_CANCELLED_STATUS):
        raise ValueError("当前订单状态不允许取消")

    member = (
        db.query(Member)
        .filter(Member.id == normalized_member_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if member is None:
        raise ValueError("会员不存在")
    if order.status == ORDER_CREATED_STATUS and member.is_active is not True:
        raise ValueError("会员不存在或已停用")

    items, allocations, account, grants = _load_locked_resources(
        db,
        order=order,
    )
    replayed = order.status == ORDER_CANCELLED_STATUS
    inventory_release_ids = _validate_inventory_evidence(
        db,
        order=order,
        items=items,
        expect_released=replayed,
    )
    points_release_ids = _validate_points_evidence(
        db,
        order=order,
        allocations=allocations,
        expect_released=replayed,
    )
    if replayed:
        return OrderCancellationResult(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            released_points=Decimal(order.total_points),
            points_release_entry_ids=points_release_ids,
            inventory_release_movement_ids=inventory_release_ids,
            replayed=True,
        )

    inventory_release_ids = []
    for item in items:
        result = release_inventory_for_order(
            db,
            member_id=member.id,
            sku_id=item.sku_id,
            quantity=item.quantity,
            order_public_id=order.order_public_id,
            idempotency_key=(
                f"order:{order.order_public_id}:sku:{item.sku_id}:release"
            ),
            now=operation_time,
        )
        if result.replayed:
            raise RuntimeError("订单状态与库存释放证据不一致")
        inventory_release_ids.append(result.movement.id)

    grants_by_id = {grant.id: grant for grant in grants}
    if Decimal(account.reserved_points) < Decimal(order.total_points):
        raise RuntimeError("订单积分预占不足，无法释放")
    points_release_ids = []
    for allocation in allocations:
        grant = grants_by_id[allocation.points_grant_id]
        points = Decimal(allocation.allocated_points)
        if grant.status not in (
            PointsGrantStatus.ACTIVE.value,
            PointsGrantStatus.FROZEN.value,
        ):
            raise RuntimeError("订单积分批次状态不允许释放")
        if Decimal(grant.reserved_points) < points:
            raise RuntimeError("订单积分预占不足，无法释放")
        grant.available_points = Decimal(grant.available_points) + points
        grant.reserved_points = Decimal(grant.reserved_points) - points
        grant.updated_at = operation_time
        entry = PointsLedgerEntry(
            grant_id=grant.id,
            entry_type=PointsLedgerEntryType.RELEASE.value,
            available_points_delta=points,
            reserved_points_delta=-points,
            idempotency_key=(
                f"order:{order.order_public_id}:grant:{grant.id}:release"
            ),
            reference_type=ORDER_REFERENCE_TYPE,
            reference_id=order.order_public_id,
            actor_admin_id=None,
            reason=POINTS_RELEASE_REASON,
            created_at=operation_time,
        )
        db.add(entry)
        db.flush()
        points_release_ids.append(entry.id)

    account.available_points = (
        Decimal(account.available_points) + Decimal(order.total_points)
    )
    account.reserved_points = (
        Decimal(account.reserved_points) - Decimal(order.total_points)
    )
    account.version = (account.version or 0) + 1
    account.updated_at = operation_time
    order.status = ORDER_CANCELLED_STATUS
    order.updated_at = operation_time
    db.flush()
    assert_points_account_balance_consistent(db, account_id=account.id)

    verified_inventory_ids = _validate_inventory_evidence(
        db,
        order=order,
        items=items,
        expect_released=True,
    )
    verified_points_ids = _validate_points_evidence(
        db,
        order=order,
        allocations=allocations,
        expect_released=True,
    )
    if (
        verified_inventory_ids != tuple(inventory_release_ids)
        or verified_points_ids != tuple(points_release_ids)
    ):
        raise RuntimeError("订单取消释放证据不完整")
    return OrderCancellationResult(
        order_id=order.id,
        order_public_id=order.order_public_id,
        status=order.status,
        released_points=Decimal(order.total_points),
        points_release_entry_ids=verified_points_ids,
        inventory_release_movement_ids=verified_inventory_ids,
        replayed=False,
    )


def execute_order_cancellation(engine, **request) -> OrderCancellationResult:
    """在独立事务中取消订单；异常时自动整体回滚。"""
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
                result = cancel_created_order(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
