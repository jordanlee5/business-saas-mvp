"""订单状态的内部只读资源与业务证据预检。"""

from dataclasses import dataclass

from sqlalchemy import or_

from .audit import MallAuditActionType
from .inventory_service import assert_inventory_balance_consistent
from .order_cancellation_service import (
    _validate_inventory_evidence as _validate_cancellation_inventory,
    _validate_points_evidence as _validate_cancellation_points,
)
from .order_fulfillment_service import (
    _validate_fulfillment_audit,
    _validate_inventory_evidence as _validate_fulfillment_inventory,
    _validate_order_totals,
    _validate_points_evidence as _validate_fulfillment_points,
)
from .order_lifecycle_service import (
    _time_key,
    _validate_completion_evidence,
    _validate_shipping_evidence,
)
from .order_refund_service import _validate_refund_evidence
from .order_state_machine import OrderStatus
from .points_ledger_service import assert_points_account_balance_consistent


@dataclass(frozen=True)
class UnfulfilledOrderPreflight:
    order_id: int
    order_public_id: str
    status: str
    item_count: int
    allocation_count: int
    cancellation_actor_admin_id: int | None
    points_release_entry_ids: tuple[int, ...]
    inventory_release_movement_ids: tuple[int, ...]


@dataclass(frozen=True)
class InProgressOrderPreflight:
    order_id: int
    order_public_id: str
    status: str
    item_count: int
    allocation_count: int
    fulfillment_actor_admin_id: int
    fulfillment_action_log_id: int
    shipping_action_log_id: int | None
    points_consume_entry_ids: tuple[int, ...]
    inventory_outbound_movement_ids: tuple[int, ...]


def inspect_unfulfilled_order_state(db, *, order_public_id) -> UnfulfilledOrderPreflight:
    """只读核对预占及取消证据；发现不一致时失败关闭，不尝试修复。"""
    from ..models import (
        AdminActionLog,
        Order,
        OrderItem,
        OrderPointsGrantAllocation,
        PointsAccount,
        PointsGrant,
    )

    if not isinstance(order_public_id, str) or not order_public_id.strip():
        raise ValueError("订单编号不能为空")
    normalized_id = order_public_id.strip()
    if len(normalized_id) > 32:
        raise ValueError("订单编号不能超过 32 个字符")
    if db.new or db.dirty or db.deleted:
        raise ValueError("订单预检需要无待写入变更的数据库会话")

    with db.no_autoflush:
        order = db.query(Order).filter(Order.order_public_id == normalized_id).one_or_none()
        if order is None:
            raise ValueError("订单不存在")
        if order.status not in (OrderStatus.CREATED.value, OrderStatus.CANCELLED.value):
            raise ValueError("仅支持待处理或已取消订单的只读预检")

        items = tuple(
            db.query(OrderItem).filter(OrderItem.order_id == order.id)
            .order_by(OrderItem.sku_id.asc()).all()
        )
        allocations = tuple(
            db.query(OrderPointsGrantAllocation)
            .filter(OrderPointsGrantAllocation.order_id == order.id)
            .order_by(OrderPointsGrantAllocation.points_grant_id.asc()).all()
        )
        _validate_order_totals(order, items, allocations)

        account = db.query(PointsAccount).filter(
            PointsAccount.member_id == order.member_id
        ).one_or_none()
        if account is None:
            raise RuntimeError("订单会员积分账户不存在")
        grant_ids = {allocation.points_grant_id for allocation in allocations}
        grants = tuple(db.query(PointsGrant).filter(PointsGrant.id.in_(grant_ids)).all())
        if len(grants) != len(grant_ids) or any(
            grant.account_id != account.id for grant in grants
        ):
            raise RuntimeError("订单积分批次证据不完整")
        assert_points_account_balance_consistent(db, account_id=account.id)
        for sku_id in sorted({item.sku_id for item in items}):
            assert_inventory_balance_consistent(db, sku_id=sku_id)

        cancellation_logs = tuple(
            db.query(AdminActionLog).filter(
                AdminActionLog.action_type == MallAuditActionType.ORDER_CANCEL.value,
                AdminActionLog.target_type == "mall_order",
                AdminActionLog.target_id == order.id,
            ).order_by(AdminActionLog.id.asc()).all()
        )
        cancelled = order.status == OrderStatus.CANCELLED.value
        if len(cancellation_logs) > 1 or (not cancelled and cancellation_logs):
            raise RuntimeError("订单取消审计证据异常")
        cancellation_actor_id = None
        if cancellation_logs:
            log = cancellation_logs[0]
            prefix = f"订单 {order.order_public_id} 取消："
            if (
                log.admin_id is None
                or not isinstance(log.description, str)
                or not log.description.startswith(prefix)
                or len(log.description) <= len(prefix)
            ):
                raise RuntimeError("订单取消审计证据异常")
            cancellation_actor_id = log.admin_id

        _validate_fulfillment_audit(db, order=order, expect_fulfilled=False)
        _validate_shipping_evidence(db, order=order, expect_shipped=False)
        _validate_completion_evidence(db, order=order, expect_completed=False)
        _validate_refund_evidence(db, order=order, expect_refunded=False)
        inventory_ids = _validate_cancellation_inventory(
            db, order=order, items=items, expect_released=cancelled,
            actor_admin_id=cancellation_actor_id,
        )
        points_ids = _validate_cancellation_points(
            db, order=order, allocations=allocations, expect_released=cancelled,
            actor_admin_id=cancellation_actor_id,
        )
        return UnfulfilledOrderPreflight(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            item_count=len(items),
            allocation_count=len(allocations),
            cancellation_actor_admin_id=cancellation_actor_id,
            points_release_entry_ids=points_ids,
            inventory_release_movement_ids=inventory_ids,
        )


