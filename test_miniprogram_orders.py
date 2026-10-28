import os
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import test_order_service as fixtures
from app.api import miniprogram_v1_router
from app.models import Member, MemberWechatBinding, Order, OrderDeliverySnapshot, InventoryBalance, PointsAccount
from app.mall.member_address_service import create_member_address, update_member_address, delete_member_address
from app.mall.miniprogram_session_service import issue_session
from app.mall.order_reporting_service import get_mall_order_detail

BODY={'recipient_name':'隔离收件人','phone':'13800000001','province':'测试省','city':'测试市','district':'测试区','detail':'测试路1号'}
APPID='wx1234567890abcdef'
PREFIX='/api/miniprogram/v1/orders'
class MemberOrderTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OrderServiceTests('runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
        for name,value in [('app.api.member_addresses.engine',self.f.engine),('app.mall.order_service.utc8_now',lambda:fixtures.NOW),('app.mall.order_cancellation_service.utc8_now',lambda:fixtures.NOW)]:
            p=patch(name,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'WECHAT_MINIPROGRAM_APP_ID':APPID,'MINIPROGRAM_SESSION_SIGNING_KEY':'orders-test-only-signing-key-20261028'});p.start();self.addCleanup(p.stop)
        self.tokens=[]
        with Session(self.f.engine) as db:
            other=Member(member_public_id='MEM-OTHER-ORDER',is_active=True);db.add(other);db.flush()
            for i,mid in enumerate((self.f.member.id,other.id)):
                b=MemberWechatBinding(member_id=mid,wechat_app_id=APPID,openid=f'order-openid-{i}');db.add(b);db.flush()
                self.tokens.append(issue_session(b.id,APPID,b.openid)[0])
            self.address=create_member_address(db,self.f.member.id,BODY);db.commit()
        app=FastAPI();app.include_router(miniprogram_v1_router);self.client=TestClient(app);self.addCleanup(self.client.close)
        self.body={'request_id':'a'*32,'address_public_id':self.address['address_public_id'],'expected_address':dict(BODY),
                   'lines':[{'product_public_id':'PRD-ORDER-001','sku_code':'ORDER-SKU-001','quantity':2,'expected_points_price':'40.00'}]}
        from app.models import Supplier
        with Session(self.f.engine) as db:
            self.fulfillment_config={'operator_name':'测试兑换公司','operator_contact':'operator@example.invalid',
                'suppliers':{s.supplier_public_id:{'name':'公开受托方 '+s.supplier_public_id,
                    'agreement_ref':'test-only-v1','enabled':True}
                    for s in db.query(Supplier)}}
        config_path=self.f.path.parent/'fulfillment.json'
        config_path.write_text(json.dumps(self.fulfillment_config),encoding='utf-8')
        p=patch.dict(os.environ,{'MALL_FULFILLMENT_SETTINGS':str(config_path)});p.start();self.addCleanup(p.stop)
        self.body['fulfillment_digest']=self.call('POST','/fulfillment-preview',{'lines':self.body['lines']}).json()['digest']
    def call(self,method,path='',body=None,owner=0):
        r=self.client.request(method,PREFIX+path,headers={} if owner is None else {'Authorization':'Bearer '+self.tokens[owner]},**({'json':body} if body is not None else {}))
        self.assertEqual(r.headers['cache-control'],'no-store, private');return r
    def place(self):
        r=self.call('POST',body=self.body);self.assertEqual(r.status_code,201,r.text);return r.json()
    def test_checkout_snapshot_and_private_response(self):
        result=self.place();self.assertEqual(result['total_points'],'80.00');self.assertEqual(result['address'],BODY)
        self.assertNotIn('cost',str(result));self.assertEqual(self.call('GET').json()['total'],1)
        with Session(self.f.engine) as db:
            self.assertEqual(db.query(OrderDeliverySnapshot).count(),1)
            self.assertEqual(db.get(PointsAccount,self.f.account.id).reserved_points,Decimal('80'))
            self.assertEqual(get_mall_order_detail(db,order_public_id=result['order_public_id']).delivery_address,BODY)
    def test_edit_and_delete_address_do_not_change_snapshot_or_replay(self):
        result=self.place()
        with Session(self.f.engine) as db:
            update_member_address(db,self.f.member.id,self.address['address_public_id'],{**BODY,'detail':'新地址'})
            delete_member_address(db,self.f.member.id,self.address['address_public_id']);db.commit()
        self.assertEqual(self.call('GET','/'+result['order_public_id']).json()['address'],BODY)
        replay=self.call('POST',body=self.body);self.assertEqual(replay.status_code,200);self.assertEqual(replay.json()['order_public_id'],result['order_public_id'])
    def test_request_conflict_and_lookup(self):
        result=self.place();self.assertEqual(self.call('GET','/request/'+'a'*32).json()['order_public_id'],result['order_public_id'])
        self.body['lines'][0]['quantity']=1
        self.assertEqual(self.call('POST',body=self.body).status_code,409)
    def test_other_member_and_unknown_orders_are_404(self):
        result=self.place()
        for suffix in ['/'+result['order_public_id'],'/request/'+'a'*32]:self.assertEqual(self.call('GET',suffix,owner=1).status_code,404)
        self.assertEqual(self.call('POST','/'+result['order_public_id']+'/cancel',owner=1).status_code,404)
        self.assertEqual(self.call('POST',body=self.body,owner=1).status_code,404)
    def test_auth_and_validation_hide_private_values(self):
        self.assertEqual(self.call('POST',body=self.body,owner=None).status_code,401)
        self.body['expected_address']['phone']='PRIVATE_INVALID'
        r=self.call('POST',body=self.body);self.assertEqual(r.status_code,422);self.assertNotIn('PRIVATE_INVALID',r.text)
        self.body['member_id']=999
        r=self.call('POST',body=self.body);self.assertEqual(r.status_code,422);self.assertNotIn('PRIVATE_INVALID',r.text)
    def test_price_change_rolls_back_everything(self):
        self.body['lines'][0]['expected_points_price']='39.00'
        self.assertEqual(self.call('POST',body=self.body).status_code,409)
        with Session(self.f.engine) as db:
            self.assertEqual(db.query(Order).count(),0);self.assertEqual(db.query(OrderDeliverySnapshot).count(),0)
            self.assertEqual(db.get(PointsAccount,self.f.account.id).reserved_points,0)
            self.assertTrue(all(x.reserved_quantity==0 for x in db.query(InventoryBalance)))
    def test_stale_address_rejected_without_order(self):
        self.body['expected_address']={**BODY,'detail':'旧地址'}
        self.assertEqual(self.call('POST',body=self.body).status_code,409)
        self.assertEqual(self.call('GET').json()['total'],0)
    def test_stock_and_points_insufficiency_roll_back(self):
        for qty in (6,5):
            self.body['lines'][0]['quantity']=qty
            self.body['fulfillment_digest']=self.call('POST','/fulfillment-preview',{'lines':self.body['lines']}).json()['digest']
            self.assertEqual(self.call('POST',body=self.body).status_code,409)
            self.assertEqual(self.call('GET').json()['total'],0)
    def test_duplicate_lines_rejected(self):
        self.body['lines']*=2
        self.assertEqual(self.call('POST',body=self.body).status_code,422)
    def test_cancel_releases_once(self):
        result=self.place()
        for _ in range(2):self.assertEqual(self.call('POST','/'+result['order_public_id']+'/cancel').json()['status'],'CANCELLED')
        with Session(self.f.engine) as db:self.assertEqual(db.get(PointsAccount,self.f.account.id).reserved_points,0)
    def test_backend_fulfillment_shipping_completion_refund_visible(self):
        result=self.place();oid=result['order_public_id']
        for action,status in [(self.f.fulfill,'FULFILLING'),(self.f.ship,'SHIPPED'),(self.f.complete,'COMPLETED'),(self.f.refund,'REFUNDED')]:
            action(oid)
            r=self.call('GET','/'+oid);self.assertEqual(r.json()['status'],status);self.assertEqual(r.json()['address'],BODY)
        self.assertEqual(self.call('POST','/'+oid+'/cancel').status_code,409)
    def test_concurrent_same_request_has_one_order(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.call('POST',body=self.body),range(2)))
        self.assertEqual(sorted(r.status_code for r in results),[200,201])
        self.assertEqual(self.call('GET').json()['total'],1)


    def test_snapshot_failure_rolls_back_order_points_and_inventory(self):
        with patch('app.api.member_orders.OrderDeliverySnapshot',side_effect=RuntimeError('test-only failure')):
            self.assertEqual(self.call('POST',body=self.body).status_code,503)
        with Session(self.f.engine) as db:
            self.assertEqual(db.query(Order).count(),0)
            self.assertEqual(db.get(PointsAccount,self.f.account.id).reserved_points,0)
            self.assertTrue(all(x.reserved_quantity==0 for x in db.query(InventoryBalance)))

    def test_notice_requires_auth_and_never_publishes_internal_contacts(self):
        from app.models import Supplier
        with Session(self.f.engine) as db:
            supplier=db.get(Supplier,self.f.supplier.id)
            supplier.contact_phone='PRIVATE_INTERNAL_PHONE';supplier.contact_name='PRIVATE_INTERNAL_NAME';db.commit()
        response=self.call('POST','/fulfillment-preview',{'lines':self.body['lines']})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('PRIVATE_INTERNAL',response.text)
        self.assertNotIn('agreement_ref',response.text)
        self.assertTrue(all(g['contact']=='operator@example.invalid' for g in response.json()['suppliers']))
        self.assertEqual(self.call('POST','/fulfillment-preview',{'lines':self.body['lines']},owner=None).status_code,401)

    def test_notice_unconfigured_or_stale_blocks_new_order(self):
        with patch('app.mall.fulfillment_notice.settings',return_value=None):
            self.assertFalse(self.call('POST','/fulfillment-preview',{'lines':self.body['lines']}).json()['ready'])
            self.assertEqual(self.call('POST',body=self.body).status_code,409)
        self.body['fulfillment_digest']='0'*64
        self.assertEqual(self.call('POST',body=self.body).status_code,409)
        self.assertEqual(self.call('GET').json()['total'],0)

    def test_notice_history_survives_profile_changes_and_revocation(self):
        result=self.place()
        changed=json.loads(json.dumps(self.fulfillment_config))
        for profile in changed['suppliers'].values():
            profile.update(name='后来的名称',enabled=False)
        with patch('app.mall.fulfillment_notice.settings',return_value=changed):
            self.assertEqual(self.call('GET','/'+result['order_public_id']).json()['fulfillment'],result['fulfillment'])
            self.assertEqual(self.call('POST',body=self.body).status_code,200)
            self.body['request_id']='b'*32
            self.assertEqual(self.call('POST',body=self.body).status_code,409)

    def _cancel_for_privacy(self):
        result=self.place();self.call('POST','/'+result['order_public_id']+'/cancel')
        return result['order_public_id']

    def test_erasure_is_policy_review_and_time_gated(self):
        from datetime import timedelta
        from app.mall.order_privacy import preview,clear_snapshot,review_fingerprint,aware
        oid=self._cancel_for_privacy()
        policy={'version':'test-only','approved':False,'days':dict(CANCELLED=10,COMPLETED=10,REFUNDED=10)}
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            now=aware(order.updated_at)+timedelta(days=11)
            self.assertEqual(preview(db,order,policy,{},now)['reason'],'policy_unapproved')
            policy['approved']=True
            self.assertEqual(preview(db,order,policy,{},now)['reason'],'review_required')
            reviews={oid:{'clearance':'NO_HOLD_OR_OPEN_CASE','order':review_fingerprint(order)}}
            self.assertEqual(preview(db,order,policy,reviews,aware(order.updated_at))['reason'],'not_due')
            reviews[oid]['clearance']='OPEN_CASE'
            with self.assertRaises(ValueError):clear_snapshot(db,order,policy,reviews,now)
            self.assertIsNotNone(db.get(OrderDeliverySnapshot,order.id))

    def test_cleared_snapshot_keeps_idempotency_and_accounting(self):
        from datetime import timedelta
        from app.models import OrderPrivacyReceipt
        from app.mall.order_privacy import clear_snapshot,review_fingerprint,aware
        oid=self._cancel_for_privacy()
        policy={'version':'test-only','approved':True,'days':dict(CANCELLED=10,COMPLETED=10,REFUNDED=10)}
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            before=(order.total_points,order.total_cost_amount,order.total_quantity,order.status)
            reviews={oid:{'clearance':'NO_HOLD_OR_OPEN_CASE','order':review_fingerprint(order)}}
            now=aware(order.updated_at)+timedelta(days=10)
            self.assertEqual(clear_snapshot(db,order,policy,reviews,now)['reason'],'cleared')
            self.assertEqual(clear_snapshot(db,order,policy,reviews,now)['reason'],'already_cleared')
            self.assertEqual((order.total_points,order.total_cost_amount,order.total_quantity,order.status),before)
            self.assertIsNone(db.get(OrderDeliverySnapshot,order.id));self.assertIsNotNone(db.get(OrderPrivacyReceipt,order.id));db.commit()
        replay=self.call('POST',body=self.body)
        self.assertEqual(replay.status_code,200)
        self.assertIsNone(replay.json()['address']);self.assertEqual(replay.json()['address_state'],'CLEARED')
        self.body['expected_address']['detail']='different'
        self.assertEqual(self.call('POST',body=self.body).status_code,409)
        with Session(self.f.engine) as db:
            self.assertEqual(db.query(Order).count(),1)
            self.assertTrue(get_mall_order_detail(db,order_public_id=oid).delivery_cleared)

    def test_active_orders_cannot_be_cleared(self):
        from datetime import timedelta
        from app.mall.order_privacy import preview,aware
        oid=self.place()['order_public_id']
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            policy={'version':'test-only','approved':True,'days':dict(CANCELLED=1,COMPLETED=1,REFUNDED=1)}
            self.assertEqual(preview(db,order,policy,{},aware(order.created_at)+timedelta(days=100))['reason'],'active_order')

    def test_restore_receipt_blocks_pii_until_replayed(self):
        from datetime import timedelta
        from app.models import OrderPrivacyReceipt
        from app.mall.order_privacy import clear_snapshot,review_fingerprint,aware,replay_receipt
        oid=self._cancel_for_privacy()
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            original=db.get(OrderDeliverySnapshot,order.id)
            values={c.name:getattr(original,c.name) for c in OrderDeliverySnapshot.__table__.columns}
            policy={'version':'test-only','approved':True,'days':dict(CANCELLED=1,COMPLETED=1,REFUNDED=1)}
            clear_snapshot(db,order,policy,{oid:{'clearance':'NO_HOLD_OR_OPEN_CASE','order':review_fingerprint(order)}},aware(order.updated_at)+timedelta(days=2))
            db.add(OrderDeliverySnapshot(**values));db.commit()
        self.assertEqual(self.call('GET','/'+oid).status_code,503)
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            replay_receipt(db,order,db.get(OrderPrivacyReceipt,order.id));db.commit()
        self.assertEqual(self.call('GET','/'+oid).json()['address_state'],'CLEARED')

    def test_supplier_packet_is_minimal_and_revocable(self):
        from app.mall.fulfillment_notice import supplier_packet
        oid=self.place()['order_public_id'];self.f.fulfill(oid)
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            packet=supplier_packet(db,order,self.f.supplier.supplier_public_id)
            self.assertEqual(set(packet),{'order_public_id','address','items'})
            self.assertEqual(set(packet['items'][0]),{'product_name','sku_name','quantity'})
            with self.assertRaises(ValueError):supplier_packet(db,order,'OTHER')
            changed=json.loads(json.dumps(self.fulfillment_config))
            for profile in changed['suppliers'].values():profile['enabled']=False
            with patch('app.mall.fulfillment_notice.settings',return_value=changed):
                with self.assertRaises(ValueError):supplier_packet(db,order,self.f.supplier.supplier_public_id)

    def test_two_suppliers_are_grouped_and_dispatch_does_not_leak_other_items(self):
        from app.models import Supplier,ProductSku
        from app.mall.fulfillment_notice import supplier_packet
        with Session(self.f.engine) as db:
            other=Supplier(supplier_public_id='SUP-SECOND',name='internal second',is_active=True)
            db.add(other);db.flush();db.get(ProductSku,self.f.sku_two.id).supplier_id=other.id;db.commit()
        self.fulfillment_config['suppliers']['SUP-SECOND']={'name':'公开第二供应商','agreement_ref':'test-2','enabled':True}
        self.body['lines'][0]['quantity']=1
        self.body['lines'].append({'product_public_id':'PRD-ORDER-001','sku_code':self.f.sku_two.sku_code,'quantity':1,'expected_points_price':f'{self.f.sku_two.points_price:.2f}'})
        with patch('app.mall.fulfillment_notice.settings',return_value=self.fulfillment_config):
            self.body['fulfillment_digest']=self.call('POST','/fulfillment-preview',{'lines':self.body['lines']}).json()['digest']
            result=self.place();self.assertEqual(len(result['fulfillment']['suppliers']),2)
            self.f.fulfill(result['order_public_id'])
            with Session(self.f.engine) as db:
                order=db.query(Order).filter_by(order_public_id=result['order_public_id']).one()
                packet=supplier_packet(db,order,'SUP-SECOND')
                self.assertEqual(len(packet['items']),1)
                self.assertEqual(packet['items'][0]['sku_name'],self.f.sku_two.name)

    def test_separate_receipt_journal_reapplies_erasure_to_older_backup(self):
        from datetime import timedelta
        from app.models import OrderPrivacyReceipt
        from app.mall.order_privacy import clear_snapshot,review_fingerprint,aware,export_receipts,restore_receipts
        oid=self._cancel_for_privacy()
        with Session(self.f.engine) as db:
            order=db.query(Order).filter_by(order_public_id=oid).one()
            original=db.get(OrderDeliverySnapshot,order.id)
            values={c.name:getattr(original,c.name) for c in OrderDeliverySnapshot.__table__.columns}
            policy={'version':'test-only','approved':True,'days':dict(CANCELLED=1,COMPLETED=1,REFUNDED=1)}
            clear_snapshot(db,order,policy,{oid:{'clearance':'NO_HOLD_OR_OPEN_CASE','order':review_fingerprint(order)}},aware(order.updated_at)+timedelta(days=2))
            journal=export_receipts(db)
            self.assertNotIn(BODY['phone'],json.dumps(journal));self.assertNotIn(BODY['detail'],json.dumps(journal))
            db.delete(db.get(OrderPrivacyReceipt,order.id));db.flush();db.add(OrderDeliverySnapshot(**values));db.flush()
            restore_receipts(db,journal);restore_receipts(db,journal)
            self.assertIsNone(db.get(OrderDeliverySnapshot,order.id));db.commit()
        self.assertEqual(self.call('POST',body=self.body).status_code,200)
