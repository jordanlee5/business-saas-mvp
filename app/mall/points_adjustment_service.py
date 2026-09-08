"""超级管理员对既有积分批次进行人工纠错的只追加领域服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import (
    PointsGrantStatus,
    PointsLedgerEntryType,
    _to_utc8_wall_time,
    normalize_points,
)
from .points_ledger_service import (
    POINTS_BALANCE_MISMATCH_MESSAGE,
    _lock_points_grant_and_account,
    _normalize_required_text,
    assert_points_account_balance_consistent,
)


ZERO = Decimal("0.00")
ADJUSTMENT_KEY_PREFIX = "points-adjustment:"
ADJUSTMENT_REFERENCE_TYPE = "ADMIN_ACTION_LOG"
ADJUSTMENT_TARGET_TYPE = "points_grant"
ADJUSTMENT_PERMISSION_MESSAGE = "当前账号无权人工调整积分"


@dataclass(frozen=True)
class PointsAdjustmentResult:
    grant_id: int
    account_id: int
    ledger_entry_id: int
    admin_action_log_id: int
    created: bool
    delta_points: Decimal
    grant_available_points: Decimal
    grant_reserved_points: Decimal
    account_available_points: Decimal
    account_reserved_points: Decimal
    grant_status: str


def _normalize_reason(value) -> str:
    normalized = _normalize_required_text(
        value,
        field_name="积分调整原因",
        maximum_length=500,
    )
    return " ".join(normalized.split())


def _normalize_request_id(value) -> tuple[str, str]:
    request_id = _normalize_required_text(
        value,
        field_name="积分调整请求号",
        maximum_length=100 - len(ADJUSTMENT_KEY_PREFIX),
    )
    return request_id, ADJUSTMENT_KEY_PREFIX + request_id


def _database_time(db, value):
    # SQLite 存 UTC+8 墙上时间；PostgreSQL 的 timestamptz 使用明确时区。
    if db.get_bind().dialect.name == "postgresql":
        return value.replace(tzinfo=UTC8_TIMEZONE)
    return value


def _description(*, grant_id, delta_points, reason, request_id) -> str:
    return (
        f"超级管理员人工调整积分批次 #{grant_id}："
        f"变化 {delta_points:+.2f}；原因：{reason}；请求号：{request_id}"
    )


def _result(*, grant, account, entry, action_log, created, delta_points):
    return PointsAdjustmentResult(
        grant_id=grant.id,
        account_id=account.id,
        ledger_entry_id=entry.id,
        admin_action_log_id=action_log.id,
        created=created,
        delta_points=delta_points,
        grant_available_points=normalize_points(grant.available_points),
        grant_reserved_points=normalize_points(grant.reserved_points),
        account_available_points=normalize_points(account.available_points),
        account_reserved_points=normalize_points(account.reserved_points),
        grant_status=grant.status,
    )


def _matching_action_log(db, *, entry, actor_id, description):
    from ..models import AdminActionLog

    if (
        entry.reference_type != ADJUSTMENT_REFERENCE_TYPE
        or not isinstance(entry.reference_id, str)
        or not entry.reference_id.isdigit()
    ):
        return None
    action_log = db.query(AdminActionLog).filter(
        AdminActionLog.id == int(entry.reference_id),
    ).one_or_none()
    if action_log is None or (
        action_log.admin_id != actor_id
        or action_log.action_type != MallAuditActionType.POINTS_ADJUST.value
        or action_log.target_type != ADJUSTMENT_TARGET_TYPE
        or action_log.target_id != entry.grant_id
        or action_log.description != description
    ):
        return None
    return action_log


def adjust_points_grant(
    db,
    *,
    grant_id: int,
    delta_points,
    actor_admin_id: int,
    reason: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> PointsAdjustmentResult:
    """调整一个未过期积分批次的可用余额；调用方负责提交或回滚。

    正向调整只能恢复到该批次原始授予上限，负向调整不能扣成负数；
    不改变授予总额、到期时间、预占余额或来源业务。相同请求可安全重放。
    """
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import AdminActionLog, PointsLedgerEntry, User

    if (
        isinstance(grant_id, bool)
        or not isinstance(grant_id, int)
        or grant_id <= 0
    ):
        raise ValueError("积分批次编号无效")
    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(ADJUSTMENT_PERMISSION_MESSAGE)

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
        or not can_perform_mall_audit_action(
            actor,
            MallAuditActionType.POINTS_ADJUST,
        )
    ):
        raise PermissionError(ADJUSTMENT_PERMISSION_MESSAGE)

    delta = normalize_points(delta_points, "积分调整值")
    if delta == ZERO:
        raise ValueError("积分调整值不能为零")
    normalized_reason = _normalize_reason(reason)
    request_id, ledger_key = _normalize_request_id(idempotency_key)
    current_time = _to_utc8_wall_time(
        utc8_now() if now is None else now,
        field_name="积分调整时间",
    )

    grant, account = _lock_points_grant_and_account(db, grant_id=grant_id)
    assert_points_account_balance_consistent(db, account_id=account.id)
    description = _description(
        grant_id=grant.id,
        delta_points=delta,
        reason=normalized_reason,
        request_id=request_id,
    )

    existing = (
        db.query(PointsLedgerEntry)
        .filter(PointsLedgerEntry.idempotency_key == ledger_key)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        same_request = (
            existing.grant_id == grant.id
            and existing.entry_type == PointsLedgerEntryType.ADJUST.value
            and normalize_points(existing.available_points_delta) == delta
            and normalize_points(existing.reserved_points_delta) == ZERO
            and existing.actor_admin_id == actor.id
            and existing.reason == normalized_reason
        )
        action_log = _matching_action_log(
            db,
            entry=existing,
            actor_id=actor.id,
            description=description,
        )
        if not same_request:
            raise ValueError("积分调整请求号冲突")
        if action_log is None:
            raise ValueError("积分调整审计记录不完整")
        return _result(
            grant=grant,
            account=account,
            entry=existing,
            action_log=action_log,
            created=False,
            delta_points=delta,
        )

    expires_at = _to_utc8_wall_time(
        grant.expires_at,
        field_name="积分到期时间",
    )
    if current_time >= expires_at:
        raise ValueError("已到期积分批次不能人工调整")
    if grant.status == PointsGrantStatus.FROZEN.value:
        raise ValueError("冻结积分批次不能人工调整")
    if grant.status not in {
        PointsGrantStatus.ACTIVE.value,
        PointsGrantStatus.EXHAUSTED.value,
    }:
        raise ValueError("当前积分批次状态不能人工调整")

    available = normalize_points(grant.available_points)
    reserved = normalize_points(grant.reserved_points)
    granted = normalize_points(grant.granted_points)
    new_available = normalize_points(available + delta)
    if new_available < ZERO:
        raise ValueError("负向调整不能超过批次可用积分")
    if new_available + reserved > granted:
        raise ValueError("正向调整不能超过批次原始授予积分上限")
    new_account_available = normalize_points(
        normalize_points(account.available_points) + delta,
    )
    if new_account_available < ZERO:
        raise ValueError(POINTS_BALANCE_MISMATCH_MESSAGE)

    stored_time = _database_time(db, current_time)
    action_log = AdminActionLog(
        admin_id=actor.id,
        action_type=MallAuditActionType.POINTS_ADJUST.value,
        target_type=ADJUSTMENT_TARGET_TYPE,
        target_id=grant.id,
        description=description,
        created_at=stored_time,
    )
    db.add(action_log)
    db.flush()
    entry = PointsLedgerEntry(
        grant_id=grant.id,
        entry_type=PointsLedgerEntryType.ADJUST.value,
        available_points_delta=delta,
        reserved_points_delta=ZERO,
        idempotency_key=ledger_key,
        reference_type=ADJUSTMENT_REFERENCE_TYPE,
        reference_id=str(action_log.id),
        actor_admin_id=actor.id,
        reason=normalized_reason,
        created_at=stored_time,
    )
    db.add(entry)
    grant.available_points = new_available
    grant.status = (
        PointsGrantStatus.EXHAUSTED.value
        if new_available == ZERO and reserved == ZERO
        else PointsGrantStatus.ACTIVE.value
    )
    grant.updated_at = stored_time
    account.available_points = new_account_available
    account.version = (account.version or 0) + 1
    account.updated_at = stored_time
    db.flush()
    assert_points_account_balance_consistent(db, account_id=account.id)
    return _result(
        grant=grant,
        account=account,
        entry=entry,
        action_log=action_log,
        created=True,
        delta_points=delta,
    )


def execute_points_adjustment(engine, **adjustment) -> PointsAdjustmentResult:
    """在独立写事务中执行并提交一次调整；异常时整体回滚。"""
    from sqlalchemy.orm import Session

    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        try:
            with Session(bind=connection, autoflush=False) as db:
                result = adjust_points_grant(db, **adjustment)
                connection.commit()
                return result
        except Exception:
            connection.rollback()
            raise
