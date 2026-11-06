"""Explicit eligibility, sourced from accepted lists and verified activations.

Callers own transactions. SQLite writes must begin IMMEDIATE before reads;
PostgreSQL locks business -> qualification, matching activation lock ordering.
This module does not yet gate public catalog or image endpoints.
"""
import json
from datetime import datetime

from .. import models as m
from ..admin_permissions import can_manage_mall_qualifications
from ..time_utils import UTC8_TIMEZONE, utc8_now, format_utc8

ACTIONS = {"CONFIRM": "名单确认", "BIND": "关联已核验会员", "ENABLE": "启用资格",
           "DISABLE": "停用资格", "VALIDITY": "调整有效期"}
PAGE_SIZE = 25


def local_time(value):
    if value.tzinfo is not None:
        return value.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return value


def db_time(db, value):
    value = local_time(value)
    return value if db.get_bind().dialect.name == "sqlite" else value.replace(tzinfo=UTC8_TIMEZONE)


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        raise ValueError("请填写有效的开始和结束时间（北京时间）") from None


def period(start, end):
    if not isinstance(start, datetime) or not isinstance(end, datetime) or local_time(end) <= local_time(start):
        raise ValueError("结束时间必须晚于开始时间")


def operator(db, actor_id):
    actor = db.query(m.User).filter_by(id=actor_id).populate_existing().with_for_update().one_or_none()
    if not can_manage_mall_qualifications(actor):
        raise PermissionError("仅启用的运营管理员或超级管理员可管理资格")


def checked_reason(value):
    value = value.strip() if isinstance(value, str) else ""
    if not value or len(value) > 500:
        raise ValueError("请填写 1—500 字的操作原因，不要填写个人身份资料")
    return value


def snapshot(row):
    return json.dumps({"enabled": row.enabled, "member_id": row.member_id,
        "valid_from": format_utc8(row.valid_from), "valid_until": format_utc8(row.valid_until),
        "version": row.version}, ensure_ascii=False, sort_keys=True)


def event(db, row, actor_id, action, reason, before, now):
    db.add(m.MemberQualificationEvent(qualification_id=row.id, actor_id=actor_id,
        action=action, reason=reason, before_state=before, after_state=snapshot(row), created_at=now))


def verified_member(db, business_id):
    # Membership identity originates in server-verified WeChat activation. No
    # operator-supplied phone/OpenID/member id is accepted as binding authority.
    return (db.query(m.Member.id).join(m.PointsAccount, m.PointsAccount.member_id == m.Member.id)
        .join(m.PointsGrant, m.PointsGrant.account_id == m.PointsAccount.id)
        .filter(m.PointsGrant.business_record_id == business_id,
                db.query(m.MemberWechatBinding.id).filter(m.MemberWechatBinding.member_id == m.Member.id).exists())
        .scalar())


def _bind(db, row, actor_id, now):
    member_id = verified_member(db, row.business_record_id)
    if not member_id:
        return False
    if row.member_id is not None:
        if row.member_id != member_id:
            raise ValueError("会员关联异常，请核查原激活记录；不能覆盖原绑定")
        return False
    before = snapshot(row)
    row.member_id, row.bound_at = member_id, now
    row.version += 1
    row.updated_at = now
    event(db, row, actor_id, "BIND", "依据该业务的已核验微信激活记录关联", before, now)
    return True


def bind_after_activation(db, business_id, *, now):
    row = db.query(m.MemberQualification).filter_by(business_record_id=business_id).with_for_update().one_or_none()
    if row is not None:
        _bind(db, row, None, db_time(db, now))
        db.flush()


def confirm(db, *, actor_id, batch_id, business_ids, valid_from, valid_until, reason, now=None):
    operator(db, actor_id)
    reason = checked_reason(reason)
    period(valid_from, valid_until)
    if not business_ids or len(business_ids) > PAGE_SIZE or any(type(i) is not int or i <= 0 for i in business_ids):
        raise ValueError("请选择本批次 1—25 条名单记录")
    ids = sorted(set(business_ids))
    records = (db.query(m.BusinessRecord).filter(m.BusinessRecord.id.in_(ids))
               .order_by(m.BusinessRecord.id).populate_existing().with_for_update().all())
    batch = db.query(m.UploadBatch).filter_by(id=batch_id).populate_existing().with_for_update().one_or_none()
    if (not batch or batch.acceptance_status != "已承接" or batch.redemption_mode != "MALL_REDEMPTION"
        or len(records) != len(ids) or any(r.batch_id != batch.id or r.user_id != batch.user_id
        or r.redemption_mode != "MALL_REDEMPTION" or r.claim_status not in ("PENDING_ACTIVATION", "ACTIVATED") for r in records)):
        raise ValueError("只可确认当前已承接商城批次内、来源一致且未冻结的名单")
    now = db_time(db, now or utc8_now())
    created = 0
    for record in records:
        row = db.query(m.MemberQualification).filter_by(business_record_id=record.id).with_for_update().one_or_none()
        if row is not None:
            # A retry never re-enables, extends or silently changes a qualification.
            continue
        row = m.MemberQualification(business_record_id=record.id, enabled=True, version=1,
            valid_from=db_time(db, valid_from), valid_until=db_time(db, valid_until),
            confirmed_by=actor_id, confirmed_at=now, updated_at=now)
        db.add(row)
        db.flush()
        event(db, row, actor_id, "CONFIRM", reason, "{}", now)
        if record.claim_status == "ACTIVATED":
            _bind(db, row, actor_id, now)
        created += 1
    db.flush()
    return created


