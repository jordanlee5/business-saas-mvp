"""Cold-backup rehearsal using only a newly created synthetic SQLite store and local HTTP."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

import httpx
from alembic import command
from alembic.config import Config
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def inventory(root):
    files = [root / "review.db", *(p for p in (root / "uploads").rglob("*") if p.is_file())]
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def seed(root, password):
    from app.admin_permissions import PRIMARY_REVIEWER
    from app.auth import get_password_hash
    from app.database import create_database_engine
    from app.models import BusinessRecord, MatchReview, User, VoucherRecord
    from sqlalchemy.orm import Session

    url = "sqlite:///" + (root / "review.db").as_posix()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    for name in ("mall_products/PRD-REVIEW", "promotion_pages", "vouchers", "excel"):
        (root / "uploads" / name).mkdir(parents=True)
    Image.new("RGB",(20,20),"green").save(root/"uploads/mall_products/PRD-REVIEW/main.webp")
    Image.new("RGB",(20,20),"blue").save(root/"uploads/vouchers/proof.png")
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.append(["虚拟导入记录"])
    workbook.save(root/"uploads/excel/source.xlsx")
    engine = create_database_engine(url)
    try:
        with Session(engine) as db:
            for name in ("reviewer", "alice", "bob"):
                admin = name == "reviewer"
                db.add(User(username=name, password_hash=get_password_hash(password), is_active=True,
                            role="admin" if admin else "partner", admin_level=PRIMARY_REVIEWER if admin else None))
            db.flush()
            owner=db.query(User).filter_by(username="alice").one()
            record=BusinessRecord(user_id=owner.id, name="虚拟业务", points_amount=10)
            proof=VoucherRecord(filename="proof.png", file_path="uploads/vouchers/proof.png", voucher_amount=10)
            db.add_all([record,proof])
            db.flush()
            review=MatchReview(business_record_id=record.id,voucher_id=proof.id,review_status="已通过",allocation_amount=10)
            db.add(review)
            db.flush()
            ids=(record.id,review.id)
            db.commit()
            return ids
    finally:
        engine.dispose()


@contextmanager
def service(root):
    # Start each service in its own working directory, with its own database and uploads.
    shutil.copytree(ROOT/"app/templates",root/"app/templates")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    origin=f"http://127.0.0.1:{port}"
    env=dict(os.environ, DATABASE_URL="sqlite:///"+(root/"review.db").as_posix(), PYTHONPATH=str(ROOT))
    for name in ("MALL_FULFILLMENT_SETTINGS","WECHAT_MINIPROGRAM_APP_SECRET","MINIPROGRAM_SESSION_SIGNING_KEY"):
        env.pop(name,None)
    server=subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--host","127.0.0.1","--port",str(port),
                             "--no-access-log"],cwd=root,env=env)
    try:
        with httpx.Client(base_url=origin,trust_env=False) as client:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("Isolated service exited")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.15)
            else:
                raise RuntimeError("Isolated service did not start")
            yield client
    finally:
        server.terminate()
        server.wait(timeout=20)


def check(root, ids, password):
    record,review=ids
    with service(root) as client:
        def login(name):
            res=client.post("/login",data={"username":name,"password":password})
            assert res.headers["location"]=="/dashboard"
        def denied(path):
            assert client.get(path).status_code==404, path
        for path in ("/uploads/vouchers/proof.png","/uploads/excel/source.xlsx"):
            denied(path)
        image=client.get("/uploads/mall_products/PRD-REVIEW/main.webp")
        assert image.status_code==200 and image.headers["content-type"]=="image/webp"
        assert image.content==(root/"uploads/mall_products/PRD-REVIEW/main.webp").read_bytes()
        url=f"/vouchers/{review}"
        assert client.get(url+"/preview").headers["location"]=="/login"
        login("reviewer")
        for page in (f"/match-reviews?review_id={review}",f"/business-records/{record}"):
            res=client.get(page)
            assert res.status_code==200 and url+"/preview" in res.text and "/uploads/vouchers/" not in res.text, page
        proof=(root/"uploads/vouchers/proof.png").read_bytes()
        preview = client.get(url + "/preview")
        assert preview.status_code == 200 and preview.content == proof
        login("bob")
        denied(url+"/download")
        login("alice")
        download=client.get(url+"/download")
        assert download.status_code == 200 and download.content == proof
        assert "no-store" in download.headers["cache-control"]
        return dict(anonymous_raw_voucher=404,anonymous_raw_excel=404, other_owner=404,
                    linked_reviewer_preview=200, owner_download=200, public_webp=200)


def run():
    if not __debug__:
        raise RuntimeError("Run without -O so that rehearsal assertions are active")
    with TemporaryDirectory(prefix="attachment-restore-") as tmp:
        base=Path(tmp)
        source=base/"source"
        source.mkdir()
        password=secrets.token_urlsafe(24)
        ids=seed(source,password)
        before=check(source,ids,password)
        original=inventory(source)
        backup=base/"backup"
        backup.mkdir()
        shutil.copyfile(source/"review.db",backup/"review.db")
        shutil.copytree(source/"uploads",backup/"uploads")
        restored=base/"restored"
        shutil.copytree(backup,restored)
        assert inventory(backup)==inventory(restored)==original
        after=check(restored,ids,password)
        assert before==after and inventory(restored)==original and inventory(source)==original
        return dict(result="PASS",scope="synthetic stopped local SQLite service with uploads",
                    before=before,after=after,restored_sha256=original)


if __name__=="__main__":
    print(json.dumps(run(),indent=2))
