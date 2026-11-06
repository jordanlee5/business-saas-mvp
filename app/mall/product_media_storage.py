"""商品图片的独立文件校验、转换和安全删除。"""

import re
import warnings
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import ProductMediaRole, normalize_product_media_role


MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PRODUCT_IMAGE_DIMENSION = 8000
MAX_PRODUCT_IMAGE_PIXELS = 30_000_000
OUTPUT_MAX_IMAGE_DIMENSION = 2400
THUMBNAIL_MAX_IMAGE_DIMENSION = 640
PRODUCT_IMAGE_STORAGE_ROOT = Path("uploads/mall_products")
PRODUCT_IMAGE_URL_PREFIX = "/uploads/mall_products"
ALLOWED_PRODUCT_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp"}
)
ALLOWED_PRODUCT_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
SUFFIX_FORMAT_MAP = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
PRODUCT_PUBLIC_ID_PATTERN = re.compile(r"^[A-Z0-9-]{1,32}$")
GENERATED_IMAGE_PATTERN = re.compile(r"^(main|carousel|detail)_[0-9a-f]{32}\.webp$")


def _thumbnail_path(path: Path) -> Path:
    return path.with_name(path.stem + ".thumb-640.webp")


def product_thumbnail_url(image_url: str | None, *, storage_root=None) -> str | None:
    """Use an existing upload-time thumbnail; never encode during a read request."""
    prefix = PRODUCT_IMAGE_URL_PREFIX + "/"
    if not image_url or not image_url.startswith(prefix):
        return image_url
    parts = image_url[len(prefix):].split("/")
    if (len(parts) != 2 or not PRODUCT_PUBLIC_ID_PATTERN.fullmatch(parts[0])
            or not GENERATED_IMAGE_PATTERN.fullmatch(parts[1])):
        return image_url
    root = Path(PRODUCT_IMAGE_STORAGE_ROOT if storage_root is None else storage_root).resolve()
    candidate = _thumbnail_path(root / parts[0] / parts[1]).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return image_url
    return prefix + parts[0] + "/" + candidate.name


def validate_product_image(*, content: bytes, original_filename: str) -> str | None:
    suffix = Path(original_filename or "").suffix.lower()
    if suffix not in ALLOWED_PRODUCT_IMAGE_SUFFIXES:
        return "商品图片只允许 JPG、PNG 或 WebP 格式"
    if not content:
        return "上传的图片文件为空"
    if len(content) > MAX_PRODUCT_IMAGE_BYTES:
        return "商品图片不能超过 5 MB"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                detected_format = image.format
                width, height = image.size
                image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return "上传文件不是有效的图片"

    if detected_format not in ALLOWED_PRODUCT_IMAGE_FORMATS:
        return "上传图片的实际格式不受支持"
    if detected_format != SUFFIX_FORMAT_MAP[suffix]:
        return "图片扩展名与实际图片格式不一致"
    if width < 1 or height < 1:
        return "图片尺寸无效"
    if (
        width > MAX_PRODUCT_IMAGE_DIMENSION
        or height > MAX_PRODUCT_IMAGE_DIMENSION
        or width * height > MAX_PRODUCT_IMAGE_PIXELS
    ):
        return "商品图片尺寸过大"
    return None


def save_product_image(
    *,
    content: bytes,
    original_filename: str,
    product_public_id: str,
    media_role: ProductMediaRole | str,
    storage_root: Path | str = PRODUCT_IMAGE_STORAGE_ROOT,
) -> str:
    """Publish a normalized image and thumbnail, cleaning both on any failure."""
    normalized_product_id = str(product_public_id or "").strip().upper()
    if not PRODUCT_PUBLIC_ID_PATTERN.fullmatch(normalized_product_id):
        raise ValueError("商品公开编号无效")
    role = normalize_product_media_role(media_role)
    error = validate_product_image(
        content=content,
        original_filename=original_filename,
    )
    if error:
        raise ValueError(error)

    root = Path(storage_root).resolve()
    target_directory = (root / normalized_product_id).resolve()
    if not target_directory.is_relative_to(root):
        raise ValueError("商品图片目录无效")
    target_directory.mkdir(parents=True, exist_ok=True)
    filename = f"{role.value.lower()}_{uuid4().hex}.webp"
    final_path = target_directory / filename
    temporary_path = target_directory / f"{filename}.tmp"
    thumbnail_path = _thumbnail_path(final_path)
    thumbnail_temporary_path = thumbnail_path.with_suffix(".webp.tmp")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source_image:
                if getattr(source_image, "is_animated", False):
                    source_image.seek(0)
                prepared_image = ImageOps.exif_transpose(source_image)
                if prepared_image.mode not in {"RGB", "RGBA"}:
                    has_alpha = (
                        "A" in prepared_image.getbands()
                        or "transparency" in prepared_image.info
                    )
                    prepared_image = prepared_image.convert(
                        "RGBA" if has_alpha else "RGB"
                    )
                prepared_image.thumbnail(
                    (OUTPUT_MAX_IMAGE_DIMENSION, OUTPUT_MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                prepared_image.save(
                    temporary_path,
                    format="WEBP",
                    quality=88,
                    method=6,
                )
                thumbnail = prepared_image.copy()
                thumbnail.thumbnail(
                    (THUMBNAIL_MAX_IMAGE_DIMENSION, THUMBNAIL_MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                thumbnail.save(thumbnail_temporary_path, format="WEBP", quality=80, method=4)
        # The database receives the main URL only after both files are published.
        thumbnail_temporary_path.replace(thumbnail_path)
        temporary_path.replace(final_path)
    except Exception:
        for path in (temporary_path, thumbnail_temporary_path, final_path, thumbnail_path):
            if path.exists():
                path.unlink()
        raise

    return (
        f"{PRODUCT_IMAGE_URL_PREFIX}/{normalized_product_id}/{filename}"
    )


def delete_product_image(
    image_url: str | None,
    *,
    storage_root: Path | str = PRODUCT_IMAGE_STORAGE_ROOT,
) -> bool:
    """Delete only a file under the dedicated product-image directory."""
    if not image_url:
        return False
    expected_prefix = f"{PRODUCT_IMAGE_URL_PREFIX}/"
    if not image_url.startswith(expected_prefix):
        return False

    relative_path = image_url[len(expected_prefix):]
    storage_root_path = Path(storage_root).resolve()
    candidate_path = (storage_root_path / relative_path).resolve()
    try:
        candidate_path.relative_to(storage_root_path)
    except ValueError:
        return False
    candidates = [candidate_path]
    if GENERATED_IMAGE_PATTERN.fullmatch(candidate_path.name):
        thumbnail = _thumbnail_path(candidate_path).resolve()
        if thumbnail.is_relative_to(storage_root_path):
            candidates.append(thumbnail)
    deleted = False
    for path in candidates:
        if path.is_file():
            path.unlink()
            deleted = True
    return deleted
