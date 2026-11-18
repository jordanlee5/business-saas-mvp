"""Synthetic organizations only: funds and transaction history cannot cross scopes."""
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from sqlalchemy.orm import Session
from app import models as m
from app.mall import institution_scope as scope
from app.mall import client_pricing as pricing
from app.mall.order_service import OrderLineRequest
import test_order_service as fixtures


class InstitutionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OrderServiceTests('runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
        self.db=self.f.db
        self.a=self.f.organization
        self.b=self.org('VIRTUAL-B')
        self.db.commit()

    def org(self, code, parent=None):
        row=m.ClientOrganization(code=code,name=code,region='虚拟地区',parent_id=parent,
            is_active=True,revision=1,updated_by_id=self.f.operator.id,created_at=fixtures.NOW,updated_at=fixtures.NOW)
        self.db.add(row);self.db.flush();return row

    def ratio(self, org, value):
        token=pricing.prepare(self.db,self.f.operator.id,org.id,'inherit' if value is None else 'custom',value or '')['preview_token']
        self.db.flush()
        pricing.save(self.db,self.f.operator.id,token);self.db.commit()

    def switch_later(self, organization):
        """Construct a separate synthetic source batch before testing isolation."""
        from tools.virtual_member_access import virtual_batch_scope
        batch=m.UploadBatch(user_id=self.f.partner.id,filename='virtual-other.xlsx',redemption_mode='MALL_REDEMPTION',acceptance_status='已承接')
        self.db.add(batch);self.db.flush();virtual_batch_scope(self.db,batch,now=fixtures.NOW,organization=organization)
        self.db.get(m.BusinessRecord,self.f.later_grant.business_record_id).batch_id=batch.id
        # Fixture assembly only; application has no reassignment endpoint.
        row=self.db.get(m.InstitutionGrant,self.f.later_grant.id)
        row.organization_id=organization.id;row.organization_code=organization.code;row.organization_name=organization.name
        self.db.commit()

    def test_hq_inherit_branch_override_and_explicit_base(self):
        parent=self.org('VIRTUAL-HQ');self.a.parent_id=parent.id;self.db.commit()
        self.ratio(parent,'90');s=scope.resolve(self.db,self.f.member.id,now=fixtures.NOW)
        self.assertEqual(scope.unit_price('40',s),Decimal('36.00'))
        self.ratio(self.a,'110');s=scope.resolve(self.db,self.f.member.id,now=fixtures.NOW)
        self.assertEqual(scope.unit_price('40',s),Decimal('44.00'))
        self.ratio(self.a,'100');self.assertEqual(scope.resolve(self.db,self.f.member.id,now=fixtures.NOW).ratio_bps,10000)
        self.ratio(self.a,None);self.assertEqual(scope.resolve(self.db,self.f.member.id,now=fixtures.NOW).ratio_bps,9000)

    def test_member_does_not_spend_other_customer_or_sibling_points(self):
        for sibling in (False,True):
            with self.subTest(sibling=sibling):
                if sibling:
                    parent=self.org('VIRTUAL-PARENT');self.a.parent_id=parent.id;self.b.parent_id=parent.id;self.db.commit()
                self.switch_later(self.b)
                # Global checksum is 170, current organization owns only 70.
                with self.assertRaises(ValueError):self.f.place(lines=(OrderLineRequest(self.f.sku_one.id,2),))
                self.db.expire_all();self.assertEqual(self.db.query(m.Order).count(),0)
                groups=scope.partition_balances(self.db,self.f.member.id,now=fixtures.NOW)
                self.assertEqual(sorted(x['available_points'] for x in groups),[Decimal('70'),Decimal('100')])

    def test_order_snapshots_adjusted_unit_price_cost_unchanged(self):
        self.ratio(self.a,'90');result=self.f.place();self.db.expire_all()
        order=self.db.get(m.Order,result.order_id);item=self.db.query(m.OrderItem).filter_by(order_id=order.id,sku_id=self.f.sku_one.id).one()
        self.assertEqual(order.total_points,Decimal('94.50'));self.assertEqual(item.unit_points_price,Decimal('36'))
        self.assertEqual(item.unit_cost_price,Decimal('12'));self.assertEqual(self.db.get(m.ProductSku,self.f.sku_one.id).points_price,Decimal('40'))
        self.assertEqual(self.db.get(m.InstitutionOrder,order.id).organization_id,self.a.id)
        self.ratio(self.a,'110');replay=self.f.place();self.assertTrue(replay.replayed);self.assertEqual(replay.total_points,Decimal('94.50'))

    def test_cancel_after_institution_switch_returns_only_original_grant(self):
        self.switch_later(self.b);self.ratio(self.a,'90')
        result=self.f.place(lines=(OrderLineRequest(self.f.sku_one.id,1),))
        self.db.expire_all();q=self.db.query(m.MemberQualification).one();q.business_record_id=self.f.later_grant.business_record_id;self.db.commit()
        self.ratio(self.b,'150');self.f.cancel(result.order_public_id);self.db.expire_all()
        self.assertEqual(self.db.get(m.PointsGrant,self.f.early_grant.id).available_points,Decimal('70'))
        self.assertEqual(self.db.get(m.PointsGrant,self.f.later_grant.id).available_points,Decimal('100'))
        self.assertEqual(scope.resolve(self.db,self.f.member.id,now=fixtures.NOW).organization_id,self.b.id)

    def test_refund_after_switch_preserves_paid_amount_and_original_institution(self):
        self.switch_later(self.b);self.ratio(self.a,'90')
        result=self.f.place(lines=(OrderLineRequest(self.f.sku_one.id,1),))
        self.f.fulfill(result.order_public_id);self.f.ship(result.order_public_id);self.f.complete(result.order_public_id)
        self.db.expire_all();q=self.db.query(m.MemberQualification).one();q.business_record_id=self.f.later_grant.business_record_id;self.db.commit()
        self.ratio(self.b,'200');refund=self.f.refund(result.order_public_id);self.assertEqual(refund.refunded_points,Decimal('36'))
        self.db.expire_all();self.assertEqual(self.db.get(m.PointsGrant,self.f.early_grant.id).available_points,Decimal('70'))
        self.assertEqual(self.db.get(m.PointsGrant,self.f.later_grant.id).available_points,Decimal('100'))

    def test_unknown_disabled_conflicting_or_expired_scope_blocks_new_orders(self):
        self.switch_later(self.b)
        q=self.db.query(m.MemberQualification).one()
        second=m.MemberQualification(business_record_id=self.f.later_grant.business_record_id,member_id=self.f.member.id,
            enabled=True,valid_from=q.valid_from,valid_until=q.valid_until,version=1,confirmed_by=self.f.operator.id,
            confirmed_at=fixtures.NOW,bound_at=fixtures.NOW,updated_at=fixtures.NOW)
        self.db.add(second);self.db.commit()
        with self.assertRaises(scope.ScopeError):self.f.place()
        second.enabled=False;self.db.commit();self.a.is_active=False;self.db.commit()
        with self.assertRaises(scope.ScopeError):self.f.place()
        self.a.is_active=True;q.valid_until=fixtures.NOW;self.db.commit()
        with self.assertRaises(scope.ScopeError):self.f.place()
        q.valid_until=fixtures.NOW+timedelta(days=1);self.db.delete(self.db.get(m.InstitutionGrant,self.f.early_grant.id));self.db.commit()
        with self.assertRaises(scope.ScopeError):self.f.place()

    def test_cross_institution_allocation_tamper_blocks_cancellation(self):
        result=self.f.place();self.switch_later(self.b)
        with self.assertRaises(RuntimeError):self.f.cancel(result.order_public_id)
        self.db.expire_all();self.assertEqual(self.db.get(m.Order,result.order_id).status,'CREATED')

    def test_sku_changes_cannot_create_zero_or_overflow_for_existing_rules(self):
        from app.mall.catalog_service import create_product_sku, update_product_sku
        self.ratio(self.a,'1')
        with self.assertRaises(ValueError):
            update_product_sku(self.db,actor_admin_id=self.f.operator.id,sku_id=self.f.sku_one.id,points_price='0.01')
        self.db.rollback()
        with self.assertRaises(ValueError):
            create_product_sku(self.db,actor_admin_id=self.f.operator.id,product_id=self.f.product.id,
                supplier_id=self.f.supplier.id,sku_code='VIRTUAL-ZERO',name='虚拟极小售价',points_price='0.01',cost_price='0')
        self.db.rollback()
        with self.assertRaises(ValueError):
            update_product_sku(self.db,actor_admin_id=self.f.operator.id,sku_id=self.f.sku_one.id,points_price='99999999999.99')
        self.db.rollback()
        self.assertEqual(self.db.get(m.ProductSku,self.f.sku_one.id).points_price,Decimal('40'))

    def test_price_change_during_checkout_rolls_back(self):
        real=scope.resolve;calls=[]
        def changing(*args,**kwargs):
            value=real(*args,**kwargs);calls.append(1)
            if len(calls)>1:
                from dataclasses import replace
                return replace(value,context='f'*64)
            return value
        with patch.object(scope,'resolve',side_effect=changing):
            with self.assertRaises(ValueError):self.f.place()
        self.assertEqual(self.db.query(m.Order).count(),0)


class InstitutionApiTests(unittest.TestCase):
    def setUp(self):
        from test_miniprogram_orders import MemberOrderTests
        self.f=MemberOrderTests('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.enterContext(patch('app.api.miniprogram_v1.SessionLocal', self.f.f.Session))
        self.enterContext(patch('app.api.miniprogram_v1.utc8_now',lambda:fixtures.NOW))
        self.enterContext(patch('app.mall.institution_scope.utc8_now',lambda:fixtures.NOW))

    def test_preupgrade_client_can_replay_original_order_without_new_context(self):
        import hashlib,json
        result=self.f.place()
        legacy=dict(self.f.body);legacy.pop('pricing_context')
        with Session(self.f.f.engine) as db:
            order=db.query(m.Order).filter_by(order_public_id=result['order_public_id']).one()
            snapshot=db.get(m.OrderDeliverySnapshot,order.id)
            snapshot.request_digest=hashlib.sha256(json.dumps(legacy,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            db.query(m.MemberQualification).update({'enabled':False});db.commit()
        replay=self.f.call('POST',body=legacy)
        self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.json()['order_public_id'],result['order_public_id'])
        self.assertEqual(replay.json()['total_points'],'80.00')

    def test_same_member_other_institution_grants_never_enter_points_api_or_checkout(self):
        f=self.f.f
        from tools.virtual_member_access import virtual_batch_scope
        with Session(f.engine) as db:
            batch=m.UploadBatch(user_id=f.partner.id,filename='virtual-second.xlsx',redemption_mode='MALL_REDEMPTION',acceptance_status='已承接')
            db.add(batch);db.flush();other=virtual_batch_scope(db,batch,now=fixtures.NOW)
            db.get(m.BusinessRecord,f.later_grant.business_record_id).batch_id=batch.id
            owner=db.get(m.InstitutionGrant,f.later_grant.id)
            owner.organization_id=other.id;owner.organization_code=other.code;owner.organization_name=other.name;db.commit()
        headers={'Authorization':'Bearer '+self.f.tokens[0]}
        summary=self.f.client.get('/api/miniprogram/v1/points/summary',headers=headers).json()
        self.assertEqual(summary['available_points'],'70.00')
        for endpoint in ('grants','ledger'):
            response=self.f.client.get('/api/miniprogram/v1/points/'+endpoint,headers=headers)
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['total'],1)
        self.assertEqual(self.f.call('POST',body=self.f.body).status_code,409)

    def test_http_catalog_exposes_only_customer_price_and_current_points(self):
        from tools.virtual_member_access import virtual_catalog_member
        from test_miniprogram_orders import APPID
        with Session(self.f.f.engine) as db:
            token=pricing.prepare(db,self.f.f.operator.id,self.f.f.organization.id,'custom','90')['preview_token']
            pricing.save(db,self.f.f.operator.id,token)
            other,other_token=virtual_catalog_member(db,app_id=APPID,now=fixtures.NOW);db.commit()
        def get(path, token):
            return self.f.client.get('/api/miniprogram/v1'+path,headers={'Authorization':'Bearer '+token})
        own=get('/products/PRD-ORDER-001',self.f.tokens[0]);other=get('/products/PRD-ORDER-001',other_token)
        self.assertEqual((own.status_code,other.status_code),(200,200))
        self.assertEqual(own.json()['skus'][0]['points_price'],'36.00')
        self.assertEqual(other.json()['skus'][0]['points_price'],'40.00')
        for key in ('base_price','cost_price','ratio_bps','organization_id'):
            self.assertNotIn(key,own.text)
        self.assertNotIn('40.00',own.text)
        listing=get('/products',self.f.tokens[0]).json()
        self.assertEqual(listing['items'][0]['min_points_price'],'22.50')
        self.assertEqual(listing['pricing_context'],own.json()['pricing_context'])
        self.assertEqual(get('/points/summary',self.f.tokens[0]).json()['available_points'],'170.00')
        self.assertEqual(get('/points/summary',other_token).json()['available_points'],'1.00')
        self.assertEqual(get('/points/grants',other_token).json()['total'],1)
        self.assertEqual(get('/points/ledger',other_token).json()['total'],1)

    def test_missing_or_old_context_cannot_create_order_but_history_replays(self):
        old=dict(self.f.body);old.pop('pricing_context')
        self.assertEqual(self.f.call('POST',body=old).status_code,409)
        stale=dict(self.f.body,pricing_context='0'*64)
        self.assertEqual(self.f.call('POST',body=stale).status_code,409)
        result=self.f.place()
        with Session(self.f.f.engine) as db:
            row=db.query(m.MemberQualification).one();row.enabled=False;db.commit()
        replay=self.f.call('POST',body=self.f.body);self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.json()['order_public_id'],result['order_public_id'])
        self.assertEqual(self.f.call('GET','/'+result['order_public_id']).status_code,200)

    def test_price_changed_context_rejected_even_if_rounded_amount_same(self):
        with Session(self.f.f.engine) as db:
            token=pricing.prepare(db,self.f.f.operator.id,self.f.f.organization.id,'custom','100.01')['preview_token']
            pricing.save(db,self.f.f.operator.id,token);db.commit()
        self.assertEqual(self.f.call('POST',body=self.f.body).status_code,409)
