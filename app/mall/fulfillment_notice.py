"""Explicitly published fulfillment profiles, never internal supplier contacts."""
import hashlib
import json
import os
from pathlib import Path

from ..models import Product, ProductSku, Supplier, OrderItem, OrderFulfillmentNotice

NOTICE = '您向本公司兑换商品，由受托供应商发货。仅向负责本次订单的受托方提供必要的收件人姓名、电话、地址及商品和订单信息，用于配送及必要售后，不用于供应商自行营销。'


def settings():
    path = os.environ.get('MALL_FULFILLMENT_SETTINGS', '')
    if not path:
        return None
    try:
        source = Path(path)
        if source.stat().st_size > 131072:
            raise ValueError()
        data = json.loads(source.read_text('utf-8'))
        if set(data) != {'operator_name', 'operator_contact', 'suppliers'}:
            raise ValueError()
        for key in ('operator_name', 'operator_contact'):
            if not isinstance(data[key], str) or not 1 <= len(data[key].strip()) <= 200:
                raise ValueError()
        if not isinstance(data['suppliers'], dict):
            raise ValueError()
        for public_id, profile in data['suppliers'].items():
            if not isinstance(public_id, str) or not public_id or len(public_id) > 32:
                raise ValueError()
            if set(profile) != {'name', 'agreement_ref', 'enabled'}:
                raise ValueError()
            if type(profile['enabled']) is not bool:
                raise ValueError()
            for key in ('name', 'agreement_ref'):
                if not isinstance(profile[key], str) or not 1 <= len(profile[key].strip()) <= 200:
                    raise ValueError()
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeError('受托发货配置不可用') from None


def checkout_notice(db, lines):
    config = settings()
    if not config:
        return {'ready': False, 'message': '发货服务信息待完善，请稍后再试', 'notice': NOTICE,
                'operator_name': '', 'operator_contact': '', 'suppliers': [], 'digest': ''}
    groups = {}
    for line in lines:
        row = (db.query(ProductSku, Product, Supplier)
               .join(Product, Product.id == ProductSku.product_id)
               .join(Supplier, Supplier.id == ProductSku.supplier_id)
               .filter(Product.product_public_id == line['product_public_id'],
                       ProductSku.sku_code == line['sku_code']).one_or_none())
        if row is None:
            raise ValueError('商品规格已不可用')
        sku, product, supplier = row
        if not sku.is_active or not supplier.is_active or product.status != 'PUBLISHED':
            raise ValueError('商品规格已不可用')
        profile = config['suppliers'].get(supplier.supplier_public_id)
        if not profile or not profile['enabled']:
            return {'ready': False, 'message': '部分商品的受托发货资料待完善，请返回购物车核对',
                    'notice': NOTICE, 'operator_name': config['operator_name'],
                    'operator_contact': config['operator_contact'], 'suppliers': [], 'digest': ''}
        group = groups.setdefault(supplier.supplier_public_id, {
            'supplier_public_id': supplier.supplier_public_id,
            'name': profile['name'], 'contact': config['operator_contact'], 'items': []})
        group['items'].append({'product_public_id': product.product_public_id,
                               'sku_code': sku.sku_code, 'product_name': product.name,
                               'sku_name': sku.name, 'quantity': line['quantity']})
    payload = {'ready': True, 'message': '', 'notice': NOTICE,
               'operator_name': config['operator_name'], 'operator_contact': config['operator_contact'],
               'suppliers': [groups[key] for key in sorted(groups)]}
    for group in payload['suppliers']:
        group['items'].sort(key=lambda x: (x['product_public_id'], x['sku_code']))
    # Include agreement revisions in the fingerprint without publishing contract IDs.
    evidence = {key: config['suppliers'][key]['agreement_ref'] for key in groups}
    payload['digest'] = hashlib.sha256(json.dumps([payload, evidence], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload


def save_notice(db, order, notice):
    expected = {(g['supplier_public_id'], i['product_public_id'], i['sku_code'], i['quantity'])
                for g in notice['suppliers'] for i in g['items']}
    actual = {(i.supplier_public_id_snapshot, i.product_public_id_snapshot, i.sku_code_snapshot, i.quantity)
              for i in db.query(OrderItem).filter_by(order_id=order.id)}
    if actual != expected:
        raise ValueError('发货安排已变化，请重新核对')
    db.add(OrderFulfillmentNotice(order_id=order.id, digest=notice['digest'],
                                 payload=json.dumps(notice, ensure_ascii=False, sort_keys=True)))
    db.flush()


def historical_notice(db, order_id):
    saved = db.get(OrderFulfillmentNotice, order_id)
    return json.loads(saved.payload) if saved else None


def supplier_packet(db, order, supplier_public_id):
    """Internal authorized-operator preparation; no supplier HTTP endpoint.

    Returned data is limited to one currently enabled, contracted supplier.
    The caller must enforce operator authorization and record actual provision.
    This function alone does not send or grant a supplier access to information.
    """
    from ..models import OrderDeliverySnapshot
    from .member_address_service import ADDRESS_FIELDS
    config = settings()
    profile = config and config['suppliers'].get(supplier_public_id)
    if not profile or not profile['enabled']:
        raise ValueError('受托发货授权不可用')
    if order.status != 'FULFILLING':
        raise ValueError('仅待发货订单可准备发货资料')
    snapshot = db.get(OrderDeliverySnapshot, order.id)
    notice = historical_notice(db, order.id)
    if not snapshot or not notice or not any(g['supplier_public_id'] == supplier_public_id for g in notice['suppliers']):
        raise ValueError('订单缺少有效收货资料或告知记录')
    items = db.query(OrderItem).filter_by(order_id=order.id, supplier_public_id_snapshot=supplier_public_id).all()
    if not items:
        raise ValueError('该供应商不负责此订单')
    return {'order_public_id': order.order_public_id,
            'address': {key: getattr(snapshot, key) for key in ADDRESS_FIELDS},
            'items': [{'product_name': i.product_name_snapshot, 'sku_name': i.sku_name_snapshot,
                       'quantity': i.quantity} for i in items]}
