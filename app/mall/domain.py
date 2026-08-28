from calendar import monthrange
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum

from ..time_utils import UTC8_TIMEZONE


POINTS_QUANTUM = Decimal("0.01")


class BusinessChannel(str, Enum):
    """一条业务可以选择的积分使用渠道。"""

    CASH_REBATE = "CASH_REBATE"
    MALL_REDEMPTION = "MALL_REDEMPTION"


VALID_BUSINESS_CHANNELS = frozenset(
    channel.value
    for channel in BusinessChannel
)


def normalize_business_channel(
    value: BusinessChannel | str,
) -> BusinessChannel:
    """
    将渠道值规范为 ``BusinessChannel``。

    渠道名称必须使用已确认的大写枚举值，避免把未知值
    静默归入现金返现或商城兑换。
    """
    if isinstance(value, BusinessChannel):
        return value

    if not isinstance(value, str):
        raise ValueError(
            "业务渠道必须是 CASH_REBATE 或 MALL_REDEMPTION"
        )

    normalized_value = value.strip()

    try:
        return BusinessChannel(normalized_value)
    except ValueError as exc:
        raise ValueError(
            "业务渠道必须是 CASH_REBATE 或 MALL_REDEMPTION"
        ) from exc


def normalize_points(
    value,
    field_name: str = "积分",
) -> Decimal:
    """
    将积分安全规范为保留两位小数的 ``Decimal``。

    此函数只负责精度规范，不限制正负号；后续入账、消费、
    退款和人工调整应各自在领域服务中校验允许的方向。
    """
    if isinstance(value, bool):
        raise ValueError(
            f"{field_name}不能是布尔值"
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
                f"{field_name}必须是有效数字"
            ) from exc

    if not decimal_value.is_finite():
        raise ValueError(
            f"{field_name}必须是有限数字"
        )

    return decimal_value.quantize(
        POINTS_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _to_utc8_wall_time(
    value: datetime,
    field_name: str,
) -> datetime:
    """把时间转换为现有系统使用的无时区 UTC+8 墙上时间。"""
    if not isinstance(value, datetime):
        raise ValueError(
            f"{field_name}必须是有效时间"
        )

    if (
        value.tzinfo is not None
        and value.utcoffset() is not None
    ):
        return value.astimezone(
            UTC8_TIMEZONE
        ).replace(tzinfo=None)

    return value


def calculate_points_expiry(
    activated_at: datetime,
) -> datetime:
    """
    计算积分批次从激活时刻起一个自然年后的到期时刻。

    到期判定统一使用 UTC+8。若激活日期为 2 月 29 日，
    而次年没有相同日期，则取次年 2 月最后一天的相同时刻。
    """
    activated_utc8 = _to_utc8_wall_time(
        activated_at,
        "激活时间",
    )
    expiry_year = activated_utc8.year + 1
    expiry_day = min(
        activated_utc8.day,
        monthrange(
            expiry_year,
            activated_utc8.month,
        )[1],
    )

    return activated_utc8.replace(
        year=expiry_year,
        day=expiry_day,
    )


def is_activation_within_deadline(
    activation_at: datetime,
    claim_deadline: datetime,
) -> bool:
    """
    判断一次激活是否仍位于所属批次的领取截止时间内。

    恰好等于截止时刻仍可激活；只有超过截止时刻才拒绝。
    激活后积分的到期时间应继续由 ``calculate_points_expiry``
    独立计算，不能被领取截止日截短。
    """
    activation_utc8 = _to_utc8_wall_time(
        activation_at,
        "激活时间",
    )
    deadline_utc8 = _to_utc8_wall_time(
        claim_deadline,
        "激活截止时间",
    )

    return activation_utc8 <= deadline_utc8
