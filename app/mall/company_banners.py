"""Company-only public carousel artwork and versioned operator edits."""
import re
from pathlib import Path
from uuid import uuid4
from sqlalchemy import update

from .audit import MallAuditActionType
from .catalog_service import _require_actor, _record_audit
from .home_media_storage import save_normalized_image
from .product_media_storage import MAX_PRODUCT_IMAGE_BYTES
from ..models import CompanyBanner
from ..time_utils import utc8_now

SLOTS = range(1, 4)
STORAGE_ROOT = Path('uploads/company_banners')
URL_PREFIX = '/uploads/company_banners/'
IMAGE_PATTERN = re.compile(r'[1-3]/[0-9a-f]{32}\.webp')


def image_target(url):
    if not isinstance(url, str) or not url.startswith(URL_PREFIX) or not IMAGE_PATTERN.fullmatch(url[len(URL_PREFIX):]):
        raise ValueError('轮播图片路径无效')
    root = Path(STORAGE_ROOT).resolve()
    target = (root / url[len(URL_PREFIX):]).resolve()
    if not target.is_relative_to(root):
        raise ValueError('轮播图片目录无效')
    return target


def save_image(slot, content, filename):
    if type(slot) is not int or slot not in SLOTS:
        raise ValueError('轮播位置无效')
    url = f'{URL_PREFIX}{slot}/{uuid4().hex}.webp'
    save_normalized_image(content, filename, image_target(url), maximum_size=1920)
    return url


def snapshot(db):
    rows = {row.slot: row for row in db.query(CompanyBanner).all()}
    return [dict(slot=slot, title=rows[slot].title, image_url=rows[slot].image_path,
                 sort_order=rows[slot].sort_order, enabled=rows[slot].enabled,
                 show_copy=rows[slot].show_copy, revision=rows[slot].revision)
            if slot in rows else dict(slot=slot, title=f'轮播素材 {slot}', image_url=None,
                sort_order=slot*10, enabled=False, show_copy=True, revision=0) for slot in SLOTS]


def published(db):
    return [dict(slot=row.slot, title=row.title, image_url=row.image_path, show_copy=row.show_copy)
            for row in db.query(CompanyBanner).filter_by(enabled=True).order_by(CompanyBanner.sort_order, CompanyBanner.slot)]


def change(db, actor_id, slot, revision, *, action, image_url=None, title='', sort_order=0,
           enabled=False, show_copy=True):
    actor = _require_actor(db, actor_admin_id=actor_id,
        action_type=MallAuditActionType.COMPANY_BANNER_UPDATE, permission_message='无首页素材管理权限')
    if actor.must_change_password:
        raise PermissionError('请先修改初始密码')
    if (type(slot) is not int or slot not in SLOTS or type(revision) is not int or not 0 <= revision < 2147483647
            or action not in ('save', 'toggle') or type(enabled) is not bool):
        raise ValueError('轮播位置、版本或操作无效')
    row = db.get(CompanyBanner, slot)
    if (row.revision if row else 0) != revision:
        raise ValueError('该轮播已被其他操作更新，请刷新后再试')
    if action == 'toggle':
        if row is None:
            raise ValueError('请先上传轮播图片')
        values = dict(enabled=enabled)
    else:
        title = title.strip() if isinstance(title, str) else ''
        if not title or len(title) > 80:
            raise ValueError('图片说明须为 1 至 80 个字符')
        if type(sort_order) is not int or not 0 <= sort_order <= 999:
            raise ValueError('排序须为 0 至 999 的整数，数值越小越靠前')
        if type(show_copy) is not bool:
            raise ValueError('文案显示设置无效')
        if image_url is not None:
            image_target(image_url)
            if not image_url.startswith(URL_PREFIX + str(slot) + '/'):
                raise ValueError('图片不属于当前轮播位置')
        selected = image_url or (row.image_path if row else None)
        if selected is None:
            raise ValueError('请先选择轮播图片')
        values = dict(title=title, sort_order=sort_order, enabled=enabled,
                      show_copy=show_copy, image_path=selected)
    now = utc8_now()
    values.update(revision=revision+1, updated_by_id=actor.id, updated_at=now)
    if row is None:
        db.add(CompanyBanner(slot=slot, **values))
        db.flush()  # The primary key also arbitrates simultaneous first uploads.
    else:
        result = db.execute(update(CompanyBanner).where(CompanyBanner.slot == slot, CompanyBanner.revision == revision).values(**values))
        if result.rowcount != 1:
            raise ValueError('该轮播已被其他操作更新，请刷新后再试')
    _record_audit(db, actor=actor, action_type=MallAuditActionType.COMPANY_BANNER_UPDATE,
        target_type='company_banner', target_id=slot, operation_time=now,
        description=f'轮播 {slot}：' + ('保存素材与设置' if action == 'save' else '调整启停') +
                    f'；启用={enabled}；版本 {revision+1}' +
                    (f'；排序={sort_order}；显示文案={show_copy}' if action == 'save' else ''))


DEFAULT_IMAGES = ('city.svg', 'connection.svg', 'delivery.svg')

def public_slides(db):
    slides = []
    for item in published(db):
        try:
            if not image_target(item['image_url']).is_file():
                continue
        except (ValueError, OSError):
            continue
        slides.append(item)
    return slides or [dict(slot=i+1, title='瑞泓开礼企业服务',
        image_url='/static/company/'+name, show_copy=True) for i,name in enumerate(DEFAULT_IMAGES)]
