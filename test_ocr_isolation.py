"""Resource admission, failure handling and voucher persistence on virtual data."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import ocr_client, ocr_service
from tools import ocr_worker


class OcrTransportTests(unittest.TestCase):
    def reply(self, status=200, body=None, error=None):
        client = httpx.Client
        def handle(request):
            self.assertEqual(str(request.url), ocr_client.ENDPOINT)
            if error: raise error
            return httpx.Response(status, json=body)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"image.png";path.write_bytes(b"virtual")
            with patch.object(ocr_client.httpx, "Client",
                              side_effect=lambda **kw: client(transport=httpx.MockTransport(handle), **kw)):
                return ocr_client.recognize_remote(path)

    def test_valid_reply(self):
        self.assertEqual(self.reply(body={"text":"virtual receipt"}),"virtual receipt")

    def test_busy_timeout_invalid_and_down_have_safe_messages(self):
        for status,word in [(429,"正在"),(504,"超时"),(422,"无法"),(503,"暂不可用"),(302,"暂不可用")]:
            with self.subTest(status=status), self.assertRaises(ocr_client.OcrUnavailable) as caught:
                self.reply(status, {"detail":"secret internal path"})
            self.assertIn(word,str(caught.exception))
            self.assertNotIn("secret",str(caught.exception))

    def test_transport_failure_is_sanitized(self):
        with self.assertRaises(ocr_client.OcrUnavailable) as caught:
            self.reply(error=httpx.ConnectError("private password"))
        self.assertNotIn("password",str(caught.exception))

    def test_malformed_or_oversized_text_rejected(self):
        for body in ({},[],{"text":42},{"text":""},{"text":"a"*65537}):
            with self.subTest(body_type=type(body)), self.assertRaises(ocr_client.OcrUnavailable):
                self.reply(body=body)

    def test_isolated_failure_never_falls_back(self):
        with patch.dict(os.environ,{"OCR_EXECUTION_MODE":"isolated"}):
            with patch.object(ocr_client,"recognize_remote",side_effect=ocr_client.OcrUnavailable("busy")):
                with patch.object(ocr_service,"local_ocr_image") as local:
                    with self.assertRaises(ocr_client.OcrUnavailable):
                        ocr_service.ocr_image("virtual.png")
                    local.assert_not_called()

    def test_web_ocr_import_does_not_load_model(self):
        result=subprocess.check_output([os.sys.executable,"-c",
            "import sys; from app import ocr_service; assert 'rapidocr' not in sys.modules; print('ok')"])
        self.assertEqual(result.strip(),b"ok")

    def test_payload_limit_before_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"large";path.write_bytes(b"a"*17)
            with patch.object(ocr_client,"MAX_IMAGE_BYTES",16),patch.object(ocr_client.httpx,"Client") as transport:
                with self.assertRaises(ocr_client.OcrUnavailable):ocr_client.recognize_remote(path)
                transport.assert_not_called()


class OcrWorkerTests(unittest.TestCase):
    def test_one_inference_busy_rejection_and_health(self):
        started=threading.Event();release=threading.Event()
        def blocked(payload):
            started.set();release.wait(5)
            return 200,{"text":"done"}
        async def exercise():
            app=ocr_worker.create_app()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://worker") as c:
                first=asyncio.create_task(c.post("/recognize",content=b"image",
                    headers={"Content-Type":"application/octet-stream"}))
                try:
                    for _ in range(100):
                        if started.is_set():break
                        await asyncio.sleep(.01)
                    self.assertTrue(started.is_set())
                    second=await c.post("/recognize",content=b"second")
                    self.assertEqual(second.status_code,429)
                    self.assertEqual((await c.get("/health")).status_code,200)
                finally:release.set()
                self.assertEqual((await first).status_code,200)
                self.assertEqual((await c.post("/recognize",content=b"again",
                    headers={"Content-Type":"application/octet-stream"})).status_code,200)
        with patch.object(ocr_worker,"execute_image",side_effect=blocked):
            asyncio.run(exercise())

    def test_invalid_empty_and_size_limits(self):
        with TestClient(ocr_worker.create_app()) as c:
            self.assertEqual(c.post("/recognize",content=b"x").status_code,415)
            headers={"Content-Type":"application/octet-stream"}
            self.assertEqual(c.post("/recognize",content=b"",headers=headers).status_code,422)
            with patch.object(ocr_worker,"MAX_IMAGE_BYTES",4):
                self.assertEqual(c.post("/recognize",content=b"12345",headers=headers).status_code,413)

    def test_timeout_and_failure_remove_private_temporary_files(self):
        seen=[]
        def timeout(command,**kwargs):
            path=Path(command[-1]);seen.append(path)
            self.assertEqual(path.read_bytes(),b"virtual")
            self.assertEqual(kwargs["timeout"],30)
            raise subprocess.TimeoutExpired(command,30)
        with patch.object(ocr_worker.subprocess,"run",side_effect=timeout):
            self.assertEqual(ocr_worker.execute_image(b"virtual")[0],504)
        self.assertTrue(all(not p.parent.exists() for p in seen))

    def test_invalid_child_response_not_leaked(self):
        result=subprocess.CompletedProcess([],0,stdout=b"internal file contents",stderr=b"private")
        with patch.object(ocr_worker.subprocess,"run",return_value=result):
            self.assertEqual(ocr_worker.execute_image(b"virtual"),(503,{"error":"unavailable"}))

    @unittest.skipUnless(os.name == "posix", "POSIX process liveness check")
    def test_real_timed_out_child_is_killed(self):
        real_run=subprocess.run
        child=[]
        def slow(command,**kwargs):
            try:
                return real_run([command[0],"-u","-c",
                    "import os,time; print(os.getpid(),flush=True); time.sleep(5)"],**kwargs)
            except subprocess.TimeoutExpired as exc:
                child.append(int(exc.stdout.strip()))
                raise
        with patch.object(ocr_worker,"TASK_TIMEOUT_SECONDS",.2):
            with patch.object(ocr_worker.subprocess,"run",side_effect=slow):
                self.assertEqual(ocr_worker.execute_image(b"virtual")[0],504)
        self.assertEqual(len(child),1)
        with self.assertRaises(ProcessLookupError):os.kill(child[0],0)

    def test_invalid_image_child_rejected(self):
        self.assertEqual(ocr_worker.execute_image(b"not an image"),(422,{"error":"unrecognizable"}))


class VoucherIsolationTests(unittest.TestCase):
    def setUp(self):
        from app import main
        from app.models import User
        from app.database import Base
        self.main=main;self.folder=tempfile.TemporaryDirectory()
        self.engine=create_engine("sqlite:///"+str(Path(self.folder.name)/"virtual.db"))
        Base.metadata.create_all(self.engine)
        self.factory=sessionmaker(bind=self.engine,autoflush=False)
        with self.factory.begin() as db:
            user=User(username="virtual-operator",password_hash="virtual",role="admin",admin_level="super_admin")
            db.add(user);db.flush();self.user_id=user.id
        with self.factory() as db:self.user=db.get(User,self.user_id)
        self.cwd=os.getcwd()
        self.template_patch=patch.object(main.templates.env.loader,"searchpath",[str(Path(self.cwd)/"app/templates")])
        self.template_patch.start()
        os.chdir(self.folder.name)
        self.session_patch=patch.object(main,"SessionLocal",self.factory);self.session_patch.start()
        self.user_patch=patch.object(main,"get_current_user",return_value=self.user);self.user_patch.start()

    def tearDown(self):
        self.user_patch.stop();self.session_patch.stop();self.template_patch.stop()
        os.chdir(self.cwd);self.engine.dispose();self.folder.cleanup()

    def upload(self,payload=b"virtual receipt"):
        from starlette.requests import Request
        from starlette.datastructures import UploadFile
        return self.main.upload_voucher_submit(Request({"type":"http","method":"POST","path":"/upload-voucher"}),
            [UploadFile(filename="virtual.png",file=io.BytesIO(payload))],0,0)

    def test_no_connection_held_during_recognition_and_duplicate_dedup(self):
        from app.models import VoucherRecord,VoucherUploadBatch
        def recognized(path):
            self.assertEqual(self.engine.pool.checkedout(),0)
            return "Amount: 100.00"
        with patch.object(self.main,"ocr_image",side_effect=recognized) as ocr:
            self.assertEqual(self.upload().status_code,200)
            self.assertEqual(self.upload().status_code,200)
            self.assertEqual(ocr.call_count,1)
        with self.factory() as db:
            self.assertEqual(db.query(VoucherRecord).count(),1)
            batches=db.query(VoucherUploadBatch).order_by(VoucherUploadBatch.id).all()
            self.assertEqual((batches[0].success_files,batches[1].duplicate_files),(1,1))

    def test_failure_persists_count_no_orphan_and_retry_succeeds(self):
        from app.models import VoucherRecord,VoucherUploadBatch
        with patch.object(self.main,"ocr_image",side_effect=ocr_client.OcrUnavailable("识别服务暂不可用")):
            response=self.upload()
        self.assertIn("暂不可用",response.body.decode())
        self.assertEqual(list(Path("uploads/vouchers").iterdir()),[])
        with self.factory() as db:
            self.assertEqual(db.query(VoucherRecord).count(),0)
            self.assertEqual(db.query(VoucherUploadBatch).one().failed_files,1)
        with patch.object(self.main,"ocr_image",return_value="Amount: 100.00"):
            self.upload()
        with self.factory() as db:self.assertEqual(db.query(VoucherRecord).count(),1)

    def test_upload_permissions_still_required(self):
        for user,location in [(None,"/login"),(type("Partner",(),{"role":"partner"})(),"/dashboard")]:
            with self.subTest(location=location),patch.object(self.main,"get_current_user",return_value=user):
                with patch.object(self.main,"ocr_image") as ocr:
                    response=self.upload()
                    self.assertEqual(response.status_code,302)
                    self.assertEqual(response.headers["location"],location)
                    ocr.assert_not_called()
