"""已激活积分的到期查询与原子过期处理；不改变来源业务的领取状态。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_

from .domain import (
    PointsGrantStatus,
    PointsLedgerEntryType,
    _to_utc8_wall_time,
    normalize_points,
)
from .points_ledger_service import (
    _lock_points_grant_and_account,
    audit_points_account_balance,
)
from ..time_utils import UTC8_TIMEZONE, utc8_now


ZERO = Decimal("0.00")
EXPIRY_REASON = "积分批次到期"


@dataclass(frozen=True)
class PointsExpiryItem:
    grant_id: int
    account_id: int
    expires_at: datetime
    available_points: Decimal
    reserved_points: Decimal
    status: str
    block_reason: str | None


@dataclass(frozen=True)
class PointsExpiryPage:
    as_of: datetime
    items: tuple[PointsExpiryItem, ...]
    has_more: bool
    next_after_grant_id: int | None


@dataclass(frozen=True)
class PointsExpiryResult:
    grant_id: int
    changed: bool
    expired_points: Decimal
    ledger_entry_id: int | None


def _current_time(now):
    return _to_utc8_wall_time(
        utc8_now() if now is None else now,
        field_name="到期检查时间",
    )


def _integer(value, name, minimum, maximum=None):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{name}超出允许范围")


def _database_time(db, value):
    # SQLite 存 UTC+8 墙上时间；PostgreSQL 的 timestamptz 使用明确时区。
    if db.get_bind().dialect.name == "postgresql":
        return value.replace(tzinfo=UTC8_TIMEZONE)
    return value


def _block_reason(grant, audit):
    if not audit.is_consistent:
        return "积分余额缓存与流水不一致"
    grant_audit = next(x for x in audit.grant_audits if x.grant_id == grant.id)
    if grant_audit.ledger_balance.entry_count == 0:
        return "积分批次尚未完成首笔入账"
    if grant.status == PointsGrantStatus.FROZEN.value:
        return "积分批次已冻结，需先处理冻结原因"
    if normalize_points(grant.reserved_points) != ZERO:
        return "积分批次存在预占余额，需先处理关联业务"
    if grant.status == PointsGrantStatus.EXHAUSTED.value:
        if normalize_points(grant.available_points) != ZERO:
            return "已用尽积分批次仍有可用余额"
    elif grant.status != PointsGrantStatus.ACTIVE.value:
        return "积分批次状态不支持自动到期处理"
    return None


def _query_expiry_page(
    db, *, now, days, account_id, limit, after_grant_id,
):
    from ..models import PointsAccount, PointsGrant

    _integer(limit, "每批数量", 1, 1000)
    _integer(after_grant_id, "积分批次游标", 0)
    if account_id is not None:
        _integer(account_id, "积分账户编号", 1)
    current_time = _current_time(now)
    if days is not None:
        _integer(days, "即将到期天数", 1, 366)
    with db.no_autoflush:
        if account_id is not None and db.query(PointsAccount.id).filter(
            PointsAccount.id == account_id
        ).one_or_none() is None:
            raise ValueError("积分账户不存在")
        # 读取列而非实体，查询不会刷新或覆盖调用方尚未保存的 ORM 对象。
        query = db.query(
            PointsGrant.id,
            PointsGrant.account_id,
            PointsGrant.expires_at,
            PointsGrant.available_points,
            PointsGrant.reserved_points,
            PointsGrant.status,
        ).filter(
            PointsGrant.id > after_grant_id,
            or_(
                PointsGrant.status != PointsGrantStatus.EXPIRED.value,
                PointsGrant.available_points > 0,
                PointsGrant.reserved_points > 0,
            ),
        )
        if account_id is not None:
            query = query.filter(PointsGrant.account_id == account_id)
        if days is None:
            query = query.filter(
                PointsGrant.expires_at <= _database_time(db, current_time),
            )
        else:
            query = query.filter(
                PointsGrant.expires_at > _database_time(db, current_time),
                PointsGrant.expires_at <= _database_time(
                    db, current_time + timedelta(days=days),
                ),
                or_(PointsGrant.available_points > 0, PointsGrant.reserved_points > 0),
            )
        rows = query.order_by(PointsGrant.id).limit(limit + 1).all()
        audits = {}
        items = []
        for grant in rows[:limit]:
            if grant.account_id not in audits:
                audits[grant.account_id] = audit_points_account_balance(
                    db, account_id=grant.account_id,
                )
            items.append(PointsExpiryItem(
                grant_id=grant.id,
                account_id=grant.account_id,
                expires_at=_to_utc8_wall_time(grant.expires_at, "积分到期时间"),
                available_points=normalize_points(grant.available_points),
                reserved_points=normalize_points(grant.reserved_points),
                status=grant.status,
                block_reason=_block_reason(grant, audits[grant.account_id]),
            ))
    return PointsExpiryPage(
        as_of=current_time,
        items=tuple(items),
        has_more=len(rows) > limit,
        next_after_grant_id=items[-1].grant_id if items else None,
    )


def list_due_points_grants(
    db, *, now=None, account_id=None, limit=100, after_grant_id=0,
) -> PointsExpiryPage:
    """只读列出已到期且尚未处理的批次，包含阻止原因，按 ID 游标分页。"""
    return _query_expiry_page(
        db, now=now, days=None, account_id=account_id,
        limit=limit, after_grant_id=after_grant_id,
    )


def list_expiring_points_grants(
    db, *, days=30, now=None, account_id=None, limit=100, after_grant_id=0,
) -> PointsExpiryPage:
    """只读查询 (当前时刻, 当前时刻 + days] 内有余额的批次。"""
    return _query_expiry_page(
        db, now=now, days=days, account_id=account_id,
        limit=limit, after_grant_id=after_grant_id,
    )


def expire_points_grant(db, *, grant_id: int, now=None) -> PointsExpiryResult:
    """追加 EXPIRE 并同步缓存；不提交，异常时调用方必须整体回滚。

    SQLite 任务入口使用 BEGIN IMMEDIATE；其他调用方也必须先取得写事务。
    PostgreSQL 依次锁账户、积分批次，与首笔 GRANT 写入口保持顺序一致。
    """
    from ..models import PointsLedgerEntry

    _integer(grant_id, "积分批次编号", 1)
    current_time = _current_time(now)
    grant, account = _lock_points_grant_and_account(db, grant_id=grant_id)
    audit = audit_points_account_balance(db, account_id=account.id)
    if not audit.is_consistent:
        raise ValueError("积分余额缓存与流水不一致")
    if _to_utc8_wall_time(grant.expires_at, "积分到期时间") > current_time:
        raise ValueError("积分批次尚未到期")

    key = f"points-expiry:{grant.id}"
    entries = db.query(PointsLedgerEntry).filter(or_(
        PointsLedgerEntry.idempotency_key == key,
        (PointsLedgerEntry.grant_id == grant.id)
        & (PointsLedgerEntry.entry_type == PointsLedgerEntryType.EXPIRE.value),
    )).all()
    entry = entries[0] if len(entries) == 1 else None
    valid_entry = entry is not None and (
        entry.grant_id == grant.id
        and entry.idempotency_key == key
        and entry.entry_type == PointsLedgerEntryType.EXPIRE.value
        and normalize_points(entry.available_points_delta) < ZERO
        and normalize_points(entry.reserved_points_delta) == ZERO
        and entry.reference_type == "POINTS_GRANT"
        and entry.reference_id == str(grant.id)
        and entry.actor_admin_id is None
        and entry.reason == EXPIRY_REASON
    )
    if entries and not valid_entry:
        raise ValueError("到期流水幂等键或内容冲突")
    if grant.status == PointsGrantStatus.EXPIRED.value:
        if grant.available_points != ZERO or grant.reserved_points != ZERO:
            raise ValueError("已过期积分批次仍有余额")
        grant_audit = next(x for x in audit.grant_audits if x.grant_id == grant.id)
        if grant_audit.ledger_balance.entry_count == 0:
            raise ValueError("积分批次尚未完成首笔入账")
        return PointsExpiryResult(grant.id, False, ZERO, entry.id if entry else None)
    if entries:
        raise ValueError("到期流水与积分批次状态不一致")
    reason = _block_reason(grant, audit)
    if reason:
        raise ValueError(reason)

    points = normalize_points(grant.available_points)
    stored_time = _database_time(db, current_time)
    if points > ZERO:
        entry = PointsLedgerEntry(
            grant_id=grant.id,
            entry_type=PointsLedgerEntryType.EXPIRE.value,
            available_points_delta=-points,
            reserved_points_delta=ZERO,
            idempotency_key=key,
            reference_type="POINTS_GRANT",
            reference_id=str(grant.id),
            reason=EXPIRY_REASON,
            created_at=stored_time,
        )
        db.add(entry)
    grant.available_points = ZERO
    grant.status = PointsGrantStatus.EXPIRED.value
    grant.updated_at = stored_time
    account.available_points = normalize_points(account.available_points) - points
    account.version += 1
    account.updated_at = stored_time
    db.flush()
    if not audit_points_account_balance(db, account_id=account.id).is_consistent:
        raise ValueError("积分余额缓存与流水不一致")
    return PointsExpiryResult(grant.id, True, points, entry.id if entry else None)
