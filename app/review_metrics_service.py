from dataclasses import dataclass

from .match_review_workflow import (
    APPROVED_REVIEW_STATUS,
    PENDING_SECONDARY_REVIEW_STATUS,
    PRIMARY_PENDING_REVIEW_STATUSES,
    REJECTED_REVIEW_STATUS,
)
from .voucher_allocation import (
    get_business_allocation_status,
)


@dataclass(frozen=True)
class ReviewMetrics:
    pending_primary_reviews: int
    pending_secondary_reviews: int
    pending_reviews: int
    approved_reviews: int
    rejected_reviews: int
    completed_reviews: int
    matched_business_count: int
    business_match_coverage_rate: float | None
    review_approval_rate: float | None


def _percentage(
    numerator: int,
    denominator: int,
) -> float | None:
    if denominator <= 0:
        return None

    return round(
        numerator * 100 / denominator,
        2,
    )


def build_review_metrics(
    reviews,
    *,
    total_business_records: int,
) -> ReviewMetrics:
    """
    汇总经营看板使用的审核指标。

    历史“待审核”和当前“待初审”统一计入待初审；
    业务匹配覆盖率按至少存在一条匹配候选的业务去重计算。
    """
    review_list = list(reviews or ())

    pending_primary_reviews = sum(
        1
        for review in review_list
        if getattr(
            review,
            "review_status",
            None,
        )
        in PRIMARY_PENDING_REVIEW_STATUSES
    )

    pending_secondary_reviews = sum(
        1
        for review in review_list
        if getattr(
            review,
            "review_status",
            None,
        )
        == PENDING_SECONDARY_REVIEW_STATUS
    )

    approved_reviews = sum(
        1
        for review in review_list
        if getattr(
            review,
            "review_status",
            None,
        )
        == APPROVED_REVIEW_STATUS
    )

    rejected_reviews = sum(
        1
        for review in review_list
        if getattr(
            review,
            "review_status",
            None,
        )
        == REJECTED_REVIEW_STATUS
    )

    matched_business_ids = {
        business_record_id
        for review in review_list
        if (
            business_record_id
            := getattr(
                review,
                "business_record_id",
                None,
            )
        )
        is not None
    }

    pending_reviews = (
        pending_primary_reviews
        + pending_secondary_reviews
    )
    completed_reviews = (
        approved_reviews
        + rejected_reviews
    )
    matched_business_count = len(
        matched_business_ids
    )

    return ReviewMetrics(
        pending_primary_reviews=(
            pending_primary_reviews
        ),
        pending_secondary_reviews=(
            pending_secondary_reviews
        ),
        pending_reviews=pending_reviews,
        approved_reviews=approved_reviews,
        rejected_reviews=rejected_reviews,
        completed_reviews=completed_reviews,
        matched_business_count=(
            matched_business_count
        ),
        business_match_coverage_rate=(
            _percentage(
                matched_business_count,
                total_business_records,
            )
        ),
        review_approval_rate=(
            _percentage(
                approved_reviews,
                completed_reviews,
            )
        ),
    )


def get_business_review_allocation_status(
    business_amount,
    reviews,
) -> str:
    """
    使用待复核与已通过的核销金额判断业务结算状态。

    只有复核通过金额足额且不存在超额预占时，
    才返回“已付清”；后续新增的驳回记录不会覆盖既有结算结果。
    """
    review_list = list(reviews or ())

    approved_allocation_amounts = [
        getattr(
            review,
            "allocation_amount",
            None,
        )
        for review in review_list
        if getattr(
            review,
            "review_status",
            None,
        )
        == APPROVED_REVIEW_STATUS
    ]

    reserved_allocation_amounts = [
        getattr(
            review,
            "allocation_amount",
            None,
        )
        for review in review_list
        if getattr(
            review,
            "review_status",
            None,
        )
        in {
            PENDING_SECONDARY_REVIEW_STATUS,
            APPROVED_REVIEW_STATUS,
        }
    ]

    return get_business_allocation_status(
        business_amount=(
            business_amount
        ),
        approved_allocation_amounts=(
            approved_allocation_amounts
        ),
        reserved_allocation_amounts=(
            reserved_allocation_amounts
        ),
    )
