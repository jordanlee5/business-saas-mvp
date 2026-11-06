"""Virtual eligibility lifecycle, trust boundaries, HTTP forms and migrations."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models as m
from app.database import Base
from app.mall import qualification_service as q
from app.mall.activation_service import activate_mall_business, issue_one_time_activation_code

NOW = datetime(2026, 11, 6, 12, 0)
START, END = NOW - timedelta(days=1), NOW + timedelta(days=30)


def seed(db):
    operator = m.User(username="virtual-operator", password_hash="virtual-only", role="admin", admin_level="operator", is_active=True)
    partner = m.User(username="virtual-partner", password_hash="virtual-only", role="partner", is_active=True)
    db.add_all([operator, partner]); db.flush()
    batch = m.UploadBatch(user_id=partner.id, filename="virtual-list.xlsx", acceptance_status="已承接",
        redemption_mode="MALL_REDEMPTION", claim_deadline=END, total_rows=1, success_rows=1, failed_rows=0)
    db.add(batch); db.flush()
    business = m.BusinessRecord(user_id=partner.id, batch_id=batch.id, business_no="BR-VIRTUAL-QUALIFICATION",
        public_business_no="BR-VIRTUAL-QUALIFICATION", name="虚拟会员", phone="13800000001", plate_number="沪A00001",
        points_amount=100, bank_card="", redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION")
    db.add(business); db.flush()
    return operator, partner, batch, business


def activation(db, business, openid="virtual-qualification-owner"):
    code = issue_one_time_activation_code(db, business_record=business, now=NOW)
    return activate_mall_business(db, business_public_no=business.public_business_no, phone=business.phone,
        plate_number=business.plate_number, activation_code=code.activation_code,
        wechat_app_id="virtual-qualification-app", openid=openid, now=NOW)


class MemberQualificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session(); self.addCleanup(self.db.close)
        self.actor, self.partner, self.batch, self.business = seed(self.db)
        self.db.commit()

    def confirm(self, **kwargs):
        args = dict(actor_id=self.actor.id, batch_id=self.batch.id, business_ids=[self.business.id],
                    valid_from=START, valid_until=END, reason="甲方已确认虚拟名单", now=NOW)
        args.update(kwargs)
        return q.confirm(self.db, **args)

    def change(self, row, action, **kwargs):
        args = dict(actor_id=self.actor.id, qualification_id=row.id, version=row.version,
                    action=action, reason="虚拟资格维护", now=NOW)
        args.update(kwargs)
        return q.update(self.db, **args)

    def row(self):
        return self.db.query(m.MemberQualification).filter_by(business_record_id=self.business.id).one()

    def test_confirm_then_verified_activation_binds_once(self):
        self.assertEqual(self.confirm(), 1)
        row = self.row()
        self.assertEqual(q.status(row, NOW), "待会员激活绑定")
        self.assertTrue(activation(self.db, self.business).success)
        self.assertEqual(row.member_id, self.db.query(m.Member).one().id)
        self.assertTrue(q.has_effective_qualification(self.db, row.member_id, now=NOW))
        events = self.db.query(m.MemberQualificationEvent).order_by(m.MemberQualificationEvent.id).all()
        self.assertEqual([e.action for e in events], ["CONFIRM", "BIND"])
        self.assertEqual(events[0].actor_id, self.actor.id)
        self.assertIsNone(events[1].actor_id)
        self.assertIsNotNone(events[1].created_at)
        self.assertNotIn("openid", events[1].after_state)
        self.assertNotIn(self.business.phone, str([e.reason for e in events]))
        q.bind_after_activation(self.db, self.business.id, now=NOW)
        self.assertEqual(self.db.query(m.MemberQualificationEvent).count(), 2)

    def test_existing_activation_requires_explicit_confirmation(self):
        self.assertTrue(activation(self.db, self.business).success)
        self.assertEqual(self.db.query(m.MemberQualification).count(), 0)
        member = self.db.query(m.Member).one()
        self.assertFalse(q.has_effective_qualification(self.db, member.id, now=NOW))
        self.confirm()
        self.assertEqual(self.row().member_id, member.id)
        self.assertEqual(self.db.query(m.MemberQualificationEvent).filter_by(action="BIND").one().actor_id, self.actor.id)

    def test_repeat_confirmation_cannot_reenable_or_extend(self):
        self.confirm(); row = self.row(); self.change(row, "DISABLE")
        self.assertEqual(self.confirm(valid_until=END+timedelta(days=40)), 0)
        self.assertFalse(row.enabled); self.assertEqual(row.valid_until, END)
        self.assertTrue(activation(self.db, self.business).success)
        self.assertIsNotNone(row.member_id); self.assertFalse(row.enabled)
        self.assertFalse(q.has_effective_qualification(self.db, row.member_id, now=NOW))

    def test_enable_disable_validity_and_stale_form(self):
        self.confirm(); activation(self.db, self.business); row = self.row(); old = row.version
        self.change(row, "DISABLE")
        with self.assertRaisesRegex(ValueError, "已被更新"):
            self.change(row, "ENABLE", version=old)
        self.change(row, "ENABLE")
        self.change(row, "VALIDITY", valid_from=NOW+timedelta(days=1), valid_until=END)
        self.assertEqual(q.status(row, NOW), "尚未生效")
        self.assertFalse(q.has_effective_qualification(self.db, row.member_id, now=NOW))
        self.assertTrue(q.has_effective_qualification(self.db, row.member_id, now=NOW+timedelta(days=1)))
        self.assertFalse(q.has_effective_qualification(self.db, row.member_id, now=END))
        self.assertEqual(q.status(row, END), "已到期")
        last = self.db.query(m.MemberQualificationEvent).order_by(m.MemberQualificationEvent.id.desc()).first()
        self.assertNotEqual(last.before_state, last.after_state)
        self.assertFalse(self.change(row, "ENABLE"))

    def test_sources_rejected_atomically(self):
        for obj, field, value in [(self.batch, "acceptance_status", "待承接"),
            (self.batch, "redemption_mode", "CASH_REBATE"), (self.business, "redemption_mode", "CASH_REBATE"),
            (self.business, "claim_status", "FROZEN"), (self.business, "claim_status", "EXPIRED"),
            (self.business, "user_id", self.actor.id)]:
            with self.subTest(field=field, value=value):
                old = getattr(obj, field)
                if obj is self.business and field == "redemption_mode":
                    self.business.claim_status = None
                setattr(obj, field, value); self.db.flush()
                with self.assertRaises(ValueError): self.confirm()
                self.assertEqual(self.db.query(m.MemberQualification).count(), 0)
                setattr(obj, field, old)
                if obj is self.business and field == "redemption_mode":
                    self.business.claim_status = "PENDING_ACTIVATION"
                self.db.flush()
        with self.assertRaises(ValueError): self.confirm(business_ids=[self.business.id, 99999])
        with self.assertRaises(ValueError): self.confirm(batch_id=99999)
        self.assertEqual(self.db.query(m.MemberQualificationEvent).count(), 0)

    def test_cross_batch_selection_and_size_limits(self):
        other = m.UploadBatch(user_id=self.partner.id, filename="another.xlsx", acceptance_status="已承接", redemption_mode="MALL_REDEMPTION")
        self.db.add(other); self.db.flush()
        with self.assertRaises(ValueError): self.confirm(batch_id=other.id)
        with self.assertRaises(ValueError): self.confirm(business_ids=[])
        with self.assertRaises(ValueError): self.confirm(business_ids=list(range(1,27)))
        self.assertEqual(self.confirm(business_ids=[self.business.id]*2), 1)

    def test_actor_permission_is_reread_from_database(self):
        for role, level, active in [("partner", "operator", True), ("admin", "primary_reviewer", True),
                                  ("admin", "secondary_reviewer", True), ("admin", "operator", False)]:
            with self.subTest(role=role, level=level, active=active):
                self.actor.role, self.actor.admin_level, self.actor.is_active = role, level, active
                self.db.flush()
                with self.assertRaises(PermissionError): self.confirm()
        self.actor.role, self.actor.admin_level, self.actor.is_active = "admin", "super_admin", True
        self.db.flush(); self.assertEqual(self.confirm(), 1)

    def test_no_manual_member_assignment_without_activation_proof(self):
        self.business.claim_status = "ACTIVATED"; self.db.flush()
        self.confirm()
        self.assertIsNone(self.row().member_id)
        with self.assertRaises(ValueError): self.change(self.row(), "BIND")
        self.assertFalse(q.has_effective_qualification(self.db, 999, now=NOW))

    def test_two_sources_and_zero_balance_do_not_remove_history_access(self):
        self.confirm(); activation(self.db, self.business); first = self.row()
        second_partner = m.User(username="virtual-other-partner", password_hash="test", role="partner")
        self.db.add(second_partner); self.db.flush()
        batch = m.UploadBatch(user_id=second_partner.id, filename="other-source.xlsx", acceptance_status="已承接", redemption_mode="MALL_REDEMPTION", claim_deadline=END)
        self.db.add(batch); self.db.flush()
        business = m.BusinessRecord(user_id=second_partner.id, batch_id=batch.id, business_no="BR-OTHER", public_business_no="BR-OTHER", name="虚拟会员", phone="13800000001", plate_number="沪A00001", points_amount=50, redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION")
        self.db.add(business); self.db.flush()
        self.confirm(batch_id=batch.id, business_ids=[business.id]); self.assertTrue(activation(self.db, business).success)
        rows = self.db.query(m.MemberQualification).all(); self.assertEqual(len({r.member_id for r in rows}), 1)
        # A depleted account does not determine browsing eligibility.
        account = self.db.query(m.PointsAccount).one(); account.available_points = 0; self.db.flush()
        grants_before = [(g.id, g.available_points) for g in self.db.query(m.PointsGrant)]
        ledger_count = self.db.query(m.PointsLedgerEntry).count()
        self.change(first, "DISABLE")
        self.assertTrue(q.has_effective_qualification(self.db, first.member_id, now=NOW))
        self.change(rows[1], "DISABLE")
        self.assertFalse(q.has_effective_qualification(self.db, first.member_id, now=NOW))
        self.assertTrue(self.db.query(m.Member).one().is_active)
        self.assertEqual(self.db.query(m.MemberWechatBinding).count(), 1)
        self.assertEqual(ledger_count, self.db.query(m.PointsLedgerEntry).count())
        self.assertEqual(grants_before, [(g.id,g.available_points) for g in self.db.query(m.PointsGrant)])

    def test_invalid_period_and_note_leave_no_qualification(self):
        for args in ({"valid_until": START}, {"valid_until": START-timedelta(days=1)}, {"reason": " "}, {"reason": "x"*501}):
            with self.assertRaises(ValueError): self.confirm(**args)
        self.assertEqual(self.db.query(m.MemberQualification).count(), 0)
        with self.assertRaises(ValueError): q.parse_time("2026-02-30T01:00")

    def test_audit_failure_rolls_back_confirmation_and_activation(self):
        with patch.object(q, "event", side_effect=RuntimeError("virtual audit failure")):
            with self.assertRaises(RuntimeError): self.confirm()
        self.db.rollback(); self.assertEqual(self.db.query(m.MemberQualification).count(), 0)
        self.confirm(); self.db.commit()
        with patch.object(q, "event", side_effect=RuntimeError("virtual audit failure")):
            with self.assertRaises(RuntimeError): activation(self.db, self.business)
        self.db.rollback()
        self.assertEqual(self.db.query(m.Member).count(), 0)
        self.assertEqual(self.db.query(m.PointsGrant).count(), 0)
        self.assertIsNone(self.row().member_id)

    def test_queue_and_history_are_paginated(self):
        for i in range(28):
            self.db.add(m.BusinessRecord(user_id=self.partner.id, batch_id=self.batch.id,
                business_no=f"BR-PAGE-{i}", public_business_no=f"BR-PAGE-{i}", redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION"))
        self.db.flush()
        rows, more = q.queue(self.db, self.batch.id, 1)
        self.assertEqual(len(rows), 25); self.assertTrue(more)
        self.assertEqual(len(q.queue(self.db, self.batch.id, 2)[0]), 4)
        self.confirm(); row=self.row()
        for i in range(28): self.change(row, "DISABLE" if i%2==0 else "ENABLE")
        events, more = q.history(self.db, row.id, 1)
        self.assertEqual(len(events), 25); self.assertTrue(more)
        self.assertEqual(len(q.history(self.db, row.id, 2)[0]), 4)

    def test_http_forms_csrf_roles_and_escaped_history(self):
        from app.main import app
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch("app.main.get_current_user", return_value=self.actor), patch("app.main.SessionLocal", self.Session):
            response = client.get(f"/mall-qualifications?batch_id={self.batch.id}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store, private")
            self.assertNotIn(self.business.phone, response.text)
            self.assertNotIn(self.business.plate_number, response.text)
            token=client.cookies.get("mall_qualifications_csrf")
            data=dict(batch_id=self.batch.id, business_ids=[self.business.id], valid_from=START.strftime("%Y-%m-%dT%H:%M"), valid_until=END.strftime("%Y-%m-%dT%H:%M"), reason="<script>virtual</script>")
            denied=client.post("/mall-qualifications/confirm", data={**data,"csrf_token":"bad"}, follow_redirects=False)
            self.assertEqual(denied.status_code,303); self.assertIn("error=",denied.headers["location"])
            self.assertEqual(self.db.query(m.MemberQualification).count(),0); self.db.rollback()
            response=client.post("/mall-qualifications/confirm",data={**data,"csrf_token":token})
            self.assertEqual(response.status_code,200); row=self.row(); self.db.rollback()
            detail=client.get(f"/mall-qualifications?qualification_id={row.id}")
            self.assertIn("&lt;script&gt;virtual&lt;/script&gt;", detail.text)
            self.assertNotIn("<script>virtual</script>", detail.text)
            token=client.cookies.get("mall_qualifications_csrf")
            changed=client.post(f"/mall-qualifications/{row.id}/update", data=dict(version=row.version,action="DISABLE",reason="虚拟停用",csrf_token=token))
            self.assertIn("已停用",changed.text)
        for actor in (None, self.partner):
            with patch("app.main.get_current_user", return_value=actor), patch("app.main.SessionLocal") as session:
                self.assertEqual(client.get("/mall-qualifications", follow_redirects=False).status_code,302)
                self.assertEqual(client.post("/mall-qualifications/confirm",data=data,follow_redirects=False).status_code,302)
                session.assert_not_called()


class QualificationMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_data_and_refuses_history_loss(self):
        directory=self.enterContext(tempfile.TemporaryDirectory())
        url="sqlite:///"+(Path(directory)/"virtual-migration.db").as_posix()
        config=Config("alembic.ini"); config.attributes["database_url"]=url
        command.upgrade(config,"0017_backoffice_sessions")
        engine=create_engine(url); self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(username,password_hash,role,is_active) VALUES ('virtual-preserved','test','partner',1)"))
        command.upgrade(config,"head")
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT username FROM users")).scalar_one(),"virtual-preserved")
        command.downgrade(config,"0017_backoffice_sessions")
        self.assertNotIn("member_qualifications",inspect(engine).get_table_names())
        command.upgrade(config,"head")
        with sessionmaker(bind=engine)() as db:
            actor, partner, batch, business=seed(db)
            q.confirm(db,actor_id=actor.id,batch_id=batch.id,business_ids=[business.id],valid_from=START,valid_until=END,reason="虚拟确认",now=NOW)
            db.commit()
        with self.assertRaisesRegex(RuntimeError,"拒绝删除留痕"):
            command.downgrade(config,"0017_backoffice_sessions")
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT count(*) FROM member_qualification_events")).scalar_one(),1)


if __name__ == "__main__":
    unittest.main()
