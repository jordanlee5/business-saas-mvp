"""Private member address endpoints with sanitized errors and no-store replies."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import engine
from ..mall.member_address_service import (
    AddressInputError, AddressLimitReached, AddressMemberUnavailable, AddressNotFound,
    create_member_address, delete_member_address, list_member_addresses,
    set_default_member_address, update_member_address,
)
from .miniprogram_v1 import _require_current_member


class PrivateAddressRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                response = await original(request)
            except RequestValidationError:
                # FastAPI's default validation response echoes rejected inputs.
                response = JSONResponse({"detail": "收货地址请求格式不正确"}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except AddressInputError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=422)
            except AddressNotFound:
                response = JSONResponse({"detail": "收货地址不存在"}, status_code=404)
            except AddressLimitReached:
                response = JSONResponse({"detail": "最多保存20条收货地址"}, status_code=409)
            except AddressMemberUnavailable:
                response = JSONResponse({"detail": "会员会话无效"}, status_code=401)
            except SQLAlchemyError:
                # Never expose SQL parameters containing the address or phone.
                response = JSONResponse({"detail": "收货地址服务暂不可用"}, status_code=503)
            response.headers["Cache-Control"] = "no-store, private"
            return response

        return handle


address_router = APIRouter(prefix="/addresses", route_class=PrivateAddressRoute)


class AddressBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipient_name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=7, max_length=16)
    province: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=80)
    district: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=240)


class AddressResponse(AddressBody):
    address_public_id: str
    is_default: bool


class AddressListResponse(BaseModel):
    items: list[AddressResponse]
    total: int
    limit: int = 20


def address_session(request: Request):
    # Begin the SQLite write transaction BEFORE authentication takes a snapshot.
    # Each request gets its own connection, with no process-global member state.
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite" and request.method != "GET":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        try:
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as db:
                yield db
        finally:
            connection.rollback()


def _commit(db):
    # This session is joined to the connection's explicit outer transaction.
    db.flush()
    db.connection().commit()


@address_router.get("", response_model=AddressListResponse)
def get_addresses(authorization: str | None = Header(None), db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    items = list_member_addresses(db, member.id)
    return {"items": items, "total": len(items), "limit": 20}


@address_router.post("", response_model=AddressResponse, status_code=201)
def create_address(body: AddressBody, authorization: str | None = Header(None),
                   db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    result = create_member_address(db, member.id, body.model_dump())
    _commit(db)
    return result


@address_router.put("/{address_public_id}", response_model=AddressResponse)
def update_address(address_public_id: str, body: AddressBody,
                   authorization: str | None = Header(None), db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    result = update_member_address(db, member.id, address_public_id, body.model_dump())
    _commit(db)
    return result


@address_router.put("/{address_public_id}/default", response_model=AddressResponse)
def default_address(address_public_id: str, authorization: str | None = Header(None),
                    db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    result = set_default_member_address(db, member.id, address_public_id)
    _commit(db)
    return result


@address_router.delete("/{address_public_id}", status_code=204)
def delete_address(address_public_id: str, authorization: str | None = Header(None),
                   db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    delete_member_address(db, member.id, address_public_id)
    _commit(db)
    return Response(status_code=204)
