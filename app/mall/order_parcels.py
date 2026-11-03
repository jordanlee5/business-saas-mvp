"""Order-item quantities physically dispatched in each recorded parcel."""
from collections import Counter
from sqlalchemy import case, exists
from sqlalchemy.exc import IntegrityError

PARTIAL = "PARTIALLY_SHIPPED"


def display_status():
    from ..models import Order, OrderParcel
    has_parcel = exists().where(OrderParcel.order_id == Order.id)
    return case(((Order.status == "FULFILLING") & has_parcel, PARTIAL), else_=Order.status)


def parcels_for_order(db, order_id):
    from ..models import OrderParcel, OrderParcelItem, OrderItem
    rows = (db.query(OrderParcel, OrderParcelItem, OrderItem)
            .join(OrderParcelItem, OrderParcelItem.parcel_id == OrderParcel.id)
            .join(OrderItem, OrderItem.id == OrderParcelItem.item_id)
            .filter(OrderParcel.order_id == order_id, OrderItem.order_id == order_id)
            .order_by(OrderParcel.id, OrderItem.id))
    parcels = {}
    for parcel, line, item in rows:
        data = parcels.setdefault(parcel.id, dict(carrier=parcel.carrier, tracking_number=parcel.tracking_number, items=[]))
        data["items"].append(dict(product_name=item.product_name_snapshot, sku_name=item.sku_name_snapshot,
                                  quantity=line.quantity, item_id=item.id))
    return list(parcels.values())


def record_parcel(db, order, actor, carrier, tracking, quantities, when):
    """Caller holds the order lock. Return (parcel, replay, all quantities shipped)."""
    from ..models import Order, OrderItem, OrderParcel, OrderParcelItem
    items = db.query(OrderItem).filter_by(order_id=order.id).all()
    ordered = {item.id: item.quantity for item in items}
    requested = ordered if quantities is None else quantities
    if not requested or any(key not in ordered or type(qty) is not int or qty < 1 for key, qty in requested.items()):
        raise ValueError("请核对本包裹的商品和数量")
    if len({item.supplier_id for item in items if item.id in requested}) != 1:
        raise ValueError("请按供应商分别登记包裹")
    prior = db.query(OrderParcel).filter_by(carrier=carrier, tracking_number=tracking).one_or_none()
    if prior:
        lines = db.query(OrderParcelItem).filter_by(parcel_id=prior.id)
        if prior.order_id != order.id or {line.item_id: line.quantity for line in lines} != requested:
            raise ValueError("该运单已用于其他订单或商品，请核对")
        return prior, True, order.status == "SHIPPED"

    if order.status != "FULFILLING":
        raise ValueError("订单已使用其他物流信息发货")
    sent = Counter()
    for parcel in parcels_for_order(db, order.id):
        for item in parcel["items"]:
            sent[item["item_id"]] += item["quantity"]
    sent.update(requested)
    if any(sent[key] > quantity for key, quantity in ordered.items()):
        raise ValueError("包裹商品数量超过待发数量")
    if db.query(Order.id).filter_by(shipping_carrier=carrier, tracking_number=tracking).first():
        raise ValueError("物流公司与运单号已用于其他订单")
    parcel = OrderParcel(order_id=order.id, actor_id=actor.id, carrier=carrier,
                         tracking_number=tracking, shipped_at=when)
    db.add(parcel)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ValueError("运单登记冲突，请刷新后核对") from exc
    db.add_all([OrderParcelItem(parcel_id=parcel.id, item_id=key, quantity=quantity)
                for key, quantity in requested.items()])
    db.flush()
    return parcel, False, sent == ordered
