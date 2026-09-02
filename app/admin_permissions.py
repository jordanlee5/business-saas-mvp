from collections.abc import Collection
from types import MappingProxyType

from .mall.audit import (
    MallAuditActionType,
    normalize_mall_audit_action_type,
)


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


# 可以创建、编辑和发布保险公司宣传页
PROMOTION_PAGE_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


# 商城普通运营写操作：超级管理员与运营管理员。
#
# 各业务域保留独立集合，当前即使权限相同也不合并，
# 便于后续在不影响其他商城模块的情况下按业务风险继续细分。
MALL_CATALOG_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


MALL_INVENTORY_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


MALL_ORDER_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


MALL_SUPPLIER_MANAGEMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
        OPERATOR,
    }
)


# 高风险写操作只允许超级管理员。
MALL_POINTS_ADJUSTMENT_LEVELS = frozenset(
    {
        SUPER_ADMIN,
    }
)


MALL_SUPPLIER_SETTLEMENT_CONFIRMATION_LEVELS = (
    frozenset(
        {
            SUPER_ADMIN,
        }
    )
)


# 每个审计动作都必须显式分级。新增枚举但未加入此映射时，
# can_perform_mall_audit_action 会失败关闭，而不是默认放行。
MALL_AUDIT_ACTION_LEVELS = MappingProxyType(
    {
        MallAuditActionType.PRODUCT_CREATE:
            MALL_CATALOG_MANAGEMENT_LEVELS,
        MallAuditActionType.PRODUCT_UPDATE:
            MALL_CATALOG_MANAGEMENT_LEVELS,
        MallAuditActionType.PRODUCT_PUBLISH:
            MALL_CATALOG_MANAGEMENT_LEVELS,
        MallAuditActionType.PRODUCT_UNPUBLISH:
            MALL_CATALOG_MANAGEMENT_LEVELS,
        MallAuditActionType.SKU_CREATE:
            MALL_CATALOG_MANAGEMENT_LEVELS,
        MallAuditActionType.SKU_UPDATE:
            MALL_CATALOG_MANAGEMENT_LEVELS,
        MallAuditActionType.INVENTORY_ADJUST:
            MALL_INVENTORY_MANAGEMENT_LEVELS,
        MallAuditActionType.ORDER_CANCEL:
            MALL_ORDER_MANAGEMENT_LEVELS,
        MallAuditActionType.ORDER_SHIP:
            MALL_ORDER_MANAGEMENT_LEVELS,
        MallAuditActionType.ORDER_REFUND:
            MALL_ORDER_MANAGEMENT_LEVELS,
        MallAuditActionType.POINTS_ADJUST:
            MALL_POINTS_ADJUSTMENT_LEVELS,
        MallAuditActionType.SUPPLIER_CREATE:
            MALL_SUPPLIER_MANAGEMENT_LEVELS,
        MallAuditActionType.SUPPLIER_UPDATE:
            MALL_SUPPLIER_MANAGEMENT_LEVELS,
        MallAuditActionType.SUPPLIER_SETTLEMENT_GENERATE:
            MALL_SUPPLIER_MANAGEMENT_LEVELS,
        MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM:
            MALL_SUPPLIER_SETTLEMENT_CONFIRMATION_LEVELS,
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


def can_edit_administrator_account(
    current_user: object | None,
    target_user: object | None,
) -> bool:
    """
    当前管理员是否可以编辑目标管理员账号。

    安全规则：
    1. 当前用户必须拥有管理员账号管理权限；
    2. 目标必须是管理员账号；
    3. 禁止编辑当前登录账号；
    4. 禁止通过普通编辑流程修改超级管理员；
    5. 缺少有效用户 ID 时默认拒绝。
    """
    if not can_manage_administrators(current_user):
        return False

    if target_user is None:
        return False

    if getattr(target_user, "role", None) != "admin":
        return False

    current_user_id = getattr(
        current_user,
        "id",
        None,
    )

    target_user_id = getattr(
        target_user,
        "id",
        None,
    )

    if (
        current_user_id is None
        or target_user_id is None
    ):
        return False

    if current_user_id == target_user_id:
        return False

    if is_super_admin(target_user):
        return False

    return True


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


def can_manage_promotion_pages(
    user: object | None,
) -> bool:
    """是否可以创建、编辑和发布宣传页。"""
    return has_admin_level(
        user,
        PROMOTION_PAGE_MANAGEMENT_LEVELS,
    )


def can_manage_mall_catalog(
    user: object | None,
) -> bool:
    """是否可以管理商城商品、SKU 与上下架状态。"""
    return has_admin_level(
        user,
        MALL_CATALOG_MANAGEMENT_LEVELS,
    )


def can_manage_mall_inventory(
    user: object | None,
) -> bool:
    """是否可以执行商城库存调整。"""
    return has_admin_level(
        user,
        MALL_INVENTORY_MANAGEMENT_LEVELS,
    )


def can_manage_mall_orders(
    user: object | None,
) -> bool:
    """是否可以执行商城订单取消、发货和退款操作。"""
    return has_admin_level(
        user,
        MALL_ORDER_MANAGEMENT_LEVELS,
    )


def can_manage_mall_suppliers(
    user: object | None,
) -> bool:
    """是否可以维护供应商并生成待确认结算。"""
    return has_admin_level(
        user,
        MALL_SUPPLIER_MANAGEMENT_LEVELS,
    )


def can_adjust_mall_points(
    user: object | None,
) -> bool:
    """是否可以人工调整会员积分。"""
    return has_admin_level(
        user,
        MALL_POINTS_ADJUSTMENT_LEVELS,
    )


def can_confirm_mall_supplier_settlements(
    user: object | None,
) -> bool:
    """是否可以确认供应商结算。"""
    return has_admin_level(
        user,
        MALL_SUPPLIER_SETTLEMENT_CONFIRMATION_LEVELS,
    )


def can_perform_mall_audit_action(
    user: object | None,
    action_type: MallAuditActionType | str,
) -> bool:
    """
    是否可以执行指定的商城审计动作。

    未登记的动作或未分级的动作一律拒绝，供后续商城路由
    和领域服务在写入前进行统一的失败关闭权限检查。
    """
    try:
        normalized_action = (
            normalize_mall_audit_action_type(
                action_type
            )
        )
    except ValueError:
        return False

    allowed_levels = MALL_AUDIT_ACTION_LEVELS.get(
        normalized_action
    )

    if allowed_levels is None:
        return False

    return has_admin_level(
        user,
        allowed_levels,
    )
