"""积分流水只追加写入与余额重算服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import (
    PointsGrantStatus,
    PointsLedgerEntryType,
    normalize_points,
)
from ..time_utils import utc8_now


POINTS_BALANCE_MISMATCH_MESSAGE = "积分余额缓存与流水不一致"


@dataclass(frozen=True)
class PointsLedgerBalance:
    """由不可变流水计算得到的积分余额。"""

    available_points: Decimal
    reserved_points: Decimal
    entry_count: int


@dataclass(frozen=True)
class PointsGrantBalanceAudit:
    """一个积分批次的缓存余额与流水余额对照。"""

    grant_id: int
    cached_available_points: Decimal
    cached_reserved_points: Decimal
    ledger_balance: PointsLedgerBalance

    @property
    def is_consistent(self) -> bool:
        return (
            self.cached_available_points
            == self.ledger_balance.available_points
            and self.cached_reserved_points
            == self.ledger_balance.reserved_points
        )


@dataclass(frozen=True)
class PointsAccountBalanceAudit:
    """会员账户及其全部积分批次的余额审计结果。"""

    account_id: int
    cached_available_points: Decimal
    cached_reserved_points: Decimal
    ledger_balance: PointsLedgerBalance
    grant_audits: tuple[PointsGrantBalanceAudit, ...]

    @property
    def inconsistent_grant_ids(self) -> tuple[int, ...]:
        return tuple(
            audit.grant_id
            for audit in self.grant_audits
            if not audit.is_consistent
        )

    @property
    def is_consistent(self) -> bool:
        return (
            self.cached_available_points
            == self.ledger_balance.available_points
            and self.cached_reserved_points
            == self.ledger_balance.reserved_points
            and not self.inconsistent_grant_ids
        )


@dataclass(frozen=True)
class InitialGrantLedgerResult:
    """首笔 GRANT 流水写入结果。"""

    ledger_entry_id: int
    created: bool
    grant_balance: PointsLedgerBalance
    account_balance: PointsLedgerBalance


def _zero_balance() -> PointsLedgerBalance:
    return PointsLedgerBalance(
        available_points=Decimal("0.00"),
        reserved_points=Decimal("0.00"),
        entry_count=0,
    )


def _sum_ledger_entries(entries) -> PointsLedgerBalance:
    available_points = Decimal("0.00")
    reserved_points = Decimal("0.00")
    entry_count = 0

    for entry in entries:
        available_points += normalize_points(
            entry.available_points_delta,
            "可用积分流水变化",
        )
        reserved_points += normalize_points(
            entry.reserved_points_delta,
            "预占积分流水变化",
        )
        entry_count += 1

    return PointsLedgerBalance(
        available_points=normalize_points(available_points),
        reserved_points=normalize_points(reserved_points),
        entry_count=entry_count,
    )


def calculate_points_grant_balance(
    db,
    *,
    grant_id: int,
) -> PointsLedgerBalance:
    """只读地从全部流水重算一个积分批次的余额。"""
    from ..models import PointsGrant, PointsLedgerEntry

    grant = db.query(PointsGrant.id).filter(
        PointsGrant.id == grant_id
    ).one_or_none()
    if grant is None:
        raise ValueError("积分批次不存在")

    entries = (
        db.query(PointsLedgerEntry)
        .filter(PointsLedgerEntry.grant_id == grant_id)
        .order_by(PointsLedgerEntry.id.asc())
        .all()
    )
    return _sum_ledger_entries(entries)


def calculate_points_account_balance(
    db,
    *,
    account_id: int,
) -> PointsLedgerBalance:
    """只读地从全部批次流水重算一个会员账户的余额。"""
    from ..models import (
        PointsAccount,
        PointsGrant,
        PointsLedgerEntry,
    )

    account = db.query(PointsAccount.id).filter(
        PointsAccount.id == account_id
    ).one_or_none()
    if account is None:
        raise ValueError("积分账户不存在")

    entries = (
        db.query(PointsLedgerEntry)
        .join(
            PointsGrant,
            PointsLedgerEntry.grant_id == PointsGrant.id,
        )
        .filter(PointsGrant.account_id == account_id)
        .order_by(PointsLedgerEntry.id.asc())
        .all()
    )
    return _sum_ledger_entries(entries)


def audit_points_account_balance(
    db,
    *,
    account_id: int,
) -> PointsAccountBalanceAudit:
    """对照账户、批次缓存与不可变流水，但不修复任何数据。"""
    from ..models import (
        PointsAccount,
        PointsGrant,
        PointsLedgerEntry,
    )

    account = db.query(PointsAccount).filter(
        PointsAccount.id == account_id
    ).one_or_none()
    if account is None:
        raise ValueError("积分账户不存在")

    grants = (
        db.query(PointsGrant)
        .filter(PointsGrant.account_id == account_id)
        .order_by(PointsGrant.id.asc())
        .all()
    )
    entries = (
        db.query(PointsLedgerEntry)
        .join(
            PointsGrant,
            PointsLedgerEntry.grant_id == PointsGrant.id,
        )
        .filter(PointsGrant.account_id == account_id)
        .order_by(PointsLedgerEntry.id.asc())
        .all()
    )

    entries_by_grant: dict[int, list] = {
        grant.id: []
        for grant in grants
    }
    for entry in entries:
        entries_by_grant.setdefault(entry.grant_id, []).append(entry)

    grant_audits = tuple(
        PointsGrantBalanceAudit(
            grant_id=grant.id,
            cached_available_points=normalize_points(
                grant.available_points,
            ),
            cached_reserved_points=normalize_points(
                grant.reserved_points,
            ),
            ledger_balance=_sum_ledger_entries(
                entries_by_grant.get(grant.id, ()),
            ),
        )
        for grant in grants
    )

    return PointsAccountBalanceAudit(
        account_id=account.id,
        cached_available_points=normalize_points(
            account.available_points,
        ),
        cached_reserved_points=normalize_points(
            account.reserved_points,
        ),
        ledger_balance=_sum_ledger_entries(entries),
        grant_audits=grant_audits,
    )


def assert_points_account_balance_consistent(
    db,
    *,
    account_id: int,
) -> PointsAccountBalanceAudit:
    """余额不一致时失败关闭，且绝不静默覆盖缓存。"""
    audit = audit_points_account_balance(
        db,
        account_id=account_id,
    )
    if not audit.is_consistent:
        raise ValueError(POINTS_BALANCE_MISMATCH_MESSAGE)
    return audit


def _normalize_required_text(
    value,
    *,
    field_name: str,
    maximum_length: int,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}不能为空")
    normalized = value.strip()
    if len(normalized) > maximum_length:
        raise ValueError(
            f"{field_name}不能超过 {maximum_length} 个字符"
        )
    return normalized


def record_initial_points_grant(
    db,
    *,
    grant,
    idempotency_key: str,
    reference_type: str,
    reference_id: str,
    now: datetime | None = None,
) -> InitialGrantLedgerResult:
    """
    为新积分批次追加唯一的首笔 GRANT 流水并同步余额缓存。

    本函数只支持 M3 的首次入账，不提前开放预占、消费或退款。
    重复提交完全相同的幂等请求不会二次增加积分；同一幂等键
    携带不同内容时失败关闭。调用方负责提交或回滚整个事务。
    """
    from ..models import (
        PointsAccount,
        PointsGrant,
        PointsLedgerEntry,
    )

    if grant is None or grant.id is None:
        raise ValueError("积分批次不存在")

    normalized_key = _normalize_required_text(
        idempotency_key,
        field_name="积分流水幂等键",
        maximum_length=100,
    )
    normalized_reference_type = _normalize_required_text(
        reference_type,
        field_name="积分流水来源类型",
        maximum_length=50,
    )
    normalized_reference_id = _normalize_required_text(
        reference_id,
        field_name="积分流水来源编号",
        maximum_length=64,
    )
    current_time = now or utc8_now()

    locked_grant = (
        db.query(PointsGrant)
        .filter(PointsGrant.id == grant.id)
        .with_for_update()
        .one_or_none()
    )
    if locked_grant is None:
        raise ValueError("积分批次不存在")
    account = (
        db.query(PointsAccount)
        .filter(PointsAccount.id == locked_grant.account_id)
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        raise ValueError("积分账户不存在")

    points = normalize_points(
        locked_grant.granted_points,
        "批次授予积分",
    )
    if points <= 0:
        raise ValueError("批次授予积分必须大于零")

    existing = (
        db.query(PointsLedgerEntry)
        .filter(
            PointsLedgerEntry.idempotency_key == normalized_key
        )
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        same_request = (
            existing.grant_id == locked_grant.id
            and existing.entry_type
            == PointsLedgerEntryType.GRANT.value
            and normalize_points(existing.available_points_delta)
            == points
            and normalize_points(existing.reserved_points_delta)
            == Decimal("0.00")
            and existing.reference_type
            == normalized_reference_type
            and existing.reference_id == normalized_reference_id
            and existing.actor_admin_id is None
            and existing.reason is None
        )
        if not same_request:
            raise ValueError("积分流水幂等键冲突")

        audit = assert_points_account_balance_consistent(
            db,
            account_id=account.id,
        )
        grant_audit = next(
            item
            for item in audit.grant_audits
            if item.grant_id == locked_grant.id
        )
        return InitialGrantLedgerResult(
            ledger_entry_id=existing.id,
            created=False,
            grant_balance=grant_audit.ledger_balance,
            account_balance=audit.ledger_balance,
        )

    before_audit = assert_points_account_balance_consistent(
        db,
        account_id=account.id,
    )
    grant_audit = next(
        item
        for item in before_audit.grant_audits
        if item.grant_id == locked_grant.id
    )
    if grant_audit.ledger_balance != _zero_balance():
        raise ValueError("积分批次已经存在流水")
    if (
        normalize_points(locked_grant.available_points)
        != Decimal("0.00")
        or normalize_points(locked_grant.reserved_points)
        != Decimal("0.00")
    ):
        raise ValueError(POINTS_BALANCE_MISMATCH_MESSAGE)
    if locked_grant.status != PointsGrantStatus.ACTIVE.value:
        raise ValueError("只有有效积分批次可以首次入账")

    entry = PointsLedgerEntry(
        grant_id=locked_grant.id,
        entry_type=PointsLedgerEntryType.GRANT.value,
        available_points_delta=points,
        reserved_points_delta=Decimal("0.00"),
        idempotency_key=normalized_key,
        reference_type=normalized_reference_type,
        reference_id=normalized_reference_id,
        created_at=current_time,
    )
    db.add(entry)
    locked_grant.available_points = points
    locked_grant.reserved_points = Decimal("0.00")
    locked_grant.updated_at = current_time
    account.available_points = (
        normalize_points(account.available_points) + points
    )
    account.reserved_points = normalize_points(
        account.reserved_points,
    )
    account.version = (account.version or 0) + 1
    account.updated_at = current_time
    db.flush()

    after_audit = assert_points_account_balance_consistent(
        db,
        account_id=account.id,
    )
    after_grant_audit = next(
        item
        for item in after_audit.grant_audits
        if item.grant_id == locked_grant.id
    )
    return InitialGrantLedgerResult(
        ledger_entry_id=entry.id,
        created=True,
        grant_balance=after_grant_audit.ledger_balance,
        account_balance=after_audit.ledger_balance,
    )
