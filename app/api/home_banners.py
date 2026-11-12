"""Versioned carousel edits and member-gated publication."""
import secrets
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .catalog_access import CatalogAccessRoute, PRIVATE_HEADERS, require_catalog_member
from .home_entries import admin, CSRF_COOKIE
from .miniprogram_v1 import public_catalog_session, release_owned_session
from ..mall import home_banners as service
from ..models import HomeBanner

router = APIRouter(route_class=CatalogAccessRoute)


@router.post('/mall-home/banners/{slot}')
def edit_banner(request: Request, slot: int, revision: int = Form(...), csrf_token: str = Form(''),
                action: str = Form('save'), title: str = Form(''), sort_order: int = Form(0),
                enabled: bool = Form(False), target_category: str = Form(''), image_file: UploadFile | None = File(None)):
    from ..main import SessionLocal
    saved = None
    try:
        user = admin(request)
        cookie = request.cookies.get(CSRF_COOKIE, '')
        if len(cookie) != 43 or not secrets.compare_digest(cookie.encode(), csrf_token.encode()):
            raise HTTPException(403, '表单已失效，请刷新后重试')
        if slot not in service.SLOTS or action not in ('save', 'toggle') or not 0 <= revision < 2147483647:
            raise HTTPException(400, '轮播位置、版本或操作无效')
        try:
            if image_file is not None and image_file.filename:
                if action != 'save':
                    raise ValueError('启停操作不接受新图片')
                saved = service.save_image(slot, image_file.file.read(service.MAX_PRODUCT_IMAGE_BYTES+1), image_file.filename)
            with SessionLocal.begin() as db:
                service.change(db, user.id, slot, revision, action=action, image_url=saved,
                    title=title, sort_order=sort_order, enabled=enabled, target_category=target_category)
        except (ValueError, PermissionError, OSError, SQLAlchemyError) as exc:
            # Retain files on ambiguous commit acknowledgement; a successful commit may reference them.
            if saved and (not isinstance(exc, SQLAlchemyError) or isinstance(exc, IntegrityError)):
                try:
                    service.image_target(saved).unlink(missing_ok=True)
                except OSError:
                    pass
            error = ('该轮播已被其他操作更新，请刷新后再试' if isinstance(exc, IntegrityError) else
                     '保存结果未确认，请刷新核对后再试' if isinstance(exc, (OSError, SQLAlchemyError)) else str(exc))
            return RedirectResponse('/mall-home?' + urlencode(dict(section='carousel', error=error)), status_code=303)
        message = f'轮播 {slot} 已保存，' + ('当前已启用' if enabled else '当前已停用')
        return RedirectResponse('/mall-home?' + urlencode(dict(section='carousel', message=message)), status_code=303)
    finally:
        if image_file is not None:
            image_file.file.close()


@router.get('/api/miniprogram/v1/home-banners')
@release_owned_session
def get_banners(authorization: str | None = Header(None), db=Depends(public_catalog_session)):
    require_catalog_member(authorization, db)
    return service.published(db)


@router.api_route('/uploads/mall_banners/{image_path:path}', methods=['GET', 'HEAD'])
@release_owned_session
def get_image(request: Request, image_path: str, authorization: str | None = Header(None), db=Depends(public_catalog_session)):
    member = authorization is not None
    if member:
        require_catalog_member(authorization, db)
    else:
        admin(request)
    url = service.URL_PREFIX + image_path
    try:
        target = service.image_target(url)
    except ValueError:
        raise HTTPException(404, '图片不存在') from None
    query = db.query(HomeBanner.slot).filter_by(image_path=url)
    if member:
        query = query.filter_by(enabled=True)
    if query.first() is None or not target.is_file():
        raise HTTPException(404, '图片不存在')
    return FileResponse(target, media_type='image/webp', headers=PRIVATE_HEADERS)
