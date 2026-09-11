"""SKU 库存余额、不可变流水及账实一致性服务。"""

from dataclasses import dataclass
from datetime import datetime
import secrets

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import (
    InventoryMovementType,
    InventoryStockStatus,
    classify_inventory_stock,
    normalize_inventory_movement_type,
)


INVENTORY_PERMISSION_MESSAGE = "当前账号无权管理商城库存"
INVENTORY_BALANCE_MISMATCH_MESSAGE = "SKU 库存余额与流水不一致"
MOVEMENT_PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MOVEMENT_PUBLIC_ID_RANDOM_LENGTH = 16
MOVEMENT_PUBLIC_ID_GENERATION_ATTEMPTS = 16


@dataclass(frozen=True)
class InventoryStockSnapshot:
    """一个 SKU 当前可审计的库存状态。"""

    sku_id: int
    sku_code: str
    on_hand_quantity: int
    reserved_quantity: int
    available_quantity: int
    low_stock_threshold: int
    stock_status: InventoryStockStatus
    balance_version: int


@dataclass(frozen=True)
class InventoryMutationResult:
    """一次库存入库或调整的结果。"""

    snapshot: InventoryStockSnapshot
    movement: object
    action_log: object | None
    replayed: bool


@dataclass(frozen=True)
class InventoryBalanceAudit:
    """库存余额与顺序流水的重算结果。"""

    snapshot: InventoryStockSnapshot
    ledger_quantity: int
    ledger_version: int
    movement_count: int
    is_consistent: bool


def _normalize_required_text(value, *, field_name, maximum_length):
    if value is None:
        raise ValueError(f"{field_name}不能为空")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name}不能超过 {maximum_length} 个字符")
    return normalized


def _normalize_positive_integer(value, *, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name}必须是正整数")
    return value


def _normalize_nonzero_integer(value, *, field_name):
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise ValueError(f"{field_name}必须是非零整数")
    return value


def _database_time(db, value):
    if db.get_bind().dialect.name == "postgresql":
        return value.replace(tzinfo=UTC8_TIMEZONE)
    return value


