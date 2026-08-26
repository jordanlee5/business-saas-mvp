from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .business_status_service import (
    BUSINESS_STATUS_MATCHED_UNSETTLED,
    BUSINESS_STATUS_SETTLED,
    BUSINESS_STATUS_UNMATCHED,
)


MONEY_QUANTUM = Decimal("0.01")
DEFAULT_TREND_POINT_LIMIT = 14
INVALID_AMOUNT_TEXT = "—"


@dataclass(frozen=True)
class BusinessTrendPoint:
    date_label: str
    full_date_label: str
    record_count: int
    points_amount: float
    height_percent: int


@dataclass(frozen=True)
class BusinessTrend:
    points: tuple[BusinessTrendPoint, ...]
    total_date_count: int
    hidden_date_count: int


@dataclass(frozen=True)
class BusinessProgressStage:
    label: str
    value: int
    percentage: float | None
    width_percent: float
    tone: str


def _record_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    return None


def _money(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")

    if isinstance(value, bool):
        return Decimal("0.00")

    try:
        amount = Decimal(str(value).strip())
    except (
        InvalidOperation,
        AttributeError,
        ValueError,
    ):
        return Decimal("0.00")

    if not amount.is_finite():
        return Decimal("0.00")

    return amount.quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def format_amount(value) -> str:
    """将金额统一显示为千分位和两位小数。"""
    if value is None or isinstance(value, bool):
        return INVALID_AMOUNT_TEXT

    try:
        amount = Decimal(str(value).strip())
    except (
        InvalidOperation,
        AttributeError,
        ValueError,
    ):
        return INVALID_AMOUNT_TEXT

    if not amount.is_finite():
        return INVALID_AMOUNT_TEXT

    rounded_amount = amount.quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return f"{rounded_amount:,.2f}"


def build_business_trend(
    records,
    *,
    point_limit: int = DEFAULT_TREND_POINT_LIMIT,
) -> BusinessTrend:
    """
    按业务导入日期汇总业务条数与积分金额。

    页面仅展示最近若干个有业务的日期，避免历史跨度过大时
    图表被压缩；总日期数和隐藏日期数单独返回供页面说明。
    """
    if point_limit <= 0:
        raise ValueError(
            "趋势点数量必须大于 0"
        )

    grouped = {}

    for record in records or ():
        created_date = _record_date(
            getattr(
                record,
                "created_at",
                None,
            )
        )

        if created_date is None:
            continue

        date_group = grouped.setdefault(
            created_date,
            {
                "record_count": 0,
                "points_amount": Decimal("0.00"),
            },
        )
        date_group["record_count"] += 1
        date_group["points_amount"] += _money(
            getattr(
                record,
                "points_amount",
                None,
            )
        )

    sorted_dates = sorted(grouped)
    total_date_count = len(sorted_dates)
    visible_dates = sorted_dates[-point_limit:]
    hidden_date_count = (
        total_date_count
        - len(visible_dates)
    )
    max_record_count = max(
        (
            grouped[item]["record_count"]
            for item in visible_dates
        ),
        default=0,
    )

    points = []

    for item in visible_dates:
        record_count = grouped[item][
            "record_count"
        ]
        height_percent = (
            max(
                12,
                round(
                    record_count
                    * 100
                    / max_record_count
                ),
            )
            if max_record_count
            else 0
        )

        points.append(
            BusinessTrendPoint(
                date_label=item.strftime(
                    "%m-%d"
                ),
                full_date_label=item.strftime(
                    "%Y-%m-%d"
                ),
                record_count=record_count,
                points_amount=float(
                    grouped[item][
                        "points_amount"
                    ].quantize(
                        MONEY_QUANTUM,
                        rounding=ROUND_HALF_UP,
                    )
                ),
                height_percent=height_percent,
            )
        )

    return BusinessTrend(
        points=tuple(points),
        total_date_count=total_date_count,
        hidden_date_count=hidden_date_count,
    )


def _progress_stage(
    *,
    label: str,
    value: int,
    total_business_records: int,
    tone: str,
) -> BusinessProgressStage:
    normalized_value = max(
        int(value or 0),
        0,
    )
    normalized_total = max(
        int(total_business_records or 0),
        0,
    )

    if normalized_total <= 0:
        percentage = None
        width_percent = 0.0
    else:
        percentage = round(
            normalized_value
            * 100
            / normalized_total,
            2,
        )
        width_percent = min(
            percentage,
            100.0,
        )

    return BusinessProgressStage(
        label=label,
        value=normalized_value,
        percentage=percentage,
        width_percent=width_percent,
        tone=tone,
    )


def build_business_status_distribution(
    *,
    total_business_records: int,
    matched_business_count: int,
    settled_business_count: int,
) -> tuple[BusinessProgressStage, ...]:
    """
    将业务拆分为互斥状态，三项合计始终等于业务总量。
    """
    normalized_total = max(
        int(total_business_records or 0),
        0,
    )
    normalized_matched = min(
        max(int(matched_business_count or 0), 0),
        normalized_total,
    )
    normalized_settled = min(
        max(int(settled_business_count or 0), 0),
        normalized_matched,
    )
    unmatched_business_count = (
        normalized_total
        - normalized_matched
    )
    matched_unsettled_business_count = (
        normalized_matched
        - normalized_settled
    )

    return (
        _progress_stage(
            label=BUSINESS_STATUS_UNMATCHED,
            value=unmatched_business_count,
            total_business_records=normalized_total,
            tone="unmatched",
        ),
        _progress_stage(
            label=BUSINESS_STATUS_MATCHED_UNSETTLED,
            value=(
                matched_unsettled_business_count
            ),
            total_business_records=normalized_total,
            tone="processing",
        ),
        _progress_stage(
            label=BUSINESS_STATUS_SETTLED,
            value=normalized_settled,
            total_business_records=normalized_total,
            tone="settled",
        ),
    )
