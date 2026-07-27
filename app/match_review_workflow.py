from .admin_permissions import (
    can_primary_review,
    can_secondary_review,
)

from datetime import datetime, timezone


# 兼容现有历史数据
LEGACY_PENDING_REVIEW_STATUS = "待审核"

PENDING_PRIMARY_REVIEW_STATUS = "待初审"
PENDING_SECONDARY_REVIEW_STATUS = "待复核"
APPROVED_REVIEW_STATUS = "已通过"
REJECTED_REVIEW_STATUS = "已驳回"

REVIEW_RESULT_APPROVED = "通过"
REVIEW_RESULT_REJECTED = "驳回"


PRIMARY_PENDING_REVIEW_STATUSES = frozenset(
    {
        LEGACY_PENDING_REVIEW_STATUS,
        PENDING_PRIMARY_REVIEW_STATUS,
    }
)


def can_primary_review_match(
    user: object | None,
    review: object | None,
) -> bool:
    """当前用户是否可以初审指定匹配记录。"""
    if not can_primary_review(user):
        return False

    if review is None:
        return False

    return (
        getattr(review, "review_status", None)
        in PRIMARY_PENDING_REVIEW_STATUSES
    )


def can_secondary_review_match(
    user: object | None,
    review: object | None,
) -> bool:
    """
    当前用户是否可以二级复核指定匹配记录。

    除权限和状态外，还要求：
    1. 已记录初审人；
    2. 当前复核人与初审人不是同一账号。
    """
    if not can_secondary_review(user):
        return False

    if review is None:
        return False

    if (
        getattr(review, "review_status", None)
        != PENDING_SECONDARY_REVIEW_STATUS
    ):
        return False

    current_user_id = getattr(user, "id", None)
    primary_reviewer_id = getattr(
        review,
        "primary_reviewer_id",
        None,
    )

    if (
        current_user_id is None
        or primary_reviewer_id is None
    ):
        return False

    return current_user_id != primary_reviewer_id

VALID_REVIEW_RESULTS = frozenset(
    {
        REVIEW_RESULT_APPROVED,
        REVIEW_RESULT_REJECTED,
    }
)


def normalize_review_comment(
    comment: str | None,
) -> str | None:
    """清理审核意见，空白内容统一保存为 None。"""
    normalized_comment = (comment or "").strip()

    return normalized_comment or None


def apply_primary_review_decision(
    user: object | None,
    review: object | None,
    result: str,
    comment: str | None = None,
    reviewed_at: datetime | None = None,
) -> bool:
    """
    写入初审决定。

    初审通过后进入待复核；
    初审驳回后直接进入已驳回。
    """
    if not can_primary_review_match(user, review):
        return False

    if result not in VALID_REVIEW_RESULTS:
        return False

    reviewer_id = getattr(user, "id", None)

    if reviewer_id is None:
        return False

    normalized_comment = normalize_review_comment(
        comment
    )

    # 驳回必须填写原因
    if (
        result == REVIEW_RESULT_REJECTED
        and normalized_comment is None
    ):
        return False

    review.primary_reviewer_id = reviewer_id
    review.primary_review_result = result
    review.primary_review_comment = normalized_comment
    review.primary_reviewed_at = (
        reviewed_at
        or datetime.now(timezone.utc)
    )

    # 重新初审时清除可能残留的二级复核数据
    review.secondary_reviewer_id = None
    review.secondary_review_result = None
    review.secondary_review_comment = None
    review.secondary_reviewed_at = None

    if result == REVIEW_RESULT_APPROVED:
        review.review_status = (
            PENDING_SECONDARY_REVIEW_STATUS
        )
    else:
        review.review_status = REJECTED_REVIEW_STATUS

    return True


def apply_secondary_review_decision(
    user: object | None,
    review: object | None,
    result: str,
    comment: str | None = None,
    reviewed_at: datetime | None = None,
) -> bool:
    """
    写入二级复核决定。

    只有二级复核通过，整体状态才会变为已通过。
    """
    if not can_secondary_review_match(user, review):
        return False

    if result not in VALID_REVIEW_RESULTS:
        return False

    reviewer_id = getattr(user, "id", None)

    if reviewer_id is None:
        return False

    normalized_comment = normalize_review_comment(
        comment
    )

    # 驳回必须填写原因
    if (
        result == REVIEW_RESULT_REJECTED
        and normalized_comment is None
    ):
        return False

    review.secondary_reviewer_id = reviewer_id
    review.secondary_review_result = result
    review.secondary_review_comment = normalized_comment
    review.secondary_reviewed_at = (
        reviewed_at
        or datetime.now(timezone.utc)
    )

    if result == REVIEW_RESULT_APPROVED:
        review.review_status = APPROVED_REVIEW_STATUS
    else:
        review.review_status = REJECTED_REVIEW_STATUS

    return True