import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy.orm import Session
import test_miniprogram_orders as orders
from app.mall.parcel_tracking import TrackingService, tracking_service
from app.mall.order_lifecycle_service import execute_order_shipping
from app.models import Order, OrderItem, PointsAccount, InventoryBalance


class VirtualProvider:
    def __init__(self):
        self.calls = []
        self.fail = False

    def query(self, carrier, number, *, timeout):
        self.calls.append((carrier, number, timeout))
        if self.fail: raise TimeoutError('private-provider-body-must-not-leak')
        return dict(status='IN_TRANSIT', source='虚拟物流，仅测试', events=[
            dict(time='2026-11-06T09:00:00+08:00', description=number+' 已揽收'),
            dict(time='2026-11-06T10:00:00+08:00', description=number+' 到达测试转运中心')])


class ParcelTrackingTests(unittest.TestCase):
    def setUp(self):
        self.case = orders.MemberOrderTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.f = self.case.f
        self.provider = VirtualProvider()
        self.now = 0
        self.service = TrackingService(self.provider, clock=lambda:self.now)
        self.case.client.app.dependency_overrides[tracking_service] = lambda:self.service
        self.oid = self.case.place()['order_public_id']
        self.f.fulfill(self.oid)
        with Session(self.f.engine) as db:
            item = db.query(OrderItem.id).join(Order).filter(Order.order_public_id==self.oid).scalar()
        for number in ['VIRTUAL-A', 'VIRTUAL-B']:
            execute_order_shipping(self.f.engine, actor_admin_id=self.f.operator.id,
                order_public_id=self.oid, shipping_carrier='虚拟快递', tracking_number=number,
                quantities={item:1})
        self.parcels = self.case.call('GET','/'+self.oid).json()['parcels']

    def query(self, index=0, owner=0, oid=None, parcel=None):
        return self.case.call('GET','/'+(oid or self.oid)+'/parcels/'+str(parcel or self.parcels[index]['parcel_id'])+'/tracking',owner=owner)

    def balances(self):
        with Session(self.f.engine) as db:
            return ([tuple(getattr(x,c.name) for c in PointsAccount.__table__.columns) for x in db.query(PointsAccount)],
                    [tuple(getattr(x,c.name) for c in InventoryBalance.__table__.columns) for x in db.query(InventoryBalance)],
                    [(x.order_public_id,x.status) for x in db.query(Order)])

    def test_two_parcels_return_distinct_tracks_and_no_business_writes(self):
        before=self.balances()
        a,b=self.query().json(),self.query(1).json()
        self.assertNotEqual(a['parcel_id'],b['parcel_id'])
        self.assertIn('VIRTUAL-A',a['events'][0]['description'])
        self.assertIn('VIRTUAL-B',b['events'][0]['description'])
        self.assertEqual(a['events'][0]['time'],'2026-11-06T10:00:00+08:00')
        self.assertEqual(self.query().json()['availability'],'CACHED')
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(before,self.balances())

    def test_anonymous_other_member_and_wrong_order_denied_even_when_cached(self):
        self.query()
        self.assertEqual(self.query(owner=None).status_code,401)
        self.assertEqual(self.query(owner=1).status_code,404)
        self.assertEqual(self.query(parcel=2147483647).status_code,404)
        self.case.body['request_id']='b'*32
        other=self.case.place()['order_public_id']
        self.assertEqual(self.query(oid=other).status_code,404)
        self.assertEqual(len(self.provider.calls),1)

    def test_disabled_member_rejected_after_cache_warmup(self):
        from app.models import Member
        self.query()
        with Session(self.f.engine) as db:
            db.get(Member,self.f.member.id).is_active=False;db.commit()
        self.assertEqual(self.query().status_code,401)
        self.assertEqual(len(self.provider.calls),1)

    def test_provider_failure_cooldown_stale_expiry_and_recovery(self):
        initial=self.query().json();self.provider.fail=True;self.now=301
        result=self.query().json()
        self.assertEqual(result['availability'],'STALE')
        self.assertEqual(result['events'],initial['events'])
        self.assertNotIn('private-provider',str(result))
        self.query();self.assertEqual(len(self.provider.calls),2)
        self.now=3601;self.assertEqual(self.query().json()['availability'],'UNAVAILABLE')
        self.provider.fail=False;self.now=3632
        self.assertEqual(self.query().json()['availability'],'LIVE')

    def test_failure_and_pending_lookup_do_not_block_checkout_or_hold_connection(self):
        started,release=Event(),Event()
        def blocked(*args,**kwargs):
            self.assertEqual(self.f.engine.pool.checkedout(),0)
            started.set();release.wait(10)
            raise TimeoutError('must-not-escape')
        with patch.object(self.provider,'query',blocked),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.query)
            try:
                self.assertTrue(started.wait(3))
                self.case.body['request_id']='c'*32
                placed=self.case.place()
                self.assertEqual(placed['status'],'CREATED')
                self.assertEqual(self.case.call('POST','/'+placed['order_public_id']+'/cancel').status_code,200)
            finally:release.set()
            self.assertEqual(pending.result().json()['availability'],'UNAVAILABLE')
        self.case.body['request_id']='d'*32
        self.assertEqual(self.case.place()['status'],'CREATED')

    def test_unconfigured_provider_never_invents_tracks(self):
        self.service=TrackingService()
        result=self.query().json()
        self.assertEqual(result['availability'],'NOT_CONFIGURED')
        self.assertEqual(result['events'],[])

    def test_invalid_provider_data_is_sanitized_and_cache_is_bounded(self):
        with patch.object(self.provider,'query',return_value={'status':'UNKNOWN','secret':'never-return'}):
            self.assertEqual(self.query().json()['availability'],'UNAVAILABLE')
        service=TrackingService(self.provider,max_entries=2)
        for i in range(4):service.query((i,),'虚拟',str(i))
        self.assertEqual(len(service.entries),2)
        self.assertEqual(list(service.entries),[(2,),(3,)])

    def test_empty_trajectory_is_success_not_transport_failure(self):
        with patch.object(self.provider,'query',return_value=dict(status='PENDING',source='虚拟物流',events=[])):
            value=self.query().json()
            self.assertEqual((value['availability'],value['status'],value['events']),('LIVE','PENDING',[]))

    def test_duplicate_lookup_and_global_inflight_limit_fail_fast(self):
        start,release=Event(),Event()
        def blocked(*args,**kwargs):start.set();release.wait(5);raise TimeoutError()
        with patch.object(self.provider,'query',blocked),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.service.query,('one',),'虚拟','A')
            try:
                self.assertTrue(start.wait(2))
                self.assertEqual(self.service.query(('one',),'虚拟','A')['availability'],'UNAVAILABLE')
                self.assertTrue(self.service.slots.acquire(blocking=False))
                try:self.assertEqual(self.service.query(('other',),'虚拟','B')['availability'],'UNAVAILABLE')
                finally:self.service.slots.release()
            finally:release.set()
            pending.result()
