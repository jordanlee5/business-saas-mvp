from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


EXTERNAL_MODE = "external"
INTERNAL_MODE = "internal"

VALID_RATE_MODES = {
    EXTERNAL_MODE,
    INTERNAL_MODE,
}

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class RateCalculationResult:
    """
    单项费率计算结果。

    base_amount:
        上传方服务清单原始金额。

    rate_percent:
        配置的百分比费率，例如 5 表示 5%。

    mode:
        external = 外扣
        internal = 内扣

    settlement_total:
        根据计算方式得出的最终结算总额。

    fee_amount:
        最终结算总额减去原始清单金额后的费用。
    """

    base_amount: Decimal
    rate_percent: Decimal
    mode: str
    settlement_total: Decimal
    fee_amount: Decimal


@dataclass(frozen=True)
class BusinessSettlementResult:
    """
    单条业务的完整上下游核算结果。

    downstream:
        下游服务费计算结果。

    upstream:
        上游成本费计算结果。

    gross_profit:
        已按分规范的下游服务费，
        减去已按分规范的上游成本费。
    """

    base_amount: Decimal
    downstream: RateCalculationResult
    upstream: RateCalculationResult
    gross_profit: Decimal


def _to_decimal(
    value,
    field_name: str,
) -> Decimal:
    """
    将输入安全转换为 Decimal。

    使用 Decimal(str(value))，避免直接把二进制 float
    转换成 Decimal 时带入不必要的小数误差。
    """
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

    return decimal_value


def _round_money(value: Decimal) -> Decimal:
    """
    将金额按人民币分保留两位小数。

    采用 ROUND_HALF_UP：
    第三位小数为 5 时向上舍入。
    """
    return value.quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _validate_rate(
    rate_percent: Decimal,
    mode: str,
) -> None:
    if rate_percent < ZERO:
        raise ValueError(
            "费率不能小于 0%"
        )

    if mode == EXTERNAL_MODE:
        if rate_percent > HUNDRED:
            raise ValueError(
                "外扣费率不能大于 100%"
            )

        return

    if mode == INTERNAL_MODE:
        if rate_percent >= HUNDRED:
            raise ValueError(
                "内扣费率必须小于 100%"
            )

        return

    raise ValueError(
        "计算方式必须是 external 或 internal"
    )


def calculate_rate_settlement(
    base_amount,
    rate_percent,
    mode: str,
) -> RateCalculationResult:
    """
    根据外扣或内扣方式计算一项结算金额。

    外扣：
        结算总额 = 清单金额 × (1 + 费率)

    内扣：
        结算总额 = 清单金额 ÷ (1 - 费率)

    参数中的 rate_percent 使用百分数形式：
        5 表示 5%
        0.5 表示 0.5%
    """
    if mode not in VALID_RATE_MODES:
        raise ValueError(
            "计算方式必须是 external 或 internal"
        )

    amount_decimal = _to_decimal(
        base_amount,
        "清单金额",
    )

    rate_decimal = _to_decimal(
        rate_percent,
        "费率",
    )

    if amount_decimal < ZERO:
        raise ValueError(
            "清单金额不能小于 0"
        )

    _validate_rate(
        rate_decimal,
        mode,
    )

    # 清单金额本身先按人民币分进行规范。
    rounded_base_amount = _round_money(
        amount_decimal
    )

    rate_fraction = (
        rate_decimal / HUNDRED
    )

    if mode == EXTERNAL_MODE:
        raw_settlement_total = (
            rounded_base_amount
            * (ONE + rate_fraction)
        )
    else:
        raw_settlement_total = (
            rounded_base_amount
            / (ONE - rate_fraction)
        )

    settlement_total = _round_money(
        raw_settlement_total
    )

    # 使用已舍入的结算总额减去已规范的本金，
    # 保证：
    # 清单金额 + 费用金额 = 结算总额
    fee_amount = _round_money(
        settlement_total
        - rounded_base_amount
    )

    return RateCalculationResult(
        base_amount=rounded_base_amount,
        rate_percent=rate_decimal,
        mode=mode,
        settlement_total=settlement_total,
        fee_amount=fee_amount,
    )

def calculate_business_settlement(
    base_amount,
    downstream_rate_percent,
    downstream_mode: str,
    upstream_rate_percent,
    upstream_mode: str,
) -> BusinessSettlementResult:
    """
    计算单条业务的上下游金额和毛利。

    核算口径：
    1. 下游服务费独立计算并保留到分；
    2. 上游成本费独立计算并保留到分；
    3. 毛利使用两个已规范到分的费用相减。

    这样保证：
        下游服务费 - 上游成本费 = 毛利
    """
    downstream_result = calculate_rate_settlement(
        base_amount,
        downstream_rate_percent,
        downstream_mode,
    )

    upstream_result = calculate_rate_settlement(
        base_amount,
        upstream_rate_percent,
        upstream_mode,
    )

    gross_profit = _round_money(
        downstream_result.fee_amount
        - upstream_result.fee_amount
    )

    return BusinessSettlementResult(
        base_amount=downstream_result.base_amount,
        downstream=downstream_result,
        upstream=upstream_result,
        gross_profit=gross_profit,
    )