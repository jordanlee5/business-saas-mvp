"""Private checkout and member orders; atomic reservations and delivery evidence."""
import hashlib
import json
from decimal import Decimal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from .member_addresses import address_session, AddressBody, _commit
from .miniprogram_v1 import _require_current_member
from ..models import Member, MemberAddress, Order, OrderItem, OrderDeliverySnapshot, Product, ProductSku
from ..mall.member_address_service import ADDRESS_FIELDS, normalize_address, AddressInputError
from ..mall.order_service import OrderLineRequest, place_order_with_reservations
from ..mall.order_cancellation_service import cancel_created_order
from ..time_utils import UTC8_TIMEZONE

class PrivateOrderRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handle(request):
            try: response=await original(request)
            except RequestValidationError:
                response=JSONResponse({'detail':'订单请求格式不正确'},status_code=422)
            except HTTPException as exc:
                response=JSONResponse({'detail':exc.detail},status_code=exc.status_code)
            except AddressInputError:
                response=JSONResponse({'detail':'收货地址格式不正确'},status_code=422)
            except ValueError:
                response=JSONResponse({'detail':'订单条件已变化，请核对商品、积分、库存及订单状态后重试'},status_code=409)
            except (SQLAlchemyError,RuntimeError):
                response=JSONResponse({'detail':'订单服务暂不可用，请查询订单确认结果'},status_code=503)
            response.headers['Cache-Control']='no-store, private'
            return response
        return handle

order_router=APIRouter(prefix='/orders',route_class=PrivateOrderRoute)
class CheckoutLine(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    product_public_id:str=Field(pattern=r'^[A-Z0-9-]{1,32}$')
    sku_code:str=Field(min_length=1,max_length=100)
    quantity:int=Field(ge=1,le=99)
    expected_points_price:str=Field(pattern=r'^\d{1,10}\.\d{2}$')
class CheckoutBody(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    address_public_id:str=Field(pattern=r'^ADDR-[a-f0-9]{32}$')
    expected_address:AddressBody
    lines:list[CheckoutLine]=Field(min_length=1,max_length=20)

def stamp(value):
    if value is None:return None
    if value.tzinfo is None:value=value.replace(tzinfo=UTC8_TIMEZONE)
    return value.astimezone(UTC8_TIMEZONE).isoformat(timespec='seconds')
def summary(order):
    return {'order_public_id':order.order_public_id,'status':order.status,
            'total_points':f'{order.total_points:.2f}','total_quantity':order.total_quantity,'created_at':stamp(order.created_at)}
def detail(db,order):
    snapshot=db.get(OrderDeliverySnapshot,order.id)
    return {**summary(order),
        'address':{key:getattr(snapshot,key) for key in ADDRESS_FIELDS} if snapshot else None,
        'shipping_carrier':order.shipping_carrier,'tracking_number':order.tracking_number,
        'shipped_at':stamp(order.shipped_at),'completed_at':stamp(order.completed_at),
        'items':[{'product_name':x.product_name_snapshot,'sku_name':x.sku_name_snapshot,
                  'quantity':x.quantity,'unit_points_price':f'{x.unit_points_price:.2f}','line_points':f'{x.line_points:.2f}'}
                 for x in db.query(OrderItem).filter_by(order_id=order.id).order_by(OrderItem.id)]}
def owned(db,member_id,public_id):
    order=db.query(Order).filter_by(member_id=member_id,order_public_id=public_id).one_or_none()
    if order is None:raise HTTPException(404,'订单不存在')
    return order

@order_router.post('',status_code=201)
def checkout(body:CheckoutBody,response:Response,authorization:str|None=Header(None),db:Session=Depends(address_session)):
    member=_require_current_member(authorization,db)
    # Match address-book serialization. Identical member requests cannot create twice.
    member=db.query(Member).filter_by(id=member.id).with_for_update().populate_existing().one()
    if not member.is_active:raise HTTPException(401,'会员会话无效')
    normalized=body.model_dump()
    normalized['expected_address']=normalize_address(normalized['expected_address'])
    normalized['lines']=sorted(normalized['lines'],key=lambda x:(x['product_public_id'],x['sku_code']))
    keys=[(x['product_public_id'],x['sku_code']) for x in normalized['lines']]
    if len(set(keys))!=len(keys):raise HTTPException(422,'同一规格不能重复提交')
    digest=hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    key=f'mini:{member.id}:{body.request_id}'
    existing=db.query(Order).filter_by(idempotency_key=key).one_or_none()
    if existing:
        saved=db.get(OrderDeliverySnapshot,existing.id)
        if not saved or saved.request_digest!=digest:raise HTTPException(409,'请求编号已用于不同订单内容')
        response.status_code=200
        return detail(db,existing)
    address=db.query(MemberAddress).filter_by(member_id=member.id,address_public_id=body.address_public_id).one_or_none()
    if address is None:raise HTTPException(404,'收货地址不存在，请重新选择')
    values={key:getattr(address,key) for key in ADDRESS_FIELDS}
    if values!=normalized['expected_address']:raise HTTPException(409,'收货地址已变化，请重新确认')
    lines=[];prices={}
    for line in body.lines:
        sku=db.query(ProductSku).join(Product,Product.id==ProductSku.product_id).filter(
            Product.product_public_id==line.product_public_id,ProductSku.sku_code==line.sku_code).one_or_none()
        if sku is None:raise HTTPException(409,'商品规格已不可用，请刷新购物车')
        lines.append(OrderLineRequest(sku.id,line.quantity));prices[sku.id]=Decimal(line.expected_points_price)
    placed=place_order_with_reservations(db,member_id=member.id,lines=lines,idempotency_key=key)
    for item in db.query(OrderItem).filter_by(order_id=placed.order_id):
        if item.unit_points_price!=prices[item.sku_id]:raise HTTPException(409,'商品积分价已变化，请刷新后重新确认')
    db.add(OrderDeliverySnapshot(order_id=placed.order_id,address_public_id=body.address_public_id,request_digest=digest,**values))
    db.flush()
    result=detail(db,db.get(Order,placed.order_id))
    _commit(db)
    return result

@order_router.get('')
def orders(page:int=Query(1,ge=1,le=1000000),authorization:str|None=Header(None),db:Session=Depends(address_session)):
    member=_require_current_member(authorization,db)
    query=db.query(Order).filter_by(member_id=member.id)
    return {'items':[summary(x) for x in query.order_by(Order.id.desc()).offset((page-1)*20).limit(20)],'total':query.count(),'page':page,'page_size':20}

@order_router.get('/request/{request_id}')
def by_request(request_id:str,authorization:str|None=Header(None),db:Session=Depends(address_session)):
    member=_require_current_member(authorization,db)
    import re
    if not re.fullmatch('[a-f0-9]{32}',request_id):raise HTTPException(422,'请求编号无效')
    order=db.query(Order).filter_by(member_id=member.id,idempotency_key=f'mini:{member.id}:{request_id}').one_or_none()
    if order is None:raise HTTPException(404,'尚未查询到订单，请稍后重试')
    return detail(db,order)

@order_router.get('/{order_public_id}')
def order_detail(order_public_id:str,authorization:str|None=Header(None),db:Session=Depends(address_session)):
    member=_require_current_member(authorization,db)
    return detail(db,owned(db,member.id,order_public_id))

@order_router.post('/{order_public_id}/cancel')
def cancel(order_public_id:str,authorization:str|None=Header(None),db:Session=Depends(address_session)):
    member=_require_current_member(authorization,db)
    order=owned(db,member.id,order_public_id)
    cancel_created_order(db,member_id=member.id,order_public_id=order_public_id)
    result=detail(db,order)
    _commit(db)
    return result
