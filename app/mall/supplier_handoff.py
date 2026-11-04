"""Audited operator preparation and manual registration of an offline handoff."""
from contextlib import contextmanager
from datetime import datetime
import re

from sqlalchemy.orm import Session, aliased

from ..admin_permissions import can_perform_mall_audit_action
from ..models import AdminActionLog, Order, User
from ..time_utils import UTC8_TIMEZONE
from .audit import MallAuditActionType
from .fulfillment_notice import supplier_packet


PREPARE = MallAuditActionType.PACKET_PREPARE
CONFIRM = MallAuditActionType.PACKET_HANDOFF
RECEIPT_ACTIONS = {"returned": MallAuditActionType.PACKET_RETURN, "deleted": MallAuditActionType.PACKET_DELETE}


@contextmanager
def _locked_order(engine, actor_id, order_public_id, action):
    with engine.begin() as connection, Session(bind=connection) as db:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        actor = db.query(User).filter_by(id=actor_id).with_for_update().one_or_none()
        if not actor or not actor.is_active or not can_perform_mall_audit_action(actor, action):
            raise PermissionError("当前账号无权操作备货资料")
        order = db.query(Order).filter_by(order_public_id=order_public_id).with_for_update().one_or_none()
        if order is None:
            raise ValueError("订单不存在")
        yield db, actor, order


def handle_packet(engine, actor_id, order_public_id, supplier_public_id, *, preparation_id=None):
    action = PREPARE if preparation_id is None else CONFIRM
    with _locked_order(engine, actor_id, order_public_id, action) as (db, actor, order):
        logs = db.query(AdminActionLog)
        if preparation_id is None:
            packet = supplier_packet(db, order, supplier_public_id)
            receipt = AdminActionLog(admin_id=actor.id, action_type=action.value,
                                     target_type="mall_order", target_id=order.id,
                                     description=supplier_public_id, created_at=datetime.now(UTC8_TIMEZONE))
            db.add(receipt)
            db.flush()
            return receipt.id, packet

        # Confirm the operator's own specific preparation, even if shipping happened
        # in between. Recording an earlier handoff never reads the address again.
        preparation = logs.filter_by(id=preparation_id, admin_id=actor.id, action_type=PREPARE.value,
                                     target_type="mall_order", target_id=order.id,
                                     description=supplier_public_id).one_or_none()
        if preparation is None:
            raise ValueError("找不到本账号对应的备货记录")
        existing = logs.filter_by(action_type=CONFIRM.value, target_type="mall_packet_preparation",
                                  target_id=preparation.id).one_or_none()
        if existing is None:
            db.add(AdminActionLog(admin_id=actor.id, action_type=CONFIRM.value,
                                  target_type="mall_packet_preparation", target_id=preparation.id,
                                  description=supplier_public_id, created_at=datetime.now(UTC8_TIMEZONE)))
            db.flush()
        return preparation.id, None


def record_disposition(engine, actor_id, order_public_id, supplier_public_id, handoff_id, outcome, reference):
    action = RECEIPT_ACTIONS.get(outcome)
    if action is None:
        raise ValueError("请选择资料返还或删除")
    reference = reference.strip()
    if not re.fullmatch(r"[\w][\w./-]{0,79}", reference):
        raise ValueError("请填写有效的线下回执编号（最多80个字符）")
    with _locked_order(engine, actor_id, order_public_id, action) as (db, actor, order):
        preparation = aliased(AdminActionLog)
        handoff = (db.query(AdminActionLog).join(preparation, preparation.id == AdminActionLog.target_id)
                   .filter(AdminActionLog.id == handoff_id, AdminActionLog.action_type == CONFIRM.value,
                           AdminActionLog.target_type == "mall_packet_preparation",
                           AdminActionLog.description == supplier_public_id,
                           preparation.action_type == PREPARE.value, preparation.target_type == "mall_order",
                           preparation.target_id == order.id, preparation.description == supplier_public_id).one_or_none())
        if handoff is None:
            raise ValueError("找不到本单该供应商已确认的交付记录")
        existing = db.query(AdminActionLog).filter_by(action_type=action.value, target_type="mall_packet_handoff",
                                                     target_id=handoff.id).one_or_none()
        if existing:
            if existing.description != reference:
                raise ValueError("此项已登记其他回执编号，请核对原记录")
            return existing.id
        receipt = AdminActionLog(admin_id=actor.id, action_type=action.value, target_type="mall_packet_handoff",
                                 target_id=handoff.id, description=reference, created_at=datetime.now(UTC8_TIMEZONE))
        db.add(receipt)
        db.flush()
        return receipt.id


def packet_history(db, order_public_id):
    confirmation = aliased(AdminActionLog)
    return (db.query(AdminActionLog.id, AdminActionLog.description.label("supplier"),
                     AdminActionLog.admin_id, AdminActionLog.created_at,
                     confirmation.created_at.label("confirmed_at"), confirmation.id.label("handoff_id"))
            .outerjoin(confirmation, (confirmation.target_id == AdminActionLog.id)
                       & (confirmation.target_type == "mall_packet_preparation")
                       & (confirmation.action_type == CONFIRM.value)
                       & (confirmation.description == AdminActionLog.description))
            .join(Order, Order.id == AdminActionLog.target_id)
            .filter(AdminActionLog.target_type == "mall_order", Order.order_public_id == order_public_id,
                    AdminActionLog.action_type == PREPARE.value)
            .order_by(AdminActionLog.id.desc()).all())


def receipts_by_handoff(db, history):
    handoff_ids = [row.handoff_id for row in history if row.handoff_id is not None]
    receipts = {key: {} for key in handoff_ids}
    if handoff_ids:
        outcomes = {action.value: outcome for outcome, action in RECEIPT_ACTIONS.items()}
        rows = (db.query(AdminActionLog.target_id, AdminActionLog.action_type, AdminActionLog.description,
                         AdminActionLog.admin_id, AdminActionLog.created_at, User.username)
                .outerjoin(User, User.id == AdminActionLog.admin_id)
                .filter(AdminActionLog.target_type == "mall_packet_handoff", AdminActionLog.target_id.in_(handoff_ids),
                        AdminActionLog.action_type.in_(outcomes)).all())
        for row in rows:
            receipts[row.target_id][outcomes[row.action_type]] = row
    return receipts
