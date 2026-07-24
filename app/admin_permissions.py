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


# 可以执行匹配结果初审
PRIMARY_REVIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
    }
)


# 可以执行匹配结果二级复核
SECONDARY_REVIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        SECONDARY_REVIEWER,
    }
)


# 可以执行传统运营类写操作
#
# 暂时保留现有定义，避免影响尚未完成权限拆分的旧路由。
# 后续将逐个路由替换为更精细的权限函数。
OPERATION_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


# 可以进入凭证识别页面并上传凭证
VOUCHER_UPLOAD_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
    }
)


# 可以查看上传方账号列表
PARTNER_VIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
        OPERATOR,
    }
)


# 可以创建、编辑、停用或恢复上传方账号
PARTNER_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


# 可以查看业务数据列表、上传批次和业务详情
BUSINESS_VIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
        SECONDARY_REVIEWER,
        OPERATOR,
    }
)


# 可以承接或拒绝上传方提交的业务批次
BUSINESS_BATCH_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


# 可以导出包含客户明细的业务数据
BUSINESS_EXPORT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


# 可以查看经营数据看板
STATS_VIEW_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
        OPERATOR,
    }
)


# 可以导出经营汇总数据
STATS_EXPORT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        PRIMARY_REVIEWER,
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
    """是否可以执行尚未拆分的传统运营类写操作。"""
    return has_admin_level(
        user,
        OPERATION_LEVELS,
    )


def can_upload_vouchers(user: object | None) -> bool:
    """是否可以进入凭证识别页面并上传凭证。"""
    return has_admin_level(
        user,
        VOUCHER_UPLOAD_LEVELS,
    )


def can_view_partners(user: object | None) -> bool:
    """是否可以只读查看上传方账号列表与费率配置。"""
    return has_admin_level(
        user,
        PARTNER_VIEW_LEVELS,
    )


def can_manage_partners(user: object | None) -> bool:
    """是否可以创建、编辑、停用或恢复上传方账号。"""
    return has_admin_level(
        user,
        PARTNER_MANAGEMENT_LEVELS,
    )


def can_view_business_records(user: object | None) -> bool:
    """是否可以查看业务数据列表、上传批次和业务详情。"""
    return has_admin_level(
        user,
        BUSINESS_VIEW_LEVELS,
    )


def can_manage_business_batches(user: object | None) -> bool:
    """是否可以承接或拒绝上传方提交的业务批次。"""
    return has_admin_level(
        user,
        BUSINESS_BATCH_MANAGEMENT_LEVELS,
    )


def can_export_business_records(user: object | None) -> bool:
    """是否可以导出包含客户明细的业务数据。"""
    return has_admin_level(
        user,
        BUSINESS_EXPORT_LEVELS,
    )


def can_view_stats(user: object | None) -> bool:
    """是否可以查看经营数据看板。"""
    return has_admin_level(
        user,
        STATS_VIEW_LEVELS,
    )


def can_export_stats(user: object | None) -> bool:
    """是否可以导出经营汇总数据。"""
    return has_admin_level(
        user,
        STATS_EXPORT_LEVELS,
    )