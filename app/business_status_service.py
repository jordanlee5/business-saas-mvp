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


def normalize_business_status_filter(value) -> str:
    if value in BUSINESS_STATUS_FILTER_OPTIONS:
        return value

    return BUSINESS_STATUS_ALL


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
