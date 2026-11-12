"""Homepage management and member-gated image delivery."""
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .catalog_access import CatalogAccessRoute, PRIVATE_HEADERS, require_catalog_member
from .miniprogram_v1 import public_catalog_session, release_owned_session
from ..admin_permissions import can_manage_mall_catalog
from ..mall import home_entries as service
from ..models import HomeEntryImage

router = APIRouter(route_class=CatalogAccessRoute)
CSRF_COOKIE = 'mall_home_csrf'


def admin(request):
    from ..main import get_current_user
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, '请先登录后台')
    if not user.is_active or user.must_change_password or not can_manage_mall_catalog(user):
        raise HTTPException(403, '无首页素材管理权限')
    return user


@router.get('/mall-home')
def home_page(request: Request, message: str = '', error: str = ''):
    from ..main import SessionLocal, templates, add_base_context
    admin(request)
    csrf = request.cookies.get(CSRF_COOKIE, '')
    if len(csrf) != 43:
        csrf = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        entries = service.snapshot(db)
    response = templates.TemplateResponse(request=request, name='mall_home.html', context=add_base_context(request,
        dict(request=request, page_title='小程序首页素材', active_page='mall_home', entries=entries,
             csrf=csrf, message=message[:200], error=error[:200])))
    response.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite='strict',
                        secure=request.url.scheme == 'https', path='/mall-home')
    return response


@router.post('/mall-home/{key}')
def edit_entry(request: Request, key: str, revision: int = Form(...), csrf_token: str = Form(''),
               action: str = Form(...), image_file: UploadFile | None = File(None)):
    from ..main import SessionLocal
    saved = None
    try:
        user = admin(request)
        cookie = request.cookies.get(CSRF_COOKIE, '')
        if len(cookie) != 43 or not secrets.compare_digest(cookie.encode(), csrf_token.encode()):
            raise HTTPException(403, '表单已失效，请刷新后重试')
        if key not in service.KEYS or action not in ('upload', 'reset') or revision < 0:
            raise HTTPException(400, '请选择有效入口与操作')
        try:
            if action == 'upload':
                if image_file is None:
                    raise ValueError('请先选择图片')
                saved = service.save_image(key, image_file.file.read(service.MAX_PRODUCT_IMAGE_BYTES + 1),
                                           image_file.filename or '')
            with SessionLocal.begin() as db:
                service.set_image(db, user.id, key, revision, saved)
        except (ValueError, PermissionError, OSError, SQLAlchemyError) as exc:
            # A connection failure while COMMIT is acknowledged can be ambiguous.
            # Keep that file: the database may already reference it. Only known
            # validation/conflict failures can safely remove a new upload.
            if saved and (not isinstance(exc, SQLAlchemyError) or isinstance(exc, IntegrityError)):
                try:
                    service.image_target(saved).unlink(missing_ok=True)
                except OSError:
                    pass  # Unreferenced files are inaccessible and retained for inspection.
            if isinstance(exc, IntegrityError):
                error = '该入口已被其他操作更新，请刷新后再试'
            elif isinstance(exc, (OSError, SQLAlchemyError)):
                error = '素材保存结果未确认，请刷新核对后再试'
            else:
                error = str(exc)
            return RedirectResponse('/mall-home?' + urlencode({'error': error}), status_code=303)
        # Previous files remain in the backup scope, but cannot be read through this API.
        message = service.KEYS[key] + ('图片已更新' if saved else '已恢复默认插画')
        return RedirectResponse('/mall-home?' + urlencode({'message': message}), status_code=303)
    finally:
        if image_file is not None:
            image_file.file.close()


@router.get('/api/miniprogram/v1/home-entries')
@release_owned_session
def get_entries(authorization: str | None = Header(None), db=Depends(public_catalog_session)):
    require_catalog_member(authorization, db)
    return [{k: v for k, v in row.items() if k != 'revision'} for row in service.snapshot(db)]


@router.api_route('/uploads/mall_home/{image_path:path}', methods=['GET', 'HEAD'])
@release_owned_session
def get_image(request: Request, image_path: str, authorization: str | None = Header(None),
              db=Depends(public_catalog_session)):
    if authorization is not None:
        require_catalog_member(authorization, db)
    else:
        admin(request)
    url = service.URL_PREFIX + image_path
    try:
        target = service.image_target(url)
    except ValueError:
        raise HTTPException(404, '图片不存在') from None
    if db.query(HomeEntryImage.id).filter_by(image_path=url).first() is None or not target.is_file():
        raise HTTPException(404, '图片不存在')
    return FileResponse(target, media_type='image/webp', headers=PRIVATE_HEADERS)
