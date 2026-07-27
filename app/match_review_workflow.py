from .admin_permissions import (
    can_primary_review,
    can_secondary_review,
)


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