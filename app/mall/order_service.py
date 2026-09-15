"""纯积分订单创建及积分、库存原子预占领域服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import secrets

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .domain import PointsGrantStatus, PointsLedgerEntryType, ProductStatus
from .inventory_service import reserve_inventory_for_order
from .points_ledger_service import assert_points_account_balance_consistent


ORDER_PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ORDER_PUBLIC_ID_RANDOM_LENGTH = 16
ORDER_PUBLIC_ID_GENERATION_ATTEMPTS = 16
ORDER_REFERENCE_TYPE = "ORDER"
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class OrderLineRequest:
    """一个 SKU 及其兑换数量。"""

    sku_id: int
    quantity: int


@dataclass(frozen=True)
class PointsGrantReservation:
    """本次订单按 FEFO 预占的一个积分批次。"""

    grant_id: int
    allocated_points: Decimal


@dataclass(frozen=True)
class OrderPlacementResult:
    """订单创建或幂等重放后的稳定结果。"""

    order_id: int
    order_public_id: str
    status: str
    total_points: Decimal
    total_cost_amount: Decimal
    total_quantity: int
    item_count: int
    points_reservations: tuple[PointsGrantReservation, ...]
    inventory_movement_ids: tuple[int, ...]
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
        raise ValueError("下单时间无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


def _normalize_lines(lines) -> tuple[OrderLineRequest, ...]:
    if isinstance(lines, (str, bytes)):
        raise ValueError("订单商品不能为空")
    try:
        submitted = tuple(lines)
    except TypeError as exc:
        raise ValueError("订单商品不能为空") from exc
    if not submitted:
        raise ValueError("订单商品不能为空")

    normalized: list[OrderLineRequest] = []
    seen_sku_ids: set[int] = set()
    for line in submitted:
        if not isinstance(line, OrderLineRequest):
            raise ValueError("订单商品格式无效")
        sku_id = _normalize_positive_integer(
            line.sku_id,
            field_name="商品 SKU",
        )
        quantity = _normalize_positive_integer(
            line.quantity,
            field_name="兑换数量",
        )
        if sku_id in seen_sku_ids:
            raise ValueError("同一商品 SKU 不能重复提交")
        seen_sku_ids.add(sku_id)
        normalized.append(OrderLineRequest(sku_id=sku_id, quantity=quantity))
    return tuple(sorted(normalized, key=lambda item: item.sku_id))


def _generate_order_public_id(db) -> str:
    from ..models import Order

    for _attempt in range(ORDER_PUBLIC_ID_GENERATION_ATTEMPTS):
        random_part = "".join(
            secrets.choice(ORDER_PUBLIC_ID_ALPHABET)
            for _ in range(ORDER_PUBLIC_ID_RANDOM_LENGTH)
        )
        value = f"ORD-{random_part}"
        with db.no_autoflush:
            exists = db.query(Order.id).filter(
                Order.order_public_id == value
            ).first()
        if exists is None:
            return value
    raise RuntimeError("无法生成唯一的订单编号")


def _build_result(db, order, *, replayed):
    from ..models import (
        InventoryMovement,
        OrderItem,
        OrderPointsGrantAllocation,
    )

    allocations = (
        db.query(OrderPointsGrantAllocation)
        .filter(OrderPointsGrantAllocation.order_id == order.id)
        .order_by(OrderPointsGrantAllocation.id.asc())
        .all()
    )
    movements = (
        db.query(InventoryMovement.id)
        .filter(
            InventoryMovement.reference_type == ORDER_REFERENCE_TYPE,
            InventoryMovement.reference_id == order.order_public_id,
            InventoryMovement.movement_type == "RESERVE",
        )
        .order_by(InventoryMovement.id.asc())
        .all()
    )
    return OrderPlacementResult(
        order_id=order.id,
        order_public_id=order.order_public_id,
        status=order.status,
        total_points=Decimal(order.total_points),
        total_cost_amount=Decimal(order.total_cost_amount),
        total_quantity=order.total_quantity,
        item_count=db.query(OrderItem.id).filter(
            OrderItem.order_id == order.id
        ).count(),
        points_reservations=tuple(
            PointsGrantReservation(
                grant_id=allocation.points_grant_id,
                allocated_points=Decimal(allocation.allocated_points),
            )
            for allocation in allocations
        ),
        inventory_movement_ids=tuple(row.id for row in movements),
        replayed=replayed,
    )


def _validate_replay(db, order, *, member_id, lines):
    from ..models import OrderItem

    stored_lines = tuple(
        db.query(OrderItem.sku_id, OrderItem.quantity)
        .filter(OrderItem.order_id == order.id)
        .order_by(OrderItem.sku_id.asc())
        .all()
    )
    requested_lines = tuple((line.sku_id, line.quantity) for line in lines)
    normalized_stored = tuple(
        (line.sku_id, line.quantity) for line in stored_lines
    )
    if order.member_id != member_id or normalized_stored != requested_lines:
        raise ValueError("订单幂等键已用于其他请求")


def place_order_with_reservations(
    db,
    *,
    member_id: int,
    lines,
    idempotency_key,
    now=None,
) -> OrderPlacementResult:
    """
    创建纯积分订单并在同一事务中预占库存与 FEFO 积分。

    本函数不提交事务；调用方必须整体提交，任何异常必须整体回滚。
    """
    from ..models import (
        Member,
        Order,
        OrderItem,
        OrderPointsGrantAllocation,
        PointsAccount,
        PointsGrant,
        PointsLedgerEntry,
        Product,
        ProductCategory,
        ProductMedia,
        ProductSku,
        Supplier,
    )

    normalized_member_id = _normalize_positive_integer(
        member_id,
        field_name="会员",
    )
    normalized_lines = _normalize_lines(lines)
    normalized_key = _normalize_required_text(
        idempotency_key,
        field_name="订单幂等键",
        maximum_length=128,
    )
    operation_time = _current_time(db, now)

    existing = (
        db.query(Order)
        .filter(Order.idempotency_key == normalized_key)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if existing is not None:
        _validate_replay(
            db,
            existing,
            member_id=normalized_member_id,
            lines=normalized_lines,
        )
        return _build_result(db, existing, replayed=True)

    member = (
        db.query(Member)
        .filter(Member.id == normalized_member_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if member is None or member.is_active is not True:
        raise ValueError("会员不存在或已停用")

    account = (
        db.query(PointsAccount)
        .filter(PointsAccount.member_id == member.id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if account is None:
        raise ValueError("会员积分账户不存在")
    assert_points_account_balance_consistent(db, account_id=account.id)

    sku_ids = tuple(line.sku_id for line in normalized_lines)
    catalog_rows = (
        db.query(ProductSku, Product, Supplier, ProductCategory)
        .join(Product, Product.id == ProductSku.product_id)
        .join(Supplier, Supplier.id == ProductSku.supplier_id)
        .join(ProductCategory, ProductCategory.id == Product.category_id)
        .filter(ProductSku.id.in_(sku_ids))
        .order_by(ProductSku.id.asc())
        .with_for_update()
        .all()
    )
    if len(catalog_rows) != len(normalized_lines):
        raise ValueError("订单包含不存在的商品 SKU")

    catalog_by_sku_id = {
        sku.id: (sku, product, supplier, category)
        for sku, product, supplier, category in catalog_rows
    }
    product_ids = tuple(
        sorted({product.id for _sku, product, _supplier, _category in catalog_rows})
    )
    main_media_rows = (
        db.query(ProductMedia)
        .filter(
            ProductMedia.product_id.in_(product_ids),
            ProductMedia.media_role == "MAIN",
            ProductMedia.is_active.is_(True),
        )
        .with_for_update()
        .all()
    )
    main_image_by_product_id = {
        media.product_id: media.image_path for media in main_media_rows
    }
    total_points = ZERO
    total_cost = ZERO
    total_quantity = 0
    prepared_items = []
    for line in normalized_lines:
        sku, product, supplier, category = catalog_by_sku_id[line.sku_id]
        if (
            product.status != ProductStatus.PUBLISHED.value
            or sku.is_active is not True
            or supplier.is_active is not True
            or category.is_active is not True
        ):
            raise ValueError("订单包含不可兑换的商品 SKU")
        unit_points = Decimal(sku.points_price)
        unit_cost = Decimal(sku.cost_price)
        line_points = unit_points * line.quantity
        line_cost = unit_cost * line.quantity
        total_points += line_points
        total_cost += line_cost
        total_quantity += line.quantity
        prepared_items.append(
            (line, sku, product, supplier, line_points, line_cost)
        )

    order = Order(
        order_public_id=_generate_order_public_id(db),
        idempotency_key=normalized_key,
        member_id=member.id,
        status="CREATED",
        total_points=total_points,
        total_cost_amount=total_cost,
        total_quantity=total_quantity,
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(order)
    db.flush()

    for line, sku, product, supplier, line_points, line_cost in prepared_items:
        db.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            sku_id=sku.id,
            supplier_id=supplier.id,
            product_public_id_snapshot=product.product_public_id,
            product_name_snapshot=product.name,
            sku_code_snapshot=sku.sku_code,
            sku_name_snapshot=sku.name,
            supplier_public_id_snapshot=supplier.supplier_public_id,
            supplier_name_snapshot=supplier.name,
            supplier_sku_code_snapshot=sku.supplier_sku_code,
            product_image_path_snapshot=main_image_by_product_id.get(
                product.id
            ),
            unit_points_price=sku.points_price,
            unit_cost_price=sku.cost_price,
            quantity=line.quantity,
            line_points=line_points,
            line_cost_amount=line_cost,
            created_at=operation_time,
        ))
    db.flush()

    inventory_movement_ids: list[int] = []
    for line in normalized_lines:
        reservation = reserve_inventory_for_order(
            db,
            member_id=member.id,
            sku_id=line.sku_id,
            quantity=line.quantity,
            order_public_id=order.order_public_id,
            idempotency_key=(
                f"order:{order.order_public_id}:sku:{line.sku_id}:reserve"
            ),
            now=operation_time,
        )
        inventory_movement_ids.append(reservation.movement.id)

    grants = (
        db.query(PointsGrant)
        .filter(
            PointsGrant.account_id == account.id,
            PointsGrant.status == PointsGrantStatus.ACTIVE.value,
            PointsGrant.expires_at > operation_time,
            PointsGrant.available_points > ZERO,
        )
        .order_by(PointsGrant.expires_at.asc(), PointsGrant.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    qualifying_points = sum(
        (Decimal(grant.available_points) for grant in grants),
        ZERO,
    )
    if (
        Decimal(account.available_points) < total_points
        or qualifying_points < total_points
    ):
        raise ValueError("可用积分不足")

    remaining = total_points
    for grant in grants:
        if remaining == ZERO:
            break
        available = Decimal(grant.available_points)
        allocated = min(available, remaining)
        if allocated <= ZERO:
            continue
        grant.available_points = available - allocated
        grant.reserved_points = Decimal(grant.reserved_points) + allocated
        grant.updated_at = operation_time
        allocation = OrderPointsGrantAllocation(
            order_id=order.id,
            points_grant_id=grant.id,
            allocated_points=allocated,
            created_at=operation_time,
        )
        db.add(allocation)
        db.add(PointsLedgerEntry(
            grant_id=grant.id,
            entry_type=PointsLedgerEntryType.RESERVE.value,
            available_points_delta=-allocated,
            reserved_points_delta=allocated,
            idempotency_key=(
                f"order:{order.order_public_id}:grant:{grant.id}:reserve"
            ),
            reference_type=ORDER_REFERENCE_TYPE,
            reference_id=order.order_public_id,
            actor_admin_id=None,
            reason=None,
            created_at=operation_time,
        ))
        remaining -= allocated

    account.available_points = Decimal(account.available_points) - total_points
    account.reserved_points = Decimal(account.reserved_points) + total_points
    account.version = (account.version or 0) + 1
    account.updated_at = operation_time
    db.flush()
    assert_points_account_balance_consistent(db, account_id=account.id)

    result = _build_result(db, order, replayed=False)
    if result.inventory_movement_ids != tuple(inventory_movement_ids):
        raise RuntimeError("订单库存预占证据不完整")
    return result


def execute_order_placement(engine, **request) -> OrderPlacementResult:
    """在独立事务中执行下单；异常时自动整体回滚。"""
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
                result = place_order_with_reservations(db, **request)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
