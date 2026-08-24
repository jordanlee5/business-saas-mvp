import warnings
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import (
    Image,
    ImageOps,
    UnidentifiedImageError,
)


MAX_PROMOTION_IMAGE_BYTES = (
    5 * 1024 * 1024
)

MAX_PROMOTION_IMAGE_DIMENSION = 8000
MAX_PROMOTION_IMAGE_PIXELS = 30_000_000
OUTPUT_MAX_IMAGE_DIMENSION = 2400

PROMOTION_IMAGE_STORAGE_ROOT = Path(
    "uploads/promotion_pages"
)

PROMOTION_IMAGE_URL_PREFIX = (
    "/uploads/promotion_pages"
)

ALLOWED_PROMOTION_IMAGE_ROLES = frozenset(
    {
        "logo",
        "hero",
        "content",
    }
)

ALLOWED_PROMOTION_IMAGE_SUFFIXES = (
    frozenset(
        {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }
    )
)

ALLOWED_PROMOTION_IMAGE_FORMATS = (
    frozenset(
        {
            "JPEG",
            "PNG",
            "WEBP",
        }
    )
)

SUFFIX_FORMAT_MAP = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}


def validate_promotion_image(
    *,
    content: bytes,
    original_filename: str,
) -> str | None:
    suffix = Path(
        original_filename or ""
    ).suffix.lower()

    if (
        suffix
        not in ALLOWED_PROMOTION_IMAGE_SUFFIXES
    ):
        return (
            "宣传页图片只允许 JPG、PNG "
            "或 WebP 格式"
        )

    if not content:
        return "上传的图片文件为空"

    if (
        len(content)
        > MAX_PROMOTION_IMAGE_BYTES
    ):
        return "宣传页图片不能超过 5 MB"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )

            with Image.open(
                BytesIO(content)
            ) as image:
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

    if (
        detected_format
        not in ALLOWED_PROMOTION_IMAGE_FORMATS
    ):
        return "上传图片的实际格式不受支持"

    expected_format = SUFFIX_FORMAT_MAP[
        suffix
    ]

    if detected_format != expected_format:
        return (
            "图片扩展名与实际图片格式不一致"
        )

    if width < 1 or height < 1:
        return "图片尺寸无效"

    if (
        width > MAX_PROMOTION_IMAGE_DIMENSION
        or height
        > MAX_PROMOTION_IMAGE_DIMENSION
        or width * height
        > MAX_PROMOTION_IMAGE_PIXELS
    ):
        return "宣传页图片尺寸过大"

    return None


def save_promotion_image(
    *,
    content: bytes,
    original_filename: str,
    promotion_page_id: int,
    image_role: str,
    storage_root: Path | str = (
        PROMOTION_IMAGE_STORAGE_ROOT
    ),
) -> str:
    if promotion_page_id < 1:
        raise ValueError(
            "宣传页 ID 无效"
        )

    if (
        image_role
        not in ALLOWED_PROMOTION_IMAGE_ROLES
    ):
        raise ValueError(
            "宣传页图片类型无效"
        )

    error = validate_promotion_image(
        content=content,
        original_filename=(
            original_filename
        ),
    )

    if error:
        raise ValueError(error)

    storage_root_path = Path(
        storage_root
    )

    target_directory = (
        storage_root_path
        / str(promotion_page_id)
    )

    target_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = (
        f"{image_role}_"
        f"{uuid4().hex}.webp"
    )

    final_path = (
        target_directory
        / filename
    )

    temporary_path = (
        target_directory
        / f"{filename}.tmp"
    )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )

            with Image.open(
                BytesIO(content)
            ) as source_image:
                if getattr(
                    source_image,
                    "is_animated",
                    False,
                ):
                    source_image.seek(0)

                prepared_image = (
                    ImageOps.exif_transpose(
                        source_image
                    )
                )

                if prepared_image.mode not in {
                    "RGB",
                    "RGBA",
                }:
                    has_alpha = (
                        "A"
                        in prepared_image.getbands()
                        or "transparency"
                        in prepared_image.info
                    )

                    prepared_image = (
                        prepared_image.convert(
                            (
                                "RGBA"
                                if has_alpha
                                else "RGB"
                            )
                        )
                    )

                prepared_image.thumbnail(
                    (
                        OUTPUT_MAX_IMAGE_DIMENSION,
                        OUTPUT_MAX_IMAGE_DIMENSION,
                    ),
                    Image.Resampling.LANCZOS,
                )

                prepared_image.save(
                    temporary_path,
                    format="WEBP",
                    quality=88,
                    method=6,
                )

        temporary_path.replace(
            final_path
        )

    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()

        raise

    return (
        f"{PROMOTION_IMAGE_URL_PREFIX}/"
        f"{promotion_page_id}/"
        f"{filename}"
    )


def delete_promotion_image(
    image_url: str | None,
    *,
    storage_root: Path | str = (
        PROMOTION_IMAGE_STORAGE_ROOT
    ),
) -> bool:
    if not image_url:
        return False

    expected_prefix = (
        f"{PROMOTION_IMAGE_URL_PREFIX}/"
    )

    if not image_url.startswith(
        expected_prefix
    ):
        return False

    relative_path = image_url[
        len(expected_prefix):
    ]

    storage_root_path = Path(
        storage_root
    ).resolve()

    candidate_path = (
        storage_root_path
        / relative_path
    ).resolve()

    try:
        candidate_path.relative_to(
            storage_root_path
        )
    except ValueError:
        return False

    if not candidate_path.is_file():
        return False

    candidate_path.unlink()

    return True