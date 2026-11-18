"""Virtual fixture helper for isolated tests, not an application enrollment API.

Requires an explicitly supplied test Session. Never opens an environment database.
Creates source evidence and a qualification through the normal confirmation flow.
"""
from datetime import timedelta
from uuid import uuid4

from app import models as m
from app.mall import qualification_service as q
from app.mall.activation_service import activate_mall_business, issue_one_time_activation_code
from app.mall.miniprogram_session_service import issue_session
from app.time_utils import utc8_now


def virtual_catalog_member(db, *, app_id="virtual-catalog-app", now=None):
    now = now or utc8_now()
    label = uuid4().hex[:12]
    actor = m.User(username="virtual-catalog-"+label, password_hash="virtual-only", role="admin", admin_level="operator", is_active=True)
    db.add(actor); db.flush()
    batch = m.UploadBatch(user_id=actor.id, filename="virtual.xlsx", acceptance_status="已承接",
                         redemption_mode="MALL_REDEMPTION", claim_deadline=now+timedelta(days=30))
    db.add(batch); db.flush()
    business = m.BusinessRecord(user_id=actor.id, batch_id=batch.id, business_no="BR-"+label,
        public_business_no="BR-"+label, name="虚拟会员", phone="13800000001", plate_number="沪A00001",
        points_amount=1, redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION")
    db.add(business); db.flush()
    virtual_batch_scope(db, batch, now=now)
    q.confirm(db, actor_id=actor.id, batch_id=batch.id, business_ids=[business.id],
              valid_from=now-timedelta(days=1), valid_until=now+timedelta(days=30), reason="虚拟名单确认", now=now)
    code = issue_one_time_activation_code(db, business_record=business, now=now)
    result = activate_mall_business(db, business_public_no=business.public_business_no, phone=business.phone,
        plate_number=business.plate_number, activation_code=code.activation_code,
        wechat_app_id=app_id, openid="virtual-openid-"+label, now=now)
    assert result.success
    member = db.query(m.Member).filter_by(member_public_id=result.member_public_id).one()
    binding = db.query(m.MemberWechatBinding).filter_by(member_id=member.id).one()
    return member, issue_session(binding.id, app_id, binding.openid)[0]


def confirm_existing_virtual_member(db, *, member_id, actor_id, now):
    """For legacy order fixtures which already contain verified bindings/grants."""
    grant = db.query(m.PointsGrant).join(m.PointsAccount).filter(m.PointsAccount.member_id == member_id).first()
    if grant is None:
        raise ValueError("虚拟会员缺少来源业务")
    business = db.get(m.BusinessRecord, grant.business_record_id)
    virtual_batch_scope(db, db.get(m.UploadBatch, business.batch_id), now=now)
    q.confirm(db, actor_id=actor_id, batch_id=business.batch_id, business_ids=[business.id],
        valid_from=now-timedelta(days=1), valid_until=now+timedelta(days=30), reason="虚拟存量会员名单确认", now=now)
    row = db.query(m.MemberQualification).filter_by(business_record_id=business.id).one()
    assert row.member_id == member_id
    return row


def virtual_batch_scope(db, batch, *, now=None, organization=None):
    """Explicit synthetic ownership; never callable from production enrollment."""
    now = now or utc8_now()
    existing = db.get(m.ClientBatchOwnership, batch.id)
    if existing: return db.get(m.ClientOrganization, existing.organization_id)
    org = organization or m.ClientOrganization(code='VIRTUAL-'+uuid4().hex[:12], name='虚拟甲方分支',
        region='虚拟地区', is_active=True, revision=1, updated_by_id=batch.user_id, created_at=now, updated_at=now)
    db.add(org); db.flush()
    db.add(m.ClientBatchOwnership(batch_id=batch.id, organization_id=org.id, organization_code=org.code,
        organization_name=org.name, organization_region=org.region, uploaded_by_id=batch.user_id, created_at=now))
    db.flush()
    from app.mall.institution_scope import bind_grant
    for grant in db.query(m.PointsGrant).join(m.BusinessRecord).filter(m.BusinessRecord.batch_id==batch.id):
        bind_grant(db, grant, required=True)
    return org
