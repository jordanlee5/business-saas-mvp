"""商城订单后台只读列表及订单快照详情。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_

from ..models import Member, Order, OrderItem, OrderPointsGrantAllocation
from ..time_utils import UTC8_TIMEZONE
from .order_fulfillment_service import _validate_order_totals


ORDER_STATUS_ALL = "ALL"
ORDER_STATUS_LABELS = {
    "CREATED": "待处理", "CANCELLED": "已取消",
    "FULFILLING": "待发货", "SHIPPED": "已发货",
    "COMPLETED": "已完成", "REFUNDED": "已退款",
}


@dataclass(frozen=True)
class MallOrderListItem:
    order_public_id: str
    member_public_id: str
    status: str
    status_label: str
    total_points: Decimal
    total_cost_amount: Decimal
    total_quantity: int
    created_at: datetime


@dataclass(frozen=True)
class MallOrderPage:
    items: tuple[MallOrderListItem, ...]
    keyword: str
    status: str
    page: int
    page_size: int
    total: int
    total_pages: int


@dataclass(frozen=True)
class MallOrderDetailItem:
    product_public_id: str
    product_name: str
    sku_code: str
    sku_name: str
    supplier_public_id: str
    supplier_name: str
    unit_points_price: Decimal
    unit_cost_price: Decimal
    quantity: int
    line_points: Decimal
    line_cost_amount: Decimal


@dataclass(frozen=True)
class MallOrderDetail:
    order: MallOrderListItem
    shipping_carrier: str | None
    tracking_number: str | None
    shipped_at: datetime | None
    completed_at: datetime | None
    refunded_at: datetime | None
    refund_reason: str | None
    items: tuple[MallOrderDetailItem, ...]


def _time(value):
    if value is None:
        return None
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return value


def _listing_item(order, member_public_id):
    label = ORDER_STATUS_LABELS.get(order.status)
    if label is None:
        raise RuntimeError("商城订单状态异常")
    return MallOrderListItem(
        order_public_id=order.order_public_id,
        member_public_id=member_public_id,
        status=order.status,
        status_label=label,
        total_points=Decimal(order.total_points),
        total_cost_amount=Decimal(order.total_cost_amount),
        total_quantity=order.total_quantity,
        created_at=_time(order.created_at),
    )


def list_mall_orders(db, *, keyword="", status=ORDER_STATUS_ALL,
                     page=1, page_size=20) -> MallOrderPage:
    if not isinstance(keyword, str) or len(keyword.strip()) > 100:
        raise ValueError("订单查询关键词无效")
    if not isinstance(status, str) or status not in (
        ORDER_STATUS_ALL, *ORDER_STATUS_LABELS,
    ):
        raise ValueError("订单状态无效")
    if (isinstance(page, bool) or not isinstance(page, int) or page < 1
            or isinstance(page_size, bool) or not isinstance(page_size, int)
            or not 1 <= page_size <= 100):
        raise ValueError("订单分页参数无效")
    keyword = " ".join(keyword.split())
    with db.no_autoflush:
        query = db.query(Order, Member.member_public_id).join(
            Member, Member.id == Order.member_id
        )
        if status != ORDER_STATUS_ALL:
            query = query.filter(Order.status == status)
        if keyword:
            # Exact lookup avoids wildcard expansion and costly unbounded scans.
            query = query.filter(or_(
                Order.order_public_id == keyword,
                Member.member_public_id == keyword,
            ))
        total = query.count()
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = query.order_by(
            Order.created_at.desc(), Order.id.desc()
        ).offset((page - 1) * page_size).limit(page_size).all()
        return MallOrderPage(
            items=tuple(_listing_item(order, member_id) for order, member_id in rows),
            keyword=keyword, status=status, page=page,
            page_size=page_size, total=total, total_pages=pages,
        )


def get_mall_order_detail(db, *, order_public_id: str) -> MallOrderDetail:
    if (not isinstance(order_public_id, str) or not order_public_id.strip()
            or len(order_public_id) > 32):
        raise ValueError("商城订单编号无效")
    with db.no_autoflush:
        row = db.query(Order, Member.member_public_id).join(
            Member, Member.id == Order.member_id
        ).filter(Order.order_public_id == order_public_id).one_or_none()
        if row is None:
            raise ValueError("商城订单不存在")
        order, member_id = row
        items = tuple(db.query(OrderItem).filter(
            OrderItem.order_id == order.id
        ).order_by(OrderItem.id.asc()).all())
        allocations = tuple(db.query(OrderPointsGrantAllocation).filter(
            OrderPointsGrantAllocation.order_id == order.id
        ).all())
        _validate_order_totals(order, items, allocations)
        if (order.status in ("SHIPPED", "COMPLETED", "REFUNDED")
                and not (order.shipping_carrier and order.tracking_number
                         and order.shipped_at)):
            raise RuntimeError("商城订单物流证据不完整")
        if (order.status in ("COMPLETED", "REFUNDED")
                and order.completed_at is None):
            raise RuntimeError("商城订单完成证据不完整")
        if order.status == "REFUNDED" and not (
            order.refunded_at and order.refund_reason
        ):
            raise RuntimeError("商城订单退款证据不完整")
        return MallOrderDetail(
            order=_listing_item(order, member_id),
            shipping_carrier=order.shipping_carrier,
            tracking_number=order.tracking_number,
            shipped_at=_time(order.shipped_at),
            completed_at=_time(order.completed_at),
            refunded_at=_time(order.refunded_at),
            refund_reason=order.refund_reason,
            items=tuple(MallOrderDetailItem(
                product_public_id=item.product_public_id_snapshot,
                product_name=item.product_name_snapshot,
                sku_code=item.sku_code_snapshot,
                sku_name=item.sku_name_snapshot,
                supplier_public_id=item.supplier_public_id_snapshot,
                supplier_name=item.supplier_name_snapshot,
                unit_points_price=Decimal(item.unit_points_price),
                unit_cost_price=Decimal(item.unit_cost_price),
                quantity=item.quantity,
                line_points=Decimal(item.line_points),
                line_cost_amount=Decimal(item.line_cost_amount),
            ) for item in items),
        )
