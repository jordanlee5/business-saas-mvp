from .voucher_allocation import (
    ALLOCATION_STATUS_COMPLETED,
)


BUSINESS_STATUS_ALL = "全部"
BUSINESS_STATUS_UNMATCHED = "无匹配记录"
BUSINESS_STATUS_MATCHED_UNSETTLED = (
    "有匹配记录未结清"
)
BUSINESS_STATUS_SETTLED = "已结清"

BUSINESS_STATUS_FILTER_OPTIONS = (
    BUSINESS_STATUS_ALL,
    BUSINESS_STATUS_UNMATCHED,
    BUSINESS_STATUS_MATCHED_UNSETTLED,
    BUSINESS_STATUS_SETTLED,
)

ALLOCATION_AMOUNT_STATUS_ALL = "全部"
ALLOCATION_AMOUNT_STATUS_ABNORMAL = "异常"

ALLOCATION_AMOUNT_STATUS_FILTER_OPTIONS = (
    ALLOCATION_AMOUNT_STATUS_ALL,
    ALLOCATION_AMOUNT_STATUS_ABNORMAL,
)


def normalize_business_status_filter(value) -> str:
    if value in BUSINESS_STATUS_FILTER_OPTIONS:
        return value

    return BUSINESS_STATUS_ALL


def normalize_allocation_amount_status_filter(
    value,
) -> str:
    if value in ALLOCATION_AMOUNT_STATUS_FILTER_OPTIONS:
        return value

    return ALLOCATION_AMOUNT_STATUS_ALL


def is_business_allocation_amount_abnormal(
    *,
    acceptance_status: str,
    abnormal_message,
) -> bool:
    """只有已承接业务才进入核销金额异常提醒。"""
    return (
        acceptance_status == "已承接"
        and bool(str(abnormal_message or "").strip())
    )


def matches_allocation_amount_status_filter(
    *,
    filter_value: str,
    acceptance_status: str,
    abnormal_message,
) -> bool:
    normalized_filter = (
        normalize_allocation_amount_status_filter(
            filter_value
        )
    )

    if normalized_filter == ALLOCATION_AMOUNT_STATUS_ALL:
        return True

    return is_business_allocation_amount_abnormal(
        acceptance_status=acceptance_status,
        abnormal_message=abnormal_message,
    )


def classify_business_status(
    *,
    acceptance_status: str,
    has_matching_record: bool,
    allocation_status: str,
) -> str | None:
    """按业务维度返回与经营看板一致的互斥处理状态。"""
    if acceptance_status != "已承接":
        return None

    if allocation_status == ALLOCATION_STATUS_COMPLETED:
        return BUSINESS_STATUS_SETTLED

    if has_matching_record:
        return BUSINESS_STATUS_MATCHED_UNSETTLED

    return BUSINESS_STATUS_UNMATCHED
