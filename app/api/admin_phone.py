"""Reserved login endpoints. Runtime remains unavailable until a reviewed integration exists."""
import secrets
import re
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from .. import admin_phone_login as domain
from ..browser_sessions import COOKIE_NAME, LIFETIME
from ..shared_browser_sessions import SessionStoreUnavailable

CSRF_COOKIE = 'admin_phone_csrf'
PRIVATE = {'Cache-Control':'no-store, private','Vary':'Cookie','X-Content-Type-Options':'nosniff'}


class PhoneRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse({'detail':'请求格式不正确，请刷新后重试。'},status_code=400)
            except domain.PhoneRateLimited as exc:
                response = JSONResponse({'detail':'操作频繁，请稍后重试。'},status_code=429,
                                        headers={'Retry-After':str(exc.retry_after)})
            except domain.HumanRejected:
                response = JSONResponse({'detail':'请重新完成人机验证。'},status_code=400)
            except domain.PhoneRejected:
                response = JSONResponse({'detail':'验证码无效或已失效，请核对或重新获取。'},status_code=401)
            except (domain.PhoneUnavailable, SQLAlchemyError, SessionStoreUnavailable):
                response = JSONResponse({'detail':'手机号登录暂不可用，请使用账号密码登录。'},status_code=503)
            except HTTPException as exc:
                response = JSONResponse({'detail':exc.detail},status_code=exc.status_code)
            response.headers.update(PRIVATE)
            return response
        return handle


router = APIRouter(route_class=PhoneRoute)


def get_phone_service():
    # No environment flags, config files, real SMS SDKs or test backdoors are wired here.
    # Isolated test apps override this dependency with their synthetic service.
    return None


def required_service(service=Depends(get_phone_service)):
    if service is None:
        raise domain.PhoneUnavailable()
    return service


def browser_nonce(request):
    cookie = request.cookies.get(CSRF_COOKIE,'')
    header = request.headers.get('X-Phone-CSRF','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',cookie) or not secrets.compare_digest(cookie.encode(),header.encode()):
        raise HTTPException(403,'页面已失效，请刷新登录页后重试。')
    return cookie


class SendBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phone: str = Field(max_length=32)
    captcha_ticket: str = Field(max_length=4096)
    captcha_randstr: str = Field(max_length=256)


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    challenge_id: str = Field(max_length=64)
    code: str = Field(max_length=8)


@router.post('/login/phone/request-code')
def request_code(request: Request, body: SendBody, service=Depends(required_service)):
    nonce = browser_nonce(request)
    return service.request_code(body.phone,nonce,request.client.host if request.client else '',
                                body.captcha_ticket,body.captcha_randstr)


@router.post('/login/phone/verify')
def verify_code(request: Request, body: VerifyBody, service=Depends(required_service)):
    nonce = browser_nonce(request)
    grant = service.verify_code(body.challenge_id,body.code,nonce,request.client.host if request.client else '',
                                 previous_session=request.cookies.get(COOKIE_NAME))
    response = JSONResponse({'redirect':'/change-password' if grant.must_change_password else '/dashboard'})
    response.delete_cookie('user_id')
    response.set_cookie(COOKIE_NAME,grant.token,max_age=LIFETIME,httponly=True,samesite='lax',
                        secure=request.url.scheme=='https')
    return response
