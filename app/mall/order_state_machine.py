"""订单现有持久化状态的纯函数流转约束。"""

from dataclasses import dataclass
from enum import Enum


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    CANCELLED = "CANCELLED"
    FULFILLING = "FULFILLING"
    SHIPPED = "SHIPPED"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"


class OrderAction(str, Enum):
    CANCEL = "CANCEL"
    FULFILL = "FULFILL"
    SHIP = "SHIP"
    COMPLETE = "COMPLETE"
    REFUND = "REFUND"


@dataclass(frozen=True)
class OrderTransition:
    status: str
    replayed: bool


# 对同一操作、同一目标状态的重放仍由领域服务验证请求和证据。
_TRANSITIONS = {
    OrderAction.CANCEL: (OrderStatus.CREATED, OrderStatus.CANCELLED, "取消"),
    OrderAction.FULFILL: (OrderStatus.CREATED, OrderStatus.FULFILLING, "确认履约"),
    OrderAction.SHIP: (OrderStatus.FULFILLING, OrderStatus.SHIPPED, "发货"),
    OrderAction.COMPLETE: (OrderStatus.SHIPPED, OrderStatus.COMPLETED, "确认完成"),
    OrderAction.REFUND: (OrderStatus.COMPLETED, OrderStatus.REFUNDED, "退款"),
}


def resolve_order_transition(status: str, action: OrderAction) -> OrderTransition:
    """返回目标及重放标记；其他状态、操作组合一律失败关闭。"""
    if not isinstance(action, OrderAction):
        raise ValueError("订单操作无效")
    source, target, label = _TRANSITIONS[action]
    if status == source.value:
        return OrderTransition(status=target.value, replayed=False)
    if status == target.value:
        return OrderTransition(status=target.value, replayed=True)
    raise ValueError(f"当前订单状态不允许{label}")
