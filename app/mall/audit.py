from enum import Enum


class MallAuditActionType(str, Enum):
    """商城后台写操作使用的稳定审计动作类型。"""

    PRODUCT_CREATE = "mall_product_create"
    PRODUCT_UPDATE = "mall_product_update"
    PRODUCT_PUBLISH = "mall_product_publish"
    PRODUCT_UNPUBLISH = "mall_product_unpublish"
    SKU_CREATE = "mall_sku_create"
    SKU_UPDATE = "mall_sku_update"
    INVENTORY_ADJUST = "mall_inventory_adjust"
    ORDER_CANCEL = "mall_order_cancel"
    ORDER_SHIP = "mall_order_ship"
    ORDER_REFUND = "mall_order_refund"
    POINTS_ADJUST = "mall_points_adjust"
    SUPPLIER_CREATE = "mall_supplier_create"
    SUPPLIER_UPDATE = "mall_supplier_update"
    SUPPLIER_SETTLEMENT_GENERATE = (
        "mall_supplier_settlement_generate"
    )
    SUPPLIER_SETTLEMENT_CONFIRM = (
        "mall_supplier_settlement_confirm"
    )


VALID_MALL_AUDIT_ACTION_TYPES = frozenset(
    action.value
    for action in MallAuditActionType
)


def normalize_mall_audit_action_type(
    value: MallAuditActionType | str,
) -> MallAuditActionType:
    """
    将外部值规范为已登记的商城审计动作类型。

    只接受枚举或精确的字符串值。未知动作必须显式失败，
    防止后台新增写操作时绕过审计类型和权限分级。
    """
    if isinstance(value, MallAuditActionType):
        return value

    if not isinstance(value, str):
        raise ValueError(
            "商城审计动作类型无效"
        )

    normalized_value = value.strip()

    try:
        return MallAuditActionType(
            normalized_value
        )
    except ValueError as exc:
        raise ValueError(
            "商城审计动作类型无效"
        ) from exc
