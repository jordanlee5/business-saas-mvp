"""Policy-gated delivery-snapshot erasure; never erases accounting evidence.

No default retention period. An explicit per-order review is also required,
so an unspecified legal hold or unresolved after-sales case fails closed.
Caller owns the transaction and must lock the order before applying.
"""
from datetime import datetime, timedelta
from ..models import OrderDeliverySnapshot, OrderPrivacyReceipt
from ..time_utils import UTC8_TIMEZONE

TERMINAL = {'COMPLETED': 'completed_at', 'REFUNDED': 'refunded_at', 'CANCELLED': 'updated_at'}


def aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC8_TIMEZONE) if value.tzinfo is None else value.astimezone(UTC8_TIMEZONE)


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {'version', 'approved', 'days'}:
        raise ValueError('保存规则格式不正确')
    if not isinstance(policy['version'], str) or not 1 <= len(policy['version']) <= 80:
        raise ValueError('规则版本无效')
    if type(policy['approved']) is not bool or not isinstance(policy['days'],dict) or set(policy['days']) != set(TERMINAL):
        raise ValueError('保存规则不完整')
    if any(type(n) is not int or not 1 <= n <= 36500 for n in policy['days'].values()):
        raise ValueError('保存期限必须显式填写正整数天数')


def review_fingerprint(order):
    # No recipient or other PII. Changes after manual review invalidate it.
    return {'status': order.status, 'updated_at': aware(order.updated_at).isoformat(),
            'terminal_at': aware(getattr(order, TERMINAL.get(order.status, 'created_at'))).isoformat()
            if getattr(order, TERMINAL.get(order.status, 'created_at')) else None}


def preview(db, order, policy, reviews, now):
    validate_policy(policy)
    receipt = db.get(OrderPrivacyReceipt, order.id)
    snapshot = db.get(OrderDeliverySnapshot, order.id)
    result = {'order_public_id': order.order_public_id, 'status': order.status,
              'eligible': False, 'reason': '', 'due_at': None, 'review': review_fingerprint(order)}
    if receipt:
        result['reason'] = 'already_cleared' if not snapshot else 'restore_requires_replay'
    elif not snapshot:
        result['reason'] = 'no_snapshot'
    elif not policy['approved']:
        result['reason'] = 'policy_unapproved'
    elif order.status not in TERMINAL:
        result['reason'] = 'active_order'
    else:
        terminal = aware(getattr(order, TERMINAL[order.status]))
        if terminal is None:
            result['reason'] = 'missing_terminal_time'
            return result
        due = terminal + timedelta(days=policy['days'][order.status])
        result['due_at'] = due.isoformat()
        reviewed = reviews.get(order.order_public_id)
        if not reviewed or reviewed.get('clearance') != 'NO_HOLD_OR_OPEN_CASE' or reviewed.get('order') != result['review']:
            result['reason'] = 'review_required'
        elif aware(now) < due:
            result['reason'] = 'not_due'
        else:
            result.update(eligible=True, reason='due_and_reviewed')
    return result


def clear_snapshot(db, order, policy, reviews, now):
    """Run under the same order row/write lock as state verification."""
    result = preview(db, order, policy, reviews, now)
    if result['reason'] == 'already_cleared':
        return result
    if not result['eligible']:
        raise ValueError('收货信息尚不满足清理条件')
    snapshot = db.get(OrderDeliverySnapshot, order.id)
    db.add(OrderPrivacyReceipt(order_id=order.id, request_digest=snapshot.request_digest,
                               policy_version=policy['version'], cleared_at=aware(now)))
    db.delete(snapshot)
    db.flush()
    return {**result, 'eligible': False, 'reason': 'cleared'}


def replay_receipt(db, order, receipt):
    """Reapply an existing receipt after restore; never apply to another order."""
    snapshot = db.get(OrderDeliverySnapshot, order.id)
    if receipt.order_id != order.id or order.status not in TERMINAL:
        raise ValueError('恢复清理记录与订单不匹配')
    if snapshot:
        if snapshot.request_digest != receipt.request_digest:
            raise ValueError('恢复副本与清理记录不匹配')
        db.delete(snapshot)
        db.flush()


def export_receipts(db):
    from ..models import Order
    return [{'order_public_id': order.order_public_id, 'request_digest': receipt.request_digest,
             'policy_version': receipt.policy_version, 'cleared_at': aware(receipt.cleared_at).isoformat()}
            for receipt, order in db.query(OrderPrivacyReceipt, Order).join(Order, Order.id == OrderPrivacyReceipt.order_id).order_by(Order.id)]


def restore_receipts(db, records):
    """Offline restore step using the operator's separately protected journal.

    This is evidence import, not an untrusted network API. A restored active or
    different order fails closed; finish restoring business events first.
    """
    from ..models import Order
    import re
    if not isinstance(records,list):raise ValueError('清理凭据格式无效')
    for entry in records:
        if not isinstance(entry,dict) or set(entry)!={'order_public_id','request_digest','policy_version','cleared_at'}:
            raise ValueError('清理凭据格式无效')
        if not re.fullmatch(r'[a-f0-9]{64}',str(entry['request_digest'])) or not isinstance(entry['policy_version'],str) or not 1<=len(entry['policy_version'])<=80:
            raise ValueError('清理凭据字段无效')
        erased=datetime.fromisoformat(entry['cleared_at'])
        if erased.tzinfo is None:raise ValueError('清理凭据缺少时区')
        order=db.query(Order).filter_by(order_public_id=entry['order_public_id']).with_for_update().one_or_none()
        if order is None or order.status not in TERMINAL:raise ValueError('清理凭据与恢复订单不匹配')
        receipt=db.get(OrderPrivacyReceipt,order.id)
        if receipt and (receipt.request_digest!=entry['request_digest'] or receipt.policy_version!=entry['policy_version'] or aware(receipt.cleared_at)!=aware(erased)):
            raise ValueError('清理凭据冲突')
        if not receipt:
            receipt=OrderPrivacyReceipt(order_id=order.id,request_digest=entry['request_digest'],policy_version=entry['policy_version'],cleared_at=erased)
            if not db.get(OrderDeliverySnapshot,order.id):raise ValueError('恢复订单缺少可核对的收货快照')
            db.add(receipt)
        replay_receipt(db,order,receipt)
    db.flush()
