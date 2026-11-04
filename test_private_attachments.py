from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from datetime import datetime
from io import BytesIO
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy.orm import sessionmaker

from app import main, models
from app.admin_permissions import PRIMARY_REVIEWER
from app.auth import get_password_hash
from app.database import Base, create_database_engine


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(chdir(root))
        engine = create_database_engine("sqlite:///" + (root / "synthetic.db").as_posix())
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine)
        self.enterContext(patch.object(main, "SessionLocal", self.sessions))
        for folder in ("vouchers", "excel", "promotion_pages", "mall_products"):
            (Path("uploads") / folder).mkdir(parents=True)
        self.password = token_urlsafe(20)
        password_hash = get_password_hash(self.password)
        with self.sessions.begin() as db:
            for name, role in (("reviewer", "admin"), ("alice", "partner"), ("bob", "partner")):
                db.add(models.User(username=name, password_hash=password_hash, role=role, is_active=True,
                                   admin_level=PRIMARY_REVIEWER if role == "admin" else None))
            db.flush()
            self.review_ids = []
            self.record_ids = []
            for name, state in (("alice", "已通过"), ("bob", "已通过"), ("alice", "待审核")):
                owner = db.query(models.User).filter_by(username=name).one()
                label = name + str(len(self.review_ids))
                batch = models.UploadBatch(user_id=owner.id, filename=label+".xlsx", acceptance_status="已承接")
                db.add(batch)
                db.flush()
                record = models.BusinessRecord(user_id=owner.id, batch_id=batch.id, points_amount=20, name=label, public_business_no=label)
                stored = Path("uploads/vouchers") / (label + ".png")
                stored.write_bytes(label.encode())
                voucher = models.VoucherRecord(filename="voucher.png", file_path=str(stored), voucher_amount=20)
                db.add_all((record, voucher))
                db.flush()
                review = models.MatchReview(voucher_id=voucher.id, business_record_id=record.id, review_status=state,
                                            match_status="匹配", allocation_amount=20)
                db.add(review)
                db.flush()
                self.review_ids.append(review.id)
                self.record_ids.append(record.id)
        Path("uploads/excel/original.xlsx").write_bytes(b"private excel")
        self.client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def login(self, name):
        client = TestClient(main.app, follow_redirects=False)
        self.addCleanup(client.close)
        response = client.post("/login", data={"username": name, "password": self.password})
        self.assertEqual(response.headers["location"], "/dashboard")
        return client

    def test_public_files_and_anonymous_direct_access(self):
        for folder in ("promotion_pages", "mall_products"):
            path = Path("uploads") / folder / "cover.webp"
            path.write_bytes(b"public media")
            response = self.client.get("/" + path.as_posix())
            self.assertEqual(response.content, b"public media")
            self.assertEqual(response.headers["content-type"], "image/webp")
        for path in ("/uploads/excel/original.xlsx", "/uploads/vouchers/alice0.png",
                     "/uploads/mall_products/%2e%2e%2fvouchers/alice0.png",
                     "/uploads/promotion_pages/..%5cvouchers%5calice0.png"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        for action in ("preview", "download"):
            self.assertEqual(self.client.get(f"/vouchers/{self.review_ids[0]}/{action}").headers["location"], "/login")

    def test_reviewer_preview_and_owner_download(self):
        reviewer, alice, bob = [self.login(name) for name in ("reviewer","alice","bob")]
        approved, other, pending = self.review_ids
        self.assertEqual(reviewer.get(f"/vouchers/{pending}/preview").content, b"alice2")
        self.assertEqual(reviewer.get(f"/vouchers/{pending}/download").status_code, 404)
        self.assertEqual(alice.get(f"/vouchers/{pending}/preview").status_code, 404)
        self.assertEqual(alice.get(f"/vouchers/{pending}/download").status_code, 404)
        self.assertEqual(alice.get(f"/vouchers/{other}/download").status_code, 404)
        self.assertEqual(bob.get(f"/vouchers/{approved}/download").status_code, 404)
        self.assertEqual(alice.get("/uploads/vouchers/alice0.png").status_code, 404)
        for client in (alice, reviewer):
            response = client.get(f"/vouchers/{approved}/download")
            self.assertEqual(response.content, b"alice0")
            self.assertIn("attachment",response.headers["content-disposition"])
            self.assertIn("no-store",response.headers["cache-control"])
        batch = f"/business-records/{self.record_ids[0]}/vouchers/download-all"
        self.assertEqual(bob.get(batch).status_code, 302)
        with ZipFile(BytesIO(alice.get(batch).content)) as archive:
            self.assertEqual([archive.read(name) for name in archive.namelist()], [b"alice0"])
        with ZipFile(BytesIO(bob.get("/my-settlement/vouchers/download").content)) as archive:
            self.assertEqual([archive.read(name) for name in archive.namelist()], [b"bob1"])

    def test_private_storage_boundaries_and_legacy_paths(self):
        from app.private_vouchers import voucher_path
        old = Path("uploads/old.jpg")
        old.write_bytes(b"legacy")
        for path in (str(old), str(old.resolve()), "/uploads/old.jpg", r"uploads\old.jpg"):
            self.assertEqual(voucher_path(path), old.resolve())
        for path in ("uploads/vouchers/../excel/original.xlsx", "uploads/excel/original.xlsx",
                     "uploads/vouchers/missing.png", "../outside.png", "uploads/vouchers/x:stream"):
            self.assertIsNone(voucher_path(path))
        reviewer = self.login("reviewer")
        with self.sessions.begin() as db:
            review = db.get(models.MatchReview, self.review_ids[0])
            db.get(models.VoucherRecord, review.voucher_id).file_path = "uploads/excel/original.xlsx"
        self.assertEqual(reviewer.get(f"/vouchers/{self.review_ids[0]}/preview").status_code, 404)
        self.assertEqual(reviewer.get(f"/vouchers/{self.review_ids[0]}/download").status_code, 404)
        alice = self.login("alice")
        self.assertEqual(alice.get(f"/business-records/{self.record_ids[0]}/vouchers/download-all").status_code, 302)

    def test_same_second_exports_remain_scoped(self):
        clients = [self.login(name) for name in ("alice", "bob")]
        with patch.object(main,"utc8_now",return_value=datetime(2026,11,4,12)), ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda c:c.get("/business-records/export"),clients))
        for index,response in enumerate(responses):
            self.assertEqual(response.status_code,200)
            self.assertIn("no-store",response.headers["cache-control"])
            workbook=load_workbook(BytesIO(response.content),read_only=True)
            try:
                data=str(list(workbook["业务数据"].values))
                self.assertIn(("alice0","bob1")[index],data)
                self.assertNotIn(("bob1","alice0")[index],data)
            finally:
                workbook.close()
        self.assertFalse(Path("exports").exists())
        self.assertEqual(self.login("reviewer").get("/business-records/export").status_code,302)
        for client, endpoint in [(clients[0],"/my-settlement/export"),(self.login("reviewer"),"/stats-dashboard/export")]:
            response = client.get(endpoint)
            self.assertEqual(response.status_code,200)
            self.assertIn("no-store",response.headers["cache-control"])
            workbook=load_workbook(BytesIO(response.content),read_only=True)
            self.assertIn("明细",workbook.sheetnames)
            workbook.close()
