"""Create or serve a persistent, loopback-only shop for joint product image acceptance."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
PORT = 8174
PRODUCT = "PRD-IMAGE-REVIEW"
ACCOUNT = "image-review"
MARKER = "image-review-v1.json"


def isolate(directory):
    os.chdir(directory)
    for key in ("MALL_FULFILLMENT_SETTINGS", "WECHAT_MINIPROGRAM_APP_ID", "WECHAT_MINIPROGRAM_APP_SECRET",
                "MINIPROGRAM_SESSION_SIGNING_KEY", "POSTGRES_TEST_DATABASE_URL", "POSTGRES_TEST_ALLOW_RESET"):
        os.environ.pop(key, None)
    os.environ["DATABASE_URL"] = "sqlite:///" + (directory / "review.db").as_posix()


def initialize(directory):
    # Require a new directory so reruns cannot replace uploads, a database or someone's DevTools project.
    directory.mkdir(parents=True, exist_ok=False)
    isolate(directory)
    from alembic import command
    from alembic.config import Config
    from app.auth import get_password_hash
    from app.admin_permissions import OPERATOR
    from app.database import SessionLocal, engine
    from app.models import InventoryBalance, Product, ProductCategory, ProductSku, Supplier, User
    from tools.build_ui_demo import png

    for name in ("templates", "static"):
        shutil.copytree(ROOT / "app" / name, directory / "app" / name)
    mini = directory / "mini"
    mini.mkdir()
    shutil.copytree(ROOT / "miniprogram/src", mini / "src")
    for name in ("tsconfig.json", "project.config.json"):
        shutil.copyfile(ROOT / "miniprogram" / name, mini / name)
    config = mini / "src/config.ts"
    source = config.read_text("utf8")
    if source.count("http://127.0.0.1:8000") != 1:
        raise RuntimeError("开发工具本机接口已变更，请核对验收入口")
    config.write_text(source.replace("http://127.0.0.1:8000", f"http://127.0.0.1:{PORT}"), encoding="utf8")
    (mini / "project.private.config.json").write_text(json.dumps({
        "projectname": "积分商城-本机商品图联验", "setting": {"urlCheck": False}
    }, ensure_ascii=False), encoding="utf8")
    password = secrets.token_urlsafe(16)
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        with SessionLocal.begin() as db:
            db.add(User(username=ACCOUNT, password_hash=get_password_hash(password), role="admin",
                        admin_level=OPERATOR, is_active=True, must_change_password=False))
            category = ProductCategory(name="居家日用", slug="home")
            supplier = Supplier(name="虚拟杯具供应商", supplier_public_id="SUP-IMAGE-REVIEW")
            db.add_all([category, supplier])
            db.flush()
            product = Product(product_public_id=PRODUCT, name="联验随行杯", category_id=category.id,
                              subtitle="两个容量，共用商品图片", status="PUBLISHED", published_at=datetime.now())
            db.add(product)
            db.flush()
            for volume, price in [(350, 30), (500, 45)]:
                sku = ProductSku(product_id=product.id, supplier_id=supplier.id, sku_code=f"CUP-{volume}",
                                 name=f"{volume} mL", points_price=price, cost_price=10)
                db.add(sku)
                db.flush()
                db.add(InventoryBalance(sku_id=sku.id, on_hand_quantity=20, reserved_quantity=0))
    finally:
        engine.dispose()
    for index, name in enumerate(["01-main", "02-carousel", "03-detail"]):
        png(directory / "samples" / (name + ".png"), index)
    with open(directory / "登录信息.txt", "x", encoding="utf-8-sig",
              opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
        out.write(f"仅限本机虚拟验收\n账号 {ACCOUNT}\n密码 {password}\n")
    (directory / MARKER).write_text(json.dumps({"product": PRODUCT, "port": PORT}), encoding="utf8")


def serve(directory):
    if json.loads((directory / MARKER).read_text("utf8")) != {"product": PRODUCT, "port": PORT}:
        raise RuntimeError("请选择本工具创建的验收目录")
    if not (directory / "review.db").is_file():
        raise RuntimeError("缺少隔离验收数据库")
    isolate(directory)
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, access_log=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "serve"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if directory.is_relative_to(ROOT):
        parser.error("请选择仓库外的独立验收目录")
    sys.path.insert(0, str(ROOT))
    if args.action == "init":
        initialize(directory)
    else:
        serve(directory)