def update(db, *, actor_id, qualification_id, version, action, reason, valid_from=None, valid_until=None, now=None):
    operator(db, actor_id)
    reason = checked_reason(reason)
    row = (db.query(m.MemberQualification).filter_by(id=qualification_id)
           .populate_existing().with_for_update().one_or_none())
    if row is None:
        raise ValueError("资格不存在")
    if row.version != version:
        raise ValueError("资格已被更新，请刷新后重新核对")
    before = snapshot(row)
    if action in ("ENABLE", "DISABLE"):
        enabled = action == "ENABLE"
        if row.enabled == enabled:
            return False
        row.enabled = enabled
    elif action == "VALIDITY":
        period(valid_from, valid_until)
        if local_time(row.valid_from) == local_time(valid_from) and local_time(row.valid_until) == local_time(valid_until):
            return False
        row.valid_from, row.valid_until = db_time(db, valid_from), db_time(db, valid_until)
    else:
        raise ValueError("不支持的资格操作")
    row.version += 1
    row.updated_at = db_time(db, now or utc8_now())
    event(db, row, actor_id, action, reason, before, row.updated_at)
    db.flush()
    return True


def status(row, now=None):
    if row is None:
        return "待确认"
    if not row.enabled:
        return "已停用"
    if row.member_id is None:
        return "待会员激活绑定"
    now = local_time(now or utc8_now())
    if now < local_time(row.valid_from):
        return "尚未生效"
    if now >= local_time(row.valid_until):
        return "已到期"
    return "有效"


def has_effective_qualification(db, member_id, *, now=None):
    """Foundation for next-round request gating; not a replacement for session auth."""
    now = db_time(db, now or utc8_now())
    return db.query(m.MemberQualification.id).join(m.Member, m.Member.id == m.MemberQualification.member_id).filter(
        m.Member.id == member_id, m.Member.is_active.is_(True),
        m.MemberQualification.enabled.is_(True), m.MemberQualification.valid_from <= now,
        m.MemberQualification.valid_until > now,
        db.query(m.MemberWechatBinding.id).filter(m.MemberWechatBinding.member_id == member_id).exists()).first() is not None


def queue(db, batch_id, page):
    query = (db.query(m.BusinessRecord, m.UploadBatch, m.User.username, m.MemberQualification, m.Member.member_public_id)
        .join(m.UploadBatch, m.UploadBatch.id == m.BusinessRecord.batch_id)
        .join(m.User, m.User.id == m.UploadBatch.user_id)
        .outerjoin(m.MemberQualification, m.MemberQualification.business_record_id == m.BusinessRecord.id)
        .outerjoin(m.Member, m.Member.id == m.MemberQualification.member_id)
        .filter(m.UploadBatch.acceptance_status == "已承接", m.UploadBatch.redemption_mode == "MALL_REDEMPTION",
                m.BusinessRecord.redemption_mode == "MALL_REDEMPTION", m.BusinessRecord.user_id == m.UploadBatch.user_id))
    if batch_id:
        query = query.filter(m.UploadBatch.id == batch_id)
    rows = query.order_by(m.UploadBatch.id.desc(), m.BusinessRecord.id).offset((page-1)*PAGE_SIZE).limit(PAGE_SIZE+1).all()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def history(db, qualification_id, page):
    rows = (db.query(m.MemberQualificationEvent, m.User.username)
        .outerjoin(m.User, m.User.id == m.MemberQualificationEvent.actor_id)
        .filter(m.MemberQualificationEvent.qualification_id == qualification_id)
        .order_by(m.MemberQualificationEvent.id.desc()).offset((page-1)*PAGE_SIZE).limit(PAGE_SIZE+1).all())
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def describe_state(value):
    data = json.loads(value)
    if not data:
        return "尚未确认"
    enabled = "启用" if data["enabled"] else "停用"
    binding = "已绑定会员 #" + str(data["member_id"]) if data["member_id"] else "待绑定"
    return f'{enabled}；{binding}；{data["valid_from"]} 至 {data["valid_until"]} 前；版本 {data["version"]}'
