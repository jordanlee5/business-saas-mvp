"""Measure virtual product files and verify 100 local HTTP downloads; no database."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from pathlib import Path
import random

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.mall.product_media_storage import save_product_image, product_thumbnail_url


def rehearse(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    storage = output / "mall_products"
    # Repeatable synthetic texture, not a real product or typical-photo claim.
    noise = Image.frombytes("RGB", (2400, 1600), random.Random(20261106).randbytes(2400 * 1600 * 3))
    source = Image.blend(Image.new("RGB", noise.size, (150, 175, 160)), noise, .22)
    drawing = ImageDraw.Draw(source)
    drawing.rounded_rectangle((600, 220, 1700, 1400), radius=90, fill=(240, 238, 220), outline=(30, 65, 55), width=25)
    drawing.rectangle((580, 180, 1720, 310), fill=(25, 55, 40))
    stream = BytesIO(); source.save(stream, "JPEG", quality=92)
    (output / "virtual-source.jpg").write_bytes(stream.getvalue())
    main = save_product_image(content=stream.getvalue(), original_filename="virtual.jpg",
                              product_public_id="PRD-VIRTUAL-THUMB", media_role="MAIN", storage_root=storage)
    thumb = product_thumbnail_url(main, storage_root=storage)
    assert main != thumb
    paths = {name: storage / url.removeprefix("/uploads/mall_products/") for name, url in (("main", main), ("thumbnail", thumb))}
    sizes = {name: path.stat().st_size for name, path in paths.items()}
    assert sizes["thumbnail"] < sizes["main"]
    app = FastAPI(); app.mount("/uploads/mall_products", StaticFiles(directory=storage))
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=16) as pool:
            responses = list(pool.map(lambda _: client.get(thumb), range(100)))
        expected = paths["thumbnail"].read_bytes()
        assert all(r.status_code == 200 and r.content == expected for r in responses)
    result = {"status": "PASS", "fixture": "deterministic synthetic textured product",
              "bytes": sizes, "reduction_percent": round(100 * (1 - sizes["thumbnail"] / sizes["main"]), 2),
              "downloads": 100, "parallel_threads": 16,
              "payload_seconds_at_10Mbps_for_100_downloads": {name: round(size * 100 * 8 / 10_000_000, 3) for name, size in sizes.items()},
              "sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
              "limits": "In-process HTTP file checks, no real WAN bandwidth or cloud capacity measured; synthetic compression ratio is not representative of all goods.",
              "database_used": False, "live_wechat_or_carrier_used": False}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    rehearse(parser.parse_args().output)
