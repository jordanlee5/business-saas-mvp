"""Resolve a voucher's recorded file inside private storage, including old root-level images."""
from pathlib import Path

PRIVATE_HEADERS = {"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "sandbox; default-src 'none'"}
PREVIEW_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def voucher_path(stored):
    if not stored:
        return None
    root = Path("uploads").absolute()
    value = stored.replace("\\", "/")
    if value.startswith("/uploads/"):
        value = value[1:]
    candidate = Path(value)
    try:
        path = candidate.absolute()
        parts = path.relative_to(root).parts
        if any(":" in part or part.endswith((" ", ".")) or part == ".." for part in parts):
            return None
        legacy = len(parts) == 1 and path.suffix.lower() in (*PREVIEW_TYPES, ".pdf")
        if not legacy and not (len(parts) == 2 and parts[0] == "vouchers"):
            return None
        if path.resolve(strict=True) != path or not path.is_file():
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path
