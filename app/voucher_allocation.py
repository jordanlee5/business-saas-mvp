from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


ZERO = Decimal("0.00")
MONEY_QUANTUM = Decimal("0.01")

BUSINESS_OVERPAID = "business_overpaid"
VOUCHER_OVERALLOCATED = "voucher_overallocated"
BUSINESS_COMPLETED = "business_completed"
VOUCHER_FULLY_ALLOCATED = "voucher_fully_allocated"


@dataclass(frozen=True)
class AllocationLimits:
    """一条匹配记录在核销前的业务与凭证金额上限。"""

    business_remaining: Decimal
    voucher_remaining: Decimal
    maximum_allocation: Decimal


@dataclass(frozen=True)
class AllocationBlockReason:
    """当前匹配记录不能进入审核或不能继续核销的原因。"""

    code: str
    message: str


@dataclass(frozen=True)
class VoucherAllocationResult:
    """一次通过金额校验后的核销结果。"""

    allocation_amount: Decimal
    business_remaining_before: Decimal
    business_remaining_after: Decimal
    voucher_remaining_before: Decimal
    voucher_remaining_after: Decimal


def _to_money(
    value,
    field_name: str,
) -> Decimal:
    """将金额安全转换为保留两位小数的 Decimal。"""
    if isinstance(value, bool):
        raise ValueError(
            f"{field_name} 不能是布尔值"
        )

    if isinstance(value, Decimal):
        decimal_value = value
    else:
        try:
            decimal_value = Decimal(
                str(value).strip()
            )
        except (
            InvalidOperation,
            AttributeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"{field_name} 必须是有效数字"
            ) from exc

    if not decimal_value.is_finite():
        raise ValueError(
            f"{field_name} 必须是有限数字"
        )

    return decimal_value.quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _require_non_negative(
    value: Decimal,
    field_name: str,
) -> None:
    if value < ZERO:
        raise ValueError(
            f"{field_name} 不能小于 0"
        )


def calculate_allocation_limits(
    business_amount,
    approved_business_amount,
    voucher_amount,
    approved_voucher_amount,
    current_allocation_amount=ZERO,
) -> AllocationLimits:
    """
    计算业务与凭证在本次核销前各自的剩余金额。

    approved_business_amount:
        该业务已经复核通过的核销金额总和。

    approved_voucher_amount:
        该凭证已经复核通过的核销金额总和。

    current_allocation_amount:
        重新审核一条已通过记录时，排除该记录原核销金额，
        避免当前记录重复占用自己的业务和凭证额度。
    """
    normalized_business_amount = _to_money(
        business_amount,
        "业务金额",
    )
    normalized_business_approved = _to_money(
        approved_business_amount,
        "业务已核销金额",
    )
    normalized_voucher_amount = _to_money(
        voucher_amount,
        "凭证金额",
    )
    normalized_voucher_approved = _to_money(
        approved_voucher_amount,
        "凭证已分配金额",
    )
    normalized_current_allocation = _to_money(
        current_allocation_amount,
        "当前记录原核销金额",
    )

    non_negative_values = (
        (
            normalized_business_amount,
            "业务金额",
        ),
        (
            normalized_business_approved,
            "业务已核销金额",
        ),
        (
            normalized_voucher_amount,
            "凭证金额",
        ),
        (
            normalized_voucher_approved,
            "凭证已分配金额",
        ),
        (
            normalized_current_allocation,
            "当前记录原核销金额",
        ),
    )

    for value, field_name in non_negative_values:
        _require_non_negative(
            value,
            field_name,
        )

    if (
        normalized_current_allocation
        > normalized_business_approved
    ):
        raise ValueError(
            "当前记录原核销金额不能大于业务已核销金额"
        )

    if (
        normalized_current_allocation
        > normalized_voucher_approved
    ):
        raise ValueError(
            "当前记录原核销金额不能大于凭证已分配金额"
        )

    effective_business_approved = (
        normalized_business_approved
        - normalized_current_allocation
    )
    effective_voucher_approved = (
        normalized_voucher_approved
        - normalized_current_allocation
    )

    business_remaining = (
        normalized_business_amount
        - effective_business_approved
    )
    voucher_remaining = (
        normalized_voucher_amount
        - effective_voucher_approved
    )

    maximum_allocation = min(
        business_remaining,
        voucher_remaining,
    )

    if maximum_allocation < ZERO:
        maximum_allocation = ZERO

    return AllocationLimits(
        business_remaining=business_remaining,
        voucher_remaining=voucher_remaining,
        maximum_allocation=maximum_allocation,
    )


def get_review_block_reason(
    limits: AllocationLimits,
) -> AllocationBlockReason | None:
    """返回不能进入初审或复核的金额原因。"""
    if limits.business_remaining < ZERO:
        return AllocationBlockReason(
            code=BUSINESS_OVERPAID,
            message=(
                "该业务已存在超额核销，"
                "必须先处理错误或重复匹配记录"
            ),
        )

    if limits.voucher_remaining < ZERO:
        return AllocationBlockReason(
            code=VOUCHER_OVERALLOCATED,
            message=(
                "该凭证已存在超额分配，"
                "必须先处理错误或重复匹配记录"
            ),
        )

    if limits.business_remaining == ZERO:
        return AllocationBlockReason(
            code=BUSINESS_COMPLETED,
            message=(
                "该业务已完成核销，"
                "不进入初审或复核"
            ),
        )

    if limits.voucher_remaining == ZERO:
        return AllocationBlockReason(
            code=VOUCHER_FULLY_ALLOCATED,
            message=(
                "该凭证已全部分配，"
                "不进入初审或复核"
            ),
        )

    return None


def validate_allocation_amount(
    allocation_amount,
    limits: AllocationLimits,
) -> VoucherAllocationResult:
    """校验本次核销金额并返回核销前后的剩余金额。"""
    block_reason = get_review_block_reason(
        limits
    )

    if block_reason is not None:
        raise ValueError(
            block_reason.message
        )

    normalized_allocation = _to_money(
        allocation_amount,
        "本次核销金额",
    )

    if normalized_allocation <= ZERO:
        raise ValueError(
            "本次核销金额必须大于 0"
        )

    if (
        normalized_allocation
        > limits.business_remaining
    ):
        raise ValueError(
            "本次核销金额不能超过业务剩余金额"
        )

    if (
        normalized_allocation
        > limits.voucher_remaining
    ):
        raise ValueError(
            "本次核销金额不能超过凭证剩余可分配金额"
        )

    business_remaining_after = (
        limits.business_remaining
        - normalized_allocation
    )
    voucher_remaining_after = (
        limits.voucher_remaining
        - normalized_allocation
    )

    return VoucherAllocationResult(
        allocation_amount=normalized_allocation,
        business_remaining_before=(
            limits.business_remaining
        ),
        business_remaining_after=(
            business_remaining_after
        ),
        voucher_remaining_before=(
            limits.voucher_remaining
        ),
        voucher_remaining_after=(
            voucher_remaining_after
        ),
    )