"""Real HTTP + disposable SQLite address tests; no real user or database."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import miniprogram_v1_router
from app.database import Base
from app.mall.miniprogram_session_service import issue_session
from app.models import Member, MemberAddress, MemberAddressEvent, MemberWechatBinding

APP_ID = "wx1234567890abcdef"
BODY = {"recipient_name": "仅供隔离测试", "phone": "13800000001",
        "province": "测试省", "city": "测试市", "district": "测试区", "detail": "测试路1号"}
PREFIX = "/api/miniprogram/v1/addresses"


class MemberAddressTests(unittest.TestCase):
    def make_engine(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "addresses.db"),
                                    connect_args={"check_same_thread": False, "timeout": 15})
        @event.listens_for(engine, "connect")
        def enable_fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine)
        return engine

    def setUp(self):
        self.engine = self.make_engine()
        self.addCleanup(self.engine.dispose)
        self.environment = patch.dict(os.environ, {"WECHAT_MINIPROGRAM_APP_ID": APP_ID,
            "MINIPROGRAM_SESSION_SIGNING_KEY": "address-tests-only-session-key-2026"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tokens = []
        self.member_ids = []
        with Session(self.engine) as db:
            for i in range(2):
                member = Member(member_public_id=f"ADDR-TEST-MEMBER-{i}", is_active=True)
                db.add(member)
                db.flush()
                binding = MemberWechatBinding(member_id=member.id, wechat_app_id=APP_ID,
                                               openid=f"address-test-openid-{i}")
                db.add(binding)
                db.flush()
                self.member_ids.append(member.id)
                self.tokens.append(issue_session(binding.id, APP_ID, binding.openid)[0])
            db.commit()
        self.engine_patch = patch("app.api.member_addresses.engine", self.engine)
        self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        application = FastAPI()
        application.include_router(miniprogram_v1_router)
        self.client = TestClient(application)
        self.addCleanup(self.client.close)

    def request(self, method, suffix="", *, owner=0, body=None):
        headers = {} if owner is None else {"Authorization": "Bearer " + self.tokens[owner]}
        result = self.client.request(method, PREFIX + suffix, headers=headers,
                                     **({"json": body} if body is not None else {}))
        self.assertEqual(result.headers.get("cache-control"), "no-store, private")
        return result

    def create(self, **changes):
        result = self.request("POST", body={**BODY, **changes})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def items(self):
        result = self.request("GET")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["items"]

    def test_empty_create_edit_and_list_contract(self):
        self.assertEqual(self.items(), [])
        first = self.create(recipient_name="  仅供隔离测试  ")
        self.assertTrue(first["is_default"])
        self.assertEqual(first["recipient_name"], BODY["recipient_name"])
        self.assertEqual(set(first), set(BODY) | {"is_default", "address_public_id"})
        second = self.create()
        self.assertFalse(second["is_default"])
        changed = self.request("PUT", "/" + first["address_public_id"],
                               body={**BODY, "detail": "新测试路2号"})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["is_default"])
        self.assertEqual(self.items()[0]["detail"], "新测试路2号")

    def test_delete_default_leaves_no_default_and_new_address_does_not_promote(self):
        first, second = self.create(), self.create()
        self.assertEqual(self.request("DELETE", "/" + first["address_public_id"]).status_code, 204)
        self.create()
        self.assertFalse(any(item["is_default"] for item in self.items()))
        self.assertEqual(self.request("PUT", "/" + second["address_public_id"] + "/default").status_code, 200)
        self.assertEqual(sum(x["is_default"] for x in self.items()), 1)
        for row in self.items():
            self.request("DELETE", "/" + row["address_public_id"])
        self.assertTrue(self.create()["is_default"])

    def test_default_replay_does_not_repeat_audit(self):
        first, second = self.create(), self.create()
        for _ in range(2):
            self.assertEqual(self.request("PUT", "/" + second["address_public_id"] + "/default").status_code, 200)
        with Session(self.engine) as db:
            self.assertEqual(db.query(MemberAddressEvent).filter_by(action="SET_DEFAULT").count(), 1)
        self.assertEqual(self.items()[0]["address_public_id"], second["address_public_id"])

    def test_capacity_edit_delete_and_member_independent_limit(self):
        rows = [self.create() for _ in range(20)]
        self.assertEqual(self.request("POST", body=BODY).status_code, 409)
        self.assertEqual(self.request("PUT", "/" + rows[1]["address_public_id"], body=BODY).status_code, 200)
        self.assertEqual(self.request("POST", owner=1, body=BODY).status_code, 201)
        self.request("DELETE", "/" + rows[1]["address_public_id"])
        self.create()
        self.assertEqual(len(self.items()), 20)

    def test_other_member_and_unknown_address_are_indistinguishable(self):
        row = self.create()
        self.assertEqual(self.request("GET", owner=1).json()["total"], 0)
        for method, ending, body in [("PUT", "", BODY), ("PUT", "/default", None), ("DELETE", "", None)]:
            other = self.request(method, "/" + row["address_public_id"] + ending, owner=1, body=body)
            absent = self.request(method, "/ADDR-" + "0" * 32 + ending, owner=1, body=body)
            self.assertEqual((other.status_code, other.json()), (404, absent.json()))
        self.assertEqual(len(self.items()), 1)

    def test_unauthenticated_expired_tampered_and_inactive_sessions(self):
        for method, suffix, body in [("GET", "", None), ("POST", "", BODY),
                                      ("PUT", "/missing", BODY), ("PUT", "/missing/default", None),
                                      ("DELETE", "/missing", None)]:
            self.assertEqual(self.request(method, suffix, owner=None, body=body).status_code, 401)
        original = self.tokens[0]
        self.tokens[0] = original + "corrupt"
        self.assertEqual(self.request("GET").status_code, 401)
        self.tokens[0] = issue_session(1, APP_ID, "address-test-openid-0", now=1)[0]
        self.assertEqual(self.request("GET").status_code, 401)
        self.tokens[0] = original
        with Session(self.engine) as db:
            db.get(Member, self.member_ids[0]).is_active = False
            db.commit()
        self.assertEqual(self.request("POST", body=BODY).status_code, 401)

    def test_validation_and_unknown_fields_do_not_echo_private_input(self):
        invalid = [dict(BODY, member_id=2), dict(BODY, is_default=True), dict(BODY, phone=13800000001),
                   dict(BODY, phone="private-invalid-phone"), dict(BODY, detail="x" * 241),
                   dict(BODY, recipient_name="  "), dict(BODY, province="line\nbreak"),
                   dict(BODY, city="\u200bhidden"), {k: v for k, v in BODY.items() if k != "district"}]
        for body in invalid:
            with self.subTest(keys=list(body)):
                result = self.request("POST", body=body)
                self.assertEqual(result.status_code, 422)
                self.assertNotIn(BODY["phone"], result.text)
                self.assertNotIn("private-invalid-phone", result.text)
                self.assertNotIn(BODY["recipient_name"], result.text)
        self.assertEqual(self.items(), [])

    def test_field_boundaries_and_international_phone(self):
        result = self.create(recipient_name="名" * 80, province="省" * 80,
                             city="市" * 80, district="区" * 80, detail="路" * 240,
                             phone="+123456789012345")
        self.assertEqual(len(result["detail"]), 240)

    def test_delete_removes_pii_but_keeps_minimal_event(self):
        row = self.create()
        self.request("DELETE", "/" + row["address_public_id"])
        self.assertEqual(self.request("DELETE", "/" + row["address_public_id"]).status_code, 404)
        with Session(self.engine) as db:
            self.assertEqual(db.query(MemberAddress).count(), 0)
            events = db.query(MemberAddressEvent).all()
            self.assertEqual([e.action for e in events], ["CREATE", "DELETE"])
            self.assertEqual(set(MemberAddressEvent.__table__.columns.keys()),
                             {"id", "member_id", "address_public_id", "action", "created_at"})

    def test_audit_failure_rolls_back_and_sanitizes_database_error(self):
        with patch("app.mall.member_address_service._event",
                   side_effect=IntegrityError("private-sql", {"phone": BODY["phone"]}, Exception("private"))):
            response = self.request("POST", body=BODY)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(BODY["phone"], response.text)
        self.assertEqual(self.items(), [])

    def test_failed_default_switch_restores_old_default(self):
        first, second = self.create(), self.create()
        with patch("app.mall.member_address_service._event", side_effect=IntegrityError("", {}, Exception())):
            result = self.request("PUT", "/" + second["address_public_id"] + "/default")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.items()[0]["address_public_id"], first["address_public_id"])

    def test_database_unique_default_constraint(self):
        self.create()
        with Session(self.engine) as db:
            db.add(MemberAddress(member_id=self.member_ids[0], address_public_id="ADDR-" + "f" * 32,
                                 is_default=True, **BODY))
            with self.assertRaises(IntegrityError):
                db.commit()

    def parallel(self, actions):
        barrier = Barrier(len(actions))
        def worker(action):
            barrier.wait(timeout=10)
            return action()
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(worker, actions))

    def test_concurrent_first_creates_have_one_default(self):
        results = self.parallel([lambda: self.request("POST", body=BODY) for _ in range(3)])
        self.assertEqual([r.status_code for r in results], [201] * 3)
        self.assertEqual(sum(x["is_default"] for x in self.items()), 1)

    def test_concurrent_capacity_cannot_exceed_twenty(self):
        for _ in range(19): self.create()
        results = self.parallel([lambda: self.request("POST", body=BODY) for _ in range(3)])
        self.assertEqual(sorted(r.status_code for r in results), [201, 409, 409])
        self.assertEqual(len(self.items()), 20)

    def test_concurrent_default_selection_and_delete(self):
        first, second, third = self.create(), self.create(), self.create()
        results = self.parallel([
            lambda: self.request("PUT", "/" + second["address_public_id"] + "/default"),
            lambda: self.request("PUT", "/" + third["address_public_id"] + "/default"),
            lambda: self.request("DELETE", "/" + first["address_public_id"]),
        ])
        self.assertEqual(sorted(r.status_code for r in results), [200, 200, 204])
        self.assertEqual(sum(x["is_default"] for x in self.items()), 1)


if __name__ == "__main__":
    unittest.main()
