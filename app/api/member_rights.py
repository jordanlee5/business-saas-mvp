from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from .member_addresses import address_session, _commit, with_member_session
from .miniprogram_v1 import _require_current_member
from ..mall import rights_service as rights
from ..models import MemberRightsRequest


class PrivateRightsRoute(APIRoute):
    def get_route_handler(self):
        route = super().get_route_handler()
        async def handle(request):
            try:
                response = await route(request)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({"detail": "申请格式不正确"}, status_code=422)
            except SQLAlchemyError:
                response = JSONResponse({"detail": "申请服务暂不可用"}, status_code=503)
            response.headers["Cache-Control"] = "no-store, private"
            return response
        return handle


router = APIRouter(prefix="/rights-requests", route_class=PrivateRightsRoute)


class Application(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: str = Field(max_length=20)
    description: str = Field(min_length=1, max_length=800)


@router.post("", status_code=201)
@with_member_session
def apply(body: Application, response: Response, authorization: str | None = Header(None), db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    try:
        case, created = rights.submit(db, member.id, body.request_key, body.kind, body.description)
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    result = rights.summary(case)
    _commit(db)
    response.status_code = 201 if created else 200
    return result


@router.get("")
@with_member_session
def mine(page: int = 1, authorization: str | None = Header(None), db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    if not 1 <= page <= 1000000:
        raise HTTPException(422, "页码无效")
    query = db.query(MemberRightsRequest).filter_by(member_id=member.id)
    rows = query.order_by(MemberRightsRequest.id.desc()).offset((page-1)*rights.PAGE_SIZE).limit(rights.PAGE_SIZE)
    return dict(items=[rights.summary(row) for row in rows], total=query.count())


@router.get("/{public_id}")
@with_member_session
def detail(public_id: str, authorization: str | None = Header(None), db=Depends(address_session)):
    member = _require_current_member(authorization, db)
    case = db.query(MemberRightsRequest).filter_by(member_id=member.id, public_id=public_id).one_or_none()
    if not case:
        raise HTTPException(404, "申请不存在")
    return rights.member_detail(db, case)
