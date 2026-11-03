"""Audited operator preparation and manual registration of an offline handoff."""
from sqlalchemy.orm import Session

from ..admin_permissions import can_perform_mall_audit_action
from ..models import AdminActionLog, Order, User
from .audit import MallAuditActionType
from .fulfillment_notice import supplier_packet


PREPARE = MallAuditActionType.PACKET_PREPARE
CONFIRM = MallAuditActionType.PACKET_HANDOFF


def handle_packet(engine, actor_id, order_public_id, supplier_public_id, *, preparation_id=None):
    action = PREPARE if preparation_id is None else CONFIRM
    with engine.begin() as connection, Session(bind=connection) as db:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        actor = db.query(User).filter_by(id=actor_id).with_for_update().one_or_none()
        if not actor or not actor.is_active or not can_perform_mall_audit_action(actor, action):
            raise PermissionError("当前账号无权操作备货资料")
        order = db.query(Order).filter_by(order_public_id=order_public_id).with_for_update().one_or_none()
        if order is None:
            raise ValueError("订单不存在")
        logs = db.query(AdminActionLog)
        if preparation_id is None:
            packet = supplier_packet(db, order, supplier_public_id)
            receipt = AdminActionLog(admin_id=actor.id, action_type=action.value,
                                     target_type="mall_order", target_id=order.id,
                                     description=supplier_public_id)
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
                                  description=supplier_public_id))
            db.flush()
        return preparation.id, None


def packet_history(db, order_public_id):
    from sqlalchemy.orm import aliased
    confirmation = aliased(AdminActionLog)
    return (db.query(AdminActionLog.id, AdminActionLog.description.label("supplier"),
                     AdminActionLog.admin_id, AdminActionLog.created_at,
                     confirmation.created_at.label("confirmed_at"))
            .outerjoin(confirmation, (confirmation.target_id == AdminActionLog.id)
                       & (confirmation.target_type == "mall_packet_preparation")
                       & (confirmation.action_type == CONFIRM.value))
            .join(Order, Order.id == AdminActionLog.target_id)
            .filter(AdminActionLog.target_type == "mall_order", Order.order_public_id == order_public_id,
                    AdminActionLog.action_type == PREPARE.value)
            .order_by(AdminActionLog.id.desc()).all())
