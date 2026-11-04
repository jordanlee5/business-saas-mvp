"""Member rights requests are handled manually. A decision never mutates the member account."""
from datetime import datetime
from uuid import uuid4

from ..admin_permissions import can_manage_mall_orders
from ..models import Member, MemberRightsRequest, MemberRightsEvent, User
from ..time_utils import UTC8_TIMEZONE, format_utc8

KINDS = {"ACCESS": "查阅", "CORRECT": "更正", "DELETE": "删除", "WITHDRAW": "撤回", "CLOSE": "注销"}
STATES = {"PENDING": "待身份核验", "VERIFIED": "身份已核验", "PROCESSING": "处理中",
          "COMPLETED": "已办结", "UNVERIFIED": "核验未通过"}
METHODS = {"BOUND_CONTACT": "通过既有绑定渠道人工核对", "IN_PERSON": "线下当面核对"}
ACTIONS = {"verify": "核验通过", "fail": "核验未通过", "progress": "更新进度", "finish": "记录结果"}
ALLOWED = {"PENDING": ("verify", "fail"), "VERIFIED": ("progress", "finish"),
           "PROCESSING": ("progress", "finish"), "COMPLETED": (), "UNVERIFIED": ()}
NEXT = {"verify": "VERIFIED", "fail": "UNVERIFIED", "progress": "PROCESSING", "finish": "COMPLETED"}
PAGE_SIZE = 20


def submit(db, member_id, key, kind, description):
    description = description.strip()
    if kind not in KINDS or not 1 <= len(description) <= 800:
        raise ValueError("请选择申请事项，并填写800字以内的申请说明")
    member = db.query(Member).filter_by(id=member_id).with_for_update().populate_existing().one()
    if not member.is_active:
        raise PermissionError("会员会话已失效")
    old = db.query(MemberRightsRequest).filter_by(member_id=member.id, request_key=key).one_or_none()
    if old:
        if old.kind != kind or old.description != description:
            raise ValueError("请求编号对应的申请内容有变化")
        return old, False
    case = MemberRightsRequest(public_id="RGT-" + uuid4().hex, member_id=member.id, request_key=key,
                               kind=kind, description=description, status="PENDING", version=1,
                               created_at=datetime.now(UTC8_TIMEZONE))
    db.add(case)
    db.flush()
    return case, True


def summary(case):
    return dict(public_id=case.public_id, kind=KINDS[case.kind], status=STATES[case.status],
                created_at=format_utc8(case.created_at))


def events(db, case):
    return db.query(MemberRightsEvent).filter_by(request_id=case.id).order_by(MemberRightsEvent.id).all()


def member_detail(db, case):
    return dict(**summary(case), description=case.description,
                history=[dict(action=ACTIONS[e.action], note=e.note, time=format_utc8(e.created_at))
                         for e in events(db, case)])


def update(db, public_id, actor_id, version, action, method, note):
    actor = db.query(User).filter_by(id=actor_id).with_for_update().populate_existing().one_or_none()
    if not actor or not actor.is_active or not can_manage_mall_orders(actor):
        raise PermissionError("无权办理权益申请")
    case = db.query(MemberRightsRequest).filter_by(public_id=public_id).with_for_update().one_or_none()
    if not case:
        raise ValueError("申请不存在")
    if case.version != version or action not in ALLOWED[case.status]:
        raise ValueError("申请进度已变化，请刷新后核对")
    verifying = action in ("verify", "fail")
    if verifying and method not in METHODS:
        raise ValueError("请登记实际使用的人工核验方式")
    note = "已完成人工身份核验" if action == "verify" else note.strip()
    if not 1 <= len(note) <= 800:
        raise ValueError("请填写800字以内的会员可见说明")
    db.add(MemberRightsEvent(request_id=case.id, actor_id=actor.id, action=action, note=note,
                             verification_method=method if verifying else None, created_at=datetime.now(UTC8_TIMEZONE)))
    case.status = NEXT[action]
    case.version += 1
    db.flush()
