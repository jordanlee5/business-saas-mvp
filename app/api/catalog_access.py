"""Request-time membership checks for catalog and product media, never history."""
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from ..models import MemberQualification
from ..mall.qualification_service import db_time
from ..time_utils import utc8_now

PRIVATE_HEADERS = {"Cache-Control": "no-store, private", "Vary": "Authorization, Cookie",
                   "X-Content-Type-Options": "nosniff"}


class CatalogAccessRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse({"detail": "商品请求格式不正确"}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except (SQLAlchemyError, RuntimeError):
                response = JSONResponse({"detail": "商品服务暂不可用，请稍后重试"}, status_code=503)
            response.headers.update(PRIVATE_HEADERS)
            return response

        return handle


def require_qualification(db, member_id, *, lock=False):
    """Checkout holds eligible rows until commit, serializing explicit revocation.

    Read paths use fresh SQL on every request, not token claims or process caches.
    Callers must already authenticate the active member; this is not identity auth.
    """
    now = db_time(db, utc8_now())
    query = db.query(MemberQualification.id).filter(
        MemberQualification.member_id == member_id, MemberQualification.enabled.is_(True),
        MemberQualification.valid_from <= now, MemberQualification.valid_until > now,
    ).order_by(MemberQualification.id)
    if lock:
        query = query.with_for_update()
    if not query.all():
        raise HTTPException(403, "暂无有效商城资格，请联系服务方核对；仍可查询本人的历史订单和权益申请")


def require_catalog_member(authorization, db):
    from .miniprogram_v1 import _require_current_member
    if not isinstance(authorization, str):
        raise HTTPException(401, "请先登录并核验会员资格")
    member = _require_current_member(authorization, db)
    require_qualification(db, member.id)
    return member
