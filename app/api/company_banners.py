"""Operator-only edits; anonymous delivery of explicitly enabled company artwork."""
import secrets
from urllib.parse import urlencode
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .home_entries import admin
CSRF_COOKIE = "company_banner_csrf"
from ..mall import company_banners as service
from ..models import CompanyBanner

router = APIRouter()


@router.post('/company-materials/banners/{slot}')
def edit_banner(request: Request, slot: int, revision: int = Form(...), csrf_token: str = Form(''),
                action: str = Form('save'), title: str = Form(''), sort_order: int = Form(0),
                enabled: bool = Form(False), show_copy: bool = Form(False), image_file: UploadFile | None = File(None)):
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
                    title=title, sort_order=sort_order, enabled=enabled, show_copy=show_copy)
        except (ValueError, PermissionError, OSError, SQLAlchemyError) as exc:
            # Retain files on ambiguous commit acknowledgement; a successful commit may reference them.
            if saved and (not isinstance(exc, SQLAlchemyError) or isinstance(exc, IntegrityError)):
                try:
                    service.image_target(saved).unlink(missing_ok=True)
                except OSError:
                    pass
            error = ('该轮播已被其他操作更新，请刷新后再试' if isinstance(exc, IntegrityError) else
                     '保存结果未确认，请刷新核对后再试' if isinstance(exc, (OSError, SQLAlchemyError)) else str(exc))
            return RedirectResponse('/company-materials?' + urlencode(dict(error=error)), status_code=303)
        message = f'轮播 {slot} 已保存，' + ('当前已启用' if enabled else '当前已停用')
        return RedirectResponse('/company-materials?' + urlencode(dict(message=message)), status_code=303)
    finally:
        if image_file is not None:
            image_file.file.close()



@router.get('/company-materials')
def editor(request: Request, message: str = '', error: str = ''):
    from ..main import SessionLocal, templates, add_base_context
    admin(request)
    csrf = request.cookies.get(CSRF_COOKIE, '')
    if len(csrf) != 43:
        csrf = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        banners = service.snapshot(db)
        preview = service.published(db)
    response = templates.TemplateResponse(request=request, name='company_materials.html', context=add_base_context(request,
        dict(request=request, page_title='企业宣传页素材', active_page='company_materials', banners=banners,
             banner_preview=preview, csrf=csrf, message=message[:200], error=error[:200])))
    response.headers['Cache-Control'] = 'no-store, private'
    response.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite='strict', secure=request.url.scheme=='https', path='/company-materials')
    return response


@router.api_route('/uploads/company_banners/{image_path:path}', methods=['GET', 'HEAD'])
def image(request: Request, image_path: str):
    from ..main import SessionLocal
    url = service.URL_PREFIX + image_path
    try:
        target = service.image_target(url)
    except ValueError:
        raise HTTPException(404, '图片不存在') from None
    with SessionLocal() as db:
        row = db.query(CompanyBanner).filter_by(image_path=url).first()
        if row is None or not target.is_file():
            raise HTTPException(404, '图片不存在')
        if not row.enabled:
            try:
                admin(request)
            except HTTPException:
                raise HTTPException(404, '图片不存在') from None
    return FileResponse(target, media_type='image/webp', headers={'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})


@router.get('/promotion-pages', name='promotion_pages_page')
@router.get('/promotion-pages/{legacy_path:path}')
def legacy_editor_entry(request: Request):
    """Old bookmarks lead to the sole current editor without reading legacy content."""
    admin(request)
    return RedirectResponse('/company-materials', status_code=303,
                            headers={'Cache-Control': 'no-store, private'})


@router.post('/promotion-pages')
@router.post('/promotion-pages/{legacy_path:path}')
def retired_editor_submission(request: Request):
    """Reject stale forms without parsing uploads or mutating either set of content."""
    admin(request)
    raise HTTPException(410, '旧宣传页配置已停用，请进入企业宣传页素材管理',
                        headers={'Cache-Control': 'no-store, private'})