def _current_time(db, value):
    current = utc8_now() if value is None else value
    if not isinstance(current, datetime):
        raise ValueError("库存操作时间无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


def _require_actor(db, *, actor_admin_id, action_type):
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import User

    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(INVENTORY_PERMISSION_MESSAGE)
    with db.no_autoflush:
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
        raise PermissionError(INVENTORY_PERMISSION_MESSAGE)
    return actor


def _lock_sku(db, sku_id):
    from ..models import ProductSku

    if isinstance(sku_id, bool) or not isinstance(sku_id, int) or sku_id <= 0:
        raise ValueError("商品 SKU 无效")
    with db.no_autoflush:
        sku = (
            db.query(ProductSku)
            .filter(ProductSku.id == sku_id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
    if sku is None:
        raise ValueError("商品 SKU 不存在")
    return sku


def _generate_movement_public_id(db):
    from ..models import InventoryMovement

    for _attempt in range(MOVEMENT_PUBLIC_ID_GENERATION_ATTEMPTS):
        random_part = "".join(
            secrets.choice(MOVEMENT_PUBLIC_ID_ALPHABET)
            for _ in range(MOVEMENT_PUBLIC_ID_RANDOM_LENGTH)
        )
        value = f"IMV-{random_part}"
        with db.no_autoflush:
            exists = (
                db.query(InventoryMovement.id)
                .filter(InventoryMovement.movement_public_id == value)
                .first()
            )
        if exists is None:
            return value
    raise RuntimeError("无法生成唯一的库存流水编号")


def _record_audit(
    db,
    *,
    actor,
    action_type,
    movement,
    sku_code,
    operation_time,
):
    from ..models import AdminActionLog

    action_label = (
        "入库"
        if action_type is MallAuditActionType.INVENTORY_RECEIVE
        else "调整"
    )
    signed_delta = f"{movement.quantity_delta:+d}"
    log = AdminActionLog(
        admin_id=actor.id,
        action_type=action_type.value,
        target_type="inventory_movement",
        target_id=movement.id,
        description=(
            f"SKU {sku_code} 库存{action_label} {signed_delta}；"
            f"现存数量 {movement.quantity_before} -> "
            f"{movement.quantity_after}；"
            f"流水 {movement.movement_public_id}"
        ),
        created_at=operation_time,
    )
    db.add(log)
    db.flush()
    return log


def _build_snapshot(sku, balance) -> InventoryStockSnapshot:
    on_hand_quantity = 0 if balance is None else balance.on_hand_quantity
    reserved_quantity = 0 if balance is None else balance.reserved_quantity
    balance_version = 0 if balance is None else balance.version
    return InventoryStockSnapshot(
        sku_id=sku.id,
        sku_code=sku.sku_code,
        on_hand_quantity=on_hand_quantity,
        reserved_quantity=reserved_quantity,
        available_quantity=on_hand_quantity - reserved_quantity,
        low_stock_threshold=sku.low_stock_threshold,
        stock_status=classify_inventory_stock(
            on_hand_quantity=on_hand_quantity,
            reserved_quantity=reserved_quantity,
            low_stock_threshold=sku.low_stock_threshold,
        ),
        balance_version=balance_version,
    )


def _find_idempotent_movement(db, idempotency_key):
    from ..models import InventoryMovement

    with db.no_autoflush:
        return (
            db.query(InventoryMovement)
            .filter(InventoryMovement.idempotency_key == idempotency_key)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )


def _validate_idempotent_replay(
    movement,
    *,
    actor_admin_id,
    sku_id,
    movement_type,
    quantity_delta,
    reason,
):
    if (
        movement.actor_admin_id != actor_admin_id
        or movement.sku_id != sku_id
        or movement.movement_type != movement_type.value
        or movement.quantity_delta != quantity_delta
        or movement.reason != reason
    ):
        raise ValueError("库存幂等键已用于其他操作")


def _apply_inventory_movement(
    db,
    *,
    actor_admin_id,
    sku_id,
    movement_type,
    quantity_delta,
    reason,
    idempotency_key,
    now=None,
) -> InventoryMutationResult:
    from ..models import InventoryBalance, InventoryMovement

    normalized_type = normalize_inventory_movement_type(movement_type)
    normalized_delta = _normalize_nonzero_integer(
        quantity_delta,
        field_name="库存变动数量",
    )
    if (
        normalized_type is InventoryMovementType.RECEIPT
        and normalized_delta <= 0
    ):
        raise ValueError("入库数量必须是正整数")
    normalized_reason = _normalize_required_text(
        reason,
        field_name="库存变动原因",
        maximum_length=500,
    )
    normalized_idempotency_key = _normalize_required_text(
        idempotency_key,
        field_name="库存幂等键",
        maximum_length=128,
    )
    action_type = (
        MallAuditActionType.INVENTORY_RECEIVE
        if normalized_type is InventoryMovementType.RECEIPT
        else MallAuditActionType.INVENTORY_ADJUST
    )
    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=action_type,
    )
    sku = _lock_sku(db, sku_id)

    existing = _find_idempotent_movement(
        db,
        normalized_idempotency_key,
    )
    if existing is not None:
        _validate_idempotent_replay(
            existing,
            actor_admin_id=actor.id,
            sku_id=sku.id,
            movement_type=normalized_type,
            quantity_delta=normalized_delta,
            reason=normalized_reason,
        )
        audit = assert_inventory_balance_consistent(db, sku_id=sku.id)
        return InventoryMutationResult(
            snapshot=audit.snapshot,
            movement=existing,
            action_log=None,
            replayed=True,
        )

    assert_inventory_balance_consistent(db, sku_id=sku.id)

    with db.no_autoflush:
        balance = (
            db.query(InventoryBalance)
            .filter(InventoryBalance.sku_id == sku.id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
    quantity_before = 0 if balance is None else balance.on_hand_quantity
    reserved_quantity = 0 if balance is None else balance.reserved_quantity
    version_before = 0 if balance is None else balance.version
    quantity_after = quantity_before + normalized_delta
    if quantity_after < reserved_quantity:
        raise ValueError("库存调整后不能低于已预占数量")

    operation_time = _current_time(db, now)
    if balance is None:
        balance = InventoryBalance(
            sku_id=sku.id,
            on_hand_quantity=quantity_after,
            reserved_quantity=0,
            version=1,
            created_at=operation_time,
            updated_at=operation_time,
        )
        db.add(balance)
    else:
        balance.on_hand_quantity = quantity_after
        balance.version = version_before + 1
        balance.updated_at = operation_time

    movement = InventoryMovement(
        movement_public_id=_generate_movement_public_id(db),
        sku_id=sku.id,
        movement_type=normalized_type.value,
        quantity_delta=normalized_delta,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        balance_version=version_before + 1,
        idempotency_key=normalized_idempotency_key,
        reason=normalized_reason,
        actor_admin_id=actor.id,
        created_at=operation_time,
    )
    db.add(movement)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=action_type,
        movement=movement,
        sku_code=sku.sku_code,
        operation_time=operation_time,
    )
    audit = assert_inventory_balance_consistent(db, sku_id=sku.id)
    return InventoryMutationResult(
        snapshot=audit.snapshot,
        movement=movement,
        action_log=action_log,
        replayed=False,
    )


def receive_inventory(
    db,
    *,
    actor_admin_id: int,
    sku_id: int,
    quantity,
    reason,
    idempotency_key,
    now=None,
) -> InventoryMutationResult:
    """按正整数数量入库；调用方负责提交或回滚。"""
    normalized_quantity = _normalize_positive_integer(
        quantity,
        field_name="入库数量",
    )
    return _apply_inventory_movement(
        db,
        actor_admin_id=actor_admin_id,
        sku_id=sku_id,
        movement_type=InventoryMovementType.RECEIPT,
        quantity_delta=normalized_quantity,
        reason=reason,
        idempotency_key=idempotency_key,
        now=now,
    )


def adjust_inventory(
    db,
    *,
    actor_admin_id: int,
    sku_id: int,
    quantity_delta,
    reason,
    idempotency_key,
    now=None,
) -> InventoryMutationResult:
    """以非零增量调整库存；调用方负责提交或回滚。"""
    return _apply_inventory_movement(
        db,
        actor_admin_id=actor_admin_id,
        sku_id=sku_id,
        movement_type=InventoryMovementType.ADJUSTMENT,
        quantity_delta=quantity_delta,
        reason=reason,
        idempotency_key=idempotency_key,
        now=now,
    )


def audit_inventory_balance(db, *, sku_id: int) -> InventoryBalanceAudit:
    """按版本顺序重算库存流水并与余额表核对。"""
    from ..models import InventoryBalance, InventoryMovement, ProductSku

    if isinstance(sku_id, bool) or not isinstance(sku_id, int) or sku_id <= 0:
        raise ValueError("商品 SKU 无效")
    sku = db.query(ProductSku).filter(ProductSku.id == sku_id).one_or_none()
    if sku is None:
        raise ValueError("商品 SKU 不存在")
    balance = (
        db.query(InventoryBalance)
        .filter(InventoryBalance.sku_id == sku.id)
        .one_or_none()
    )
    movements = (
        db.query(InventoryMovement)
        .filter(InventoryMovement.sku_id == sku.id)
        .order_by(InventoryMovement.balance_version, InventoryMovement.id)
        .all()
    )

    ledger_quantity = 0
    ledger_version = 0
    chain_is_consistent = True
    for movement in movements:
        expected_version = ledger_version + 1
        expected_after = ledger_quantity + movement.quantity_delta
        if (
            movement.balance_version != expected_version
            or movement.quantity_before != ledger_quantity
            or movement.quantity_after != expected_after
            or expected_after < 0
        ):
            chain_is_consistent = False
        ledger_quantity = movement.quantity_after
        ledger_version = movement.balance_version

    snapshot = _build_snapshot(sku, balance)
    is_consistent = (
        chain_is_consistent
        and snapshot.on_hand_quantity == ledger_quantity
        and snapshot.balance_version == ledger_version
    )
    return InventoryBalanceAudit(
        snapshot=snapshot,
        ledger_quantity=ledger_quantity,
        ledger_version=ledger_version,
        movement_count=len(movements),
        is_consistent=is_consistent,
    )


def assert_inventory_balance_consistent(
    db,
    *,
    sku_id: int,
) -> InventoryBalanceAudit:
    """库存余额无法由流水重算时失败关闭。"""
    result = audit_inventory_balance(db, sku_id=sku_id)
    if not result.is_consistent:
        raise RuntimeError(INVENTORY_BALANCE_MISMATCH_MESSAGE)
    return result
