"""Shared pixel-only encoding for homepage artwork; callers constrain storage paths."""
import warnings
from io import BytesIO
from PIL import Image, ImageOps
from .product_media_storage import validate_product_image


def save_normalized_image(content, filename, target, *, maximum_size):
    error = validate_product_image(content=content, original_filename=filename)
    if error:
        raise ValueError(error.replace('商品图片', '图片'))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                source.seek(0)
                prepared = ImageOps.exif_transpose(source).convert('RGBA')
                prepared.thumbnail((maximum_size, maximum_size), Image.Resampling.LANCZOS)
                pixels = Image.frombytes('RGBA', prepared.size, prepared.tobytes())
                pixels.save(temporary, format='WEBP', quality=88, method=4)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
