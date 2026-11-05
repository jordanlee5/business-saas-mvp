"""One bounded OCR child: no database imports, no business files or credentials."""
import contextlib
import json
import sys
from PIL import Image

MAX_PIXELS = 16_000_000


def recognize(path):
    with Image.open(path) as image:
        if image.format not in {"PNG", "JPEG"} or image.width * image.height > MAX_PIXELS:
            raise ValueError("Unsupported image")
        image.verify()
    from app.ocr_service import local_ocr_image
    text = local_ocr_image(path, single_thread=True)
    if not text.strip() or len(text) > 65536:
        raise ValueError("Invalid recognition")
    return text


def main():
    try:
        with contextlib.redirect_stdout(sys.stderr):
            text = recognize(sys.argv[1])
        print(json.dumps({"text": text}, ensure_ascii=False))
        return 0
    except Exception:
        # Do not return file contents, library tracebacks or internal paths.
        print('{"error":"unrecognizable"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
