"""Admin-only preview/confirm workflow; member APIs are intentionally unchanged."""
import secrets
from urllib.parse import urlencode
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from .. import models as m
from ..mall import client_pricing as service
from .client_organizations import actor

router = APIRouter()
COOKIE = 'client_pricing_csrf'


def csrf(request, token):
    cookie = request.cookies.get(COOKIE, '')
    if len(cookie) != 43 or not secrets.compare_digest(cookie.encode(), token.encode()):
        raise HTTPException(403, '表单已失效，请刷新后重试')


def render(request, db, *, preview=None, message='', error='', status=200):
    from ..main import templates, add_base_context
    token = request.cookies.get(COOKIE, '')
    if len(token) != 43:
        token = secrets.token_urlsafe(32)
    history = db.query(m.ClientPriceRuleEvent).order_by(m.ClientPriceRuleEvent.id.desc()).limit(30).all()
    names = {o.id:o.name for o in db.query(m.ClientOrganization).all()}
    users = {u.id:u.username for u in db.query(m.User).filter(m.User.id.in_([e.actor_id for e in history])).all()}
    response = templates.TemplateResponse(request=request, name='client_pricing.html',
        context=add_base_context(request, dict(request=request, active_page='mall_price_rules',
            page_title='客户价格规则', rows=service.overview(db), preview=preview,
            history=history, names=names, users=users, ratio_label=service.ratio_label,
            csrf=token, message=message[:200], error=error[:200])), status_code=status)
    response.headers['Cache-Control'] = 'no-store, private'
    response.set_cookie(COOKIE, token, httponly=True, samesite='strict', secure=request.url.scheme=='https', path='/mall-price-rules')
    return response


@router.get('/mall-price-rules')
def page(request: Request, message: str='', error: str=''):
    from ..main import SessionLocal
    actor(request, manage=True)
    with SessionLocal() as db:
        return render(request, db, message=message, error=error)


@router.post('/mall-price-rules/preview')
def preview(request: Request, organization_id: int=Form(...), mode: str=Form(...),
            percent: str=Form(''), csrf_token: str=Form('')):
    from ..main import SessionLocal
    user = actor(request, manage=True)
    csrf(request, csrf_token)
    with SessionLocal() as db:
        try:
            result = service.prepare(db, user.id, organization_id, mode, percent)
            db.commit()
            return render(request, db, preview=result)
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            db.rollback()
            message = '预览暂未完成，请刷新后重试' if isinstance(exc, SQLAlchemyError) else str(exc)
            return render(request, db, error=message, status=400)


@router.post('/mall-price-rules/save')
def save(request: Request, preview_token: str=Form(''), confirmed: bool=Form(False), csrf_token: str=Form('')):
    from ..main import SessionLocal
    user = actor(request, manage=True)
    csrf(request, csrf_token)
    if not confirmed:
        raise HTTPException(400, '请先确认预览中的影响范围')
    try:
        with SessionLocal.begin() as db:
            event, changed = service.save(db, user.id, preview_token)
            revision = event.revision
    except (ValueError, PermissionError, SQLAlchemyError) as exc:
        message = '保存结果未确认，请刷新核对；重试同一预览不会重复生效' if isinstance(exc, SQLAlchemyError) else str(exc)
        return RedirectResponse('/mall-price-rules?' + urlencode(dict(error=message)), status_code=303)
    message = (f'第{revision}版规则已保存并立即生效' if changed else f'此预览已保存为第{revision}版，本次未重复修改')
    return RedirectResponse('/mall-price-rules?' + urlencode(dict(message=message)), status_code=303)
