from collections.abc import Collection


SUPER_ADMIN = "super_admin"
PRIMARY_REVIEWER = "primary_reviewer"
SECONDARY_REVIEWER = "secondary_reviewer"
OPERATOR = "operator"


# 系统允许的管理员级别
VALID_ADMIN_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
        SECONDARY_REVIEWER,
        OPERATOR,
    }
)


# 可以管理管理员账号
ADMIN_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
    }
)


# 可以执行初审
PRIMARY_REVIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
    }
)


# 可以执行二级复核
SECONDARY_REVIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        SECONDARY_REVIEWER,
    }
)


# 可以执行运营类操作
OPERATION_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


def get_admin_level(user: object | None) -> str | None:
    """
    返回有效的管理员级别。

    非管理员账号、管理员级别为空或管理员级别无效时，
    统一返回 None。
    """
    if user is None:
        return None

    if getattr(user, "role", None) != "admin":
        return None

    admin_level = getattr(user, "admin_level", None)

    if admin_level not in VALID_ADMIN_LEVELS:
        return None

    return admin_level


def is_admin_user(user: object | None) -> bool:
    """是否为具有有效管理员级别的管理员账号。"""
    return get_admin_level(user) is not None


def is_super_admin(user: object | None) -> bool:
    """是否为超级管理员。"""
    return get_admin_level(user) == SUPER_ADMIN


def has_admin_level(
    user: object | None,
    allowed_levels: Collection[str],
) -> bool:
    """账号的管理员级别是否位于允许范围内。"""
    admin_level = get_admin_level(user)

    return (
        admin_level is not None
        and admin_level in allowed_levels
    )


def can_manage_administrators(user: object | None) -> bool:
    """是否可以创建、编辑、启停管理员账号。"""
    return has_admin_level(
        user,
        ADMIN_MANAGEMENT_LEVELS,
    )


def can_primary_review(user: object | None) -> bool:
    """是否可以执行匹配结果初审。"""
    return has_admin_level(
        user,
        PRIMARY_REVIEW_LEVELS,
    )


def can_secondary_review(user: object | None) -> bool:
    """是否可以执行匹配结果二级复核。"""
    return has_admin_level(
        user,
        SECONDARY_REVIEW_LEVELS,
    )


def can_operate(user: object | None) -> bool:
    """是否可以执行运营类操作。"""
    return has_admin_level(
        user,
        OPERATION_LEVELS,
    )
