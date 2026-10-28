import os
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
