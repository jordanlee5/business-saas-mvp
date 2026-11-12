"""Fixed homepage slots with optimistic edits; never change catalog routing."""
import re
import warnings
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps
from sqlalchemy import update

from .audit import MallAuditActionType
from .catalog_service import _require_actor, _record_audit
from .product_media_storage import validate_product_image, MAX_PRODUCT_IMAGE_BYTES
from ..models import HomeEntryImage
from ..time_utils import utc8_now

ENTRIES = (('home', '居家日用'), ('food', '滋味零食'), ('fresh', '食品生鲜'),
           ('bags', '首饰箱包'), ('digital', '数码家电'), ('beauty', '全球美妆'),
           ('kitchen', '厨房用品'), ('kids', '母婴儿童'))
KEYS = dict(ENTRIES)
STORAGE_ROOT = Path('uploads/mall_home')
URL_PREFIX = '/uploads/mall_home/'
IMAGE_PATTERN = re.compile(r'(home|food|fresh|bags|digital|beauty|kitchen|kids)/[0-9a-f]{32}\.webp')


def image_target(url):
    if not isinstance(url, str) or not url.startswith(URL_PREFIX):
        raise ValueError('首页图片路径无效')
    relative = url[len(URL_PREFIX):]
    if not IMAGE_PATTERN.fullmatch(relative):
        raise ValueError('首页图片路径无效')
    root = Path(STORAGE_ROOT).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('首页图片目录无效')
    return target


def save_image(key, content, filename):
    if key not in KEYS:
        raise ValueError('首页入口不存在')
    error = validate_product_image(content=content, original_filename=filename)
    if error:
        raise ValueError(error.replace('商品图片', '图片'))
    url = f'{URL_PREFIX}{key}/{uuid4().hex}.webp'
    target = image_target(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                source.seek(0)
                prepared = ImageOps.exif_transpose(source).convert('RGBA')
                prepared.thumbnail((640, 640), Image.Resampling.LANCZOS)
                # Encode pixels only: no source filename, EXIF, profile or other metadata.
                pixels = Image.frombytes('RGBA', prepared.size, prepared.tobytes())
                pixels.save(temporary, format='WEBP', quality=88, method=4)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    return url


def snapshot(db):
    rows = {row.entry_key: row for row in db.query(HomeEntryImage).all()}
    return [dict(key=key, name=name, image_url=rows[key].image_path if key in rows else None,
                 revision=rows[key].revision if key in rows else 0) for key, name in ENTRIES]


def set_image(db, actor_id, key, revision, image_url):
    actor = _require_actor(db, actor_admin_id=actor_id,
        action_type=MallAuditActionType.HOME_ENTRY_IMAGE_UPDATE, permission_message='无首页素材管理权限')
    if actor.must_change_password:
        raise PermissionError('请先修改初始密码')
    if key not in KEYS or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError('首页入口或版本无效')
    if image_url is not None:
        image_target(image_url)
        if not image_url.startswith(URL_PREFIX + key + '/'):
            raise ValueError('图片不属于当前首页入口')
    now = utc8_now()
    if revision == 0:
        row = HomeEntryImage(entry_key=key, image_path=image_url, revision=1,
                             updated_by_id=actor.id, updated_at=now)
        db.add(row)
        db.flush()  # Unique key also rejects two simultaneous first uploads.
    else:
        result = db.execute(update(HomeEntryImage).where(HomeEntryImage.entry_key == key,
            HomeEntryImage.revision == revision).values(image_path=image_url, revision=revision+1,
            updated_by_id=actor.id, updated_at=now))
        if result.rowcount != 1:
            raise ValueError('该入口已被其他操作更新，请刷新后再试')
        row = db.query(HomeEntryImage).filter_by(entry_key=key).one()
    _record_audit(db, actor=actor, action_type=MallAuditActionType.HOME_ENTRY_IMAGE_UPDATE,
        target_type='home_entry_image', target_id=row.id, operation_time=now,
        description=f'{KEYS[key]}：' + ('替换图片' if image_url else '恢复默认插画') + f'；版本 {revision + 1}')