def inspect_in_progress_order_state(db, *, order_public_id) -> InProgressOrderPreflight:
    """只读核对待发货或已发货订单；异常时失败关闭。"""
    from ..models import (
        AdminActionLog,
        Order,
        OrderItem,
        OrderPointsGrantAllocation,
        PointsAccount,
        PointsGrant,
        SupplierSettlementItem,
    )

    if not isinstance(order_public_id, str) or not order_public_id.strip():
        raise ValueError("订单编号不能为空")
    normalized_id = order_public_id.strip()
    if len(normalized_id) > 32:
        raise ValueError("订单编号不能超过 32 个字符")
    if db.new or db.dirty or db.deleted:
        raise ValueError("订单预检需要无待写入变更的数据库会话")

    with db.no_autoflush:
        order = db.query(Order).filter(Order.order_public_id == normalized_id).one_or_none()
        if order is None:
            raise ValueError("订单不存在")
        if order.status not in (OrderStatus.FULFILLING.value, OrderStatus.SHIPPED.value):
            raise ValueError("仅支持待发货或已发货订单的只读预检")

        items = tuple(
            db.query(OrderItem).filter(OrderItem.order_id == order.id)
            .order_by(OrderItem.sku_id.asc()).all()
        )
        allocations = tuple(
            db.query(OrderPointsGrantAllocation)
            .filter(OrderPointsGrantAllocation.order_id == order.id)
            .order_by(OrderPointsGrantAllocation.points_grant_id.asc()).all()
        )
        _validate_order_totals(order, items, allocations)

        account = db.query(PointsAccount).filter(
            PointsAccount.member_id == order.member_id
        ).one_or_none()
        if account is None:
            raise RuntimeError("订单会员积分账户不存在")
        grant_ids = {allocation.points_grant_id for allocation in allocations}
        grants = tuple(db.query(PointsGrant).filter(PointsGrant.id.in_(grant_ids)).all())
        if len(grants) != len(grant_ids) or any(
            grant.account_id != account.id for grant in grants
        ):
            raise RuntimeError("订单积分批次证据不完整")
        assert_points_account_balance_consistent(db, account_id=account.id)
        for sku_id in sorted({item.sku_id for item in items}):
            assert_inventory_balance_consistent(db, sku_id=sku_id)

        if db.query(AdminActionLog.id).filter(
            AdminActionLog.action_type == MallAuditActionType.ORDER_CANCEL.value,
            AdminActionLog.target_type == "mall_order",
            AdminActionLog.target_id == order.id,
        ).first():
            raise RuntimeError("待发货或已发货订单存在取消审计")
        if db.query(SupplierSettlementItem.id).filter(or_(
            SupplierSettlementItem.order_id == order.id,
            SupplierSettlementItem.order_item_id.in_(item.id for item in items),
        )).first():
            raise RuntimeError("待发货或已发货订单提前进入供应商结算")

        fulfillment_log = _validate_fulfillment_audit(
            db, order=order, expect_fulfilled=True,
        )
        inventory_ids = _validate_fulfillment_inventory(
            db, order=order, items=items, expect_fulfilled=True,
            fulfillment_actor_admin_id=fulfillment_log.admin_id,
        )
        points_ids = _validate_fulfillment_points(
            db, order=order, allocations=allocations, expect_fulfilled=True,
            fulfillment_actor_admin_id=fulfillment_log.admin_id,
        )
        shipping_log = _validate_shipping_evidence(
            db, order=order, expect_shipped=order.status == OrderStatus.SHIPPED.value,
        )
        if shipping_log is not None and (
            _time_key(order.shipped_at) < _time_key(fulfillment_log.created_at)
        ):
            raise RuntimeError("订单发货时间早于确认履约时间")
        _validate_completion_evidence(db, order=order, expect_completed=False)
        _validate_refund_evidence(db, order=order, expect_refunded=False)

        return InProgressOrderPreflight(
            order_id=order.id,
            order_public_id=order.order_public_id,
            status=order.status,
            item_count=len(items),
            allocation_count=len(allocations),
            fulfillment_actor_admin_id=fulfillment_log.admin_id,
            fulfillment_action_log_id=fulfillment_log.id,
            shipping_action_log_id=shipping_log.id if shipping_log else None,
            points_consume_entry_ids=points_ids,
            inventory_outbound_movement_ids=inventory_ids,
        )
