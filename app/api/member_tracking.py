"""Per-parcel member lookup, releasing the database before provider work."""
from fastapi import APIRouter, Depends, Header, HTTPException, Path
from sqlalchemy import exists

from .member_addresses import address_session
from .member_orders import PrivateOrderRoute, owned
from .miniprogram_v1 import _require_current_member
from ..mall.parcel_tracking import tracking_service
from ..models import OrderParcel, OrderParcelItem, OrderItem

router = APIRouter(prefix="/orders", route_class=PrivateOrderRoute)


@router.get("/{order_id}/parcels/{parcel_id}/tracking")
def get_tracking(order_id: str, parcel_id: int = Path(ge=1, le=2147483647),
                 authorization: str | None = Header(None),
                 session=Depends(address_session), service=Depends(tracking_service)):
    with session as db:
        member = _require_current_member(authorization, db)
        order = owned(db, member.id, order_id)
        parcel = db.query(OrderParcel).filter_by(id=parcel_id, order_id=order.id).one_or_none()
        if parcel is None or not db.query(exists().where(
                (OrderParcelItem.parcel_id == parcel.id) &
                (OrderParcelItem.item_id == OrderItem.id) &
                (OrderItem.order_id == order.id))).scalar():
            raise HTTPException(404, "包裹不存在")
        carrier, number = parcel.carrier, parcel.tracking_number
        key = (member.id, order.id, parcel.id, carrier, number)
    # No DB transaction/connection or business write during the provider call.
    return {"parcel_id": parcel_id, **service.query(key, carrier, number)}
