"""Synthetic pricing configuration, real form requests, and additive migration."""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
from unittest.mock import patch
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from app import models as m
from app.mall import client_organizations as orgs, client_pricing as pricing
from app.mall.public_catalog_service import list_public_products
from app.schema_readiness import assert_database_schema_ready
from app.time_utils import utc8_now
import test_client_organizations as organization_tests
from test_candidate_upgrade import fingerprint


def seed_catalog(db):
    category=m.ProductCategory(name='虚拟日用',slug='virtual-daily')
    supplier=m.Supplier(supplier_public_id='SUP-VIRTUAL-PRICE',name='虚拟供货单位')
    db.add_all([category,supplier]);db.flush()
    product=m.Product(product_public_id='PRD-VIRTUAL-PRICE',name='虚拟保温杯',category_id=category.id,
        status='PUBLISHED',published_at=utc8_now())
    db.add(product);db.flush()
    sku=m.ProductSku(product_id=product.id,supplier_id=supplier.id,sku_code='VIRTUAL-PRICE-01',
        name='标准款',points_price=Decimal('100.00'),cost_price=Decimal('45.00'),is_active=True)
    db.add(sku);db.flush()
    return sku.id


class ClientPricingTests(unittest.TestCase):
    def setUp(self):
        organization_tests.ClientOrganizationsTests.setUp(self)
        with self.database.begin() as db:self.sku=seed_catalog(db)

    def prepare(self, org=None, percent='90', mode='custom', actor=None):
        with self.database.begin() as db:
            return pricing.prepare(db,actor or self.user_id,org or self.parent,mode,percent)['preview_token']

    def save(self, token, actor=None):
        with self.database.begin() as db:
            event,changed=pricing.save(db,actor or self.user_id,token)
            return event.revision,changed

    def configure(self, org=None, percent='90', mode='custom'):
        return self.save(self.prepare(org,percent,mode))

    def effective(self, org):
        with self.database() as db:
            rules={r.organization_id:r for r in db.query(m.ClientPriceRule).all()}
            return pricing.preview_effective(db.get(m.ClientOrganization,org),rules)

    def csrf(self):
        response=self.client.get('/mall-price-rules')
        self.assertEqual(response.status_code,200)
        return self.client.cookies.get('client_pricing_csrf')

    def test_parent_default_override_and_explicit_base_are_distinct(self):
        self.configure(percent='90')
        self.assertEqual(self.effective(self.hrb),(9000,self.parent))
        self.configure(self.gx,'110')
        self.assertEqual(self.effective(self.gx),(11000,self.gx))
        self.configure(self.hrb,'100')
        self.configure(percent='80')
        self.assertEqual(self.effective(self.hrb),(10000,self.hrb))
        self.assertEqual(self.effective(self.gx),(11000,self.gx))
        self.configure(self.hrb,mode='inherit')
        self.assertEqual(self.effective(self.hrb),(8000,self.parent))
        with self.database() as db:
            row=next(r for r in pricing.overview(db) if r['org'].id==self.hrb)
            self.assertTrue(row['inherited'])
            self.assertEqual(row['effective_rule'].revision,2)
        self.configure(mode='inherit')
        self.assertEqual(self.effective(self.hrb),(10000,None))
        self.assertEqual(self.effective(self.gx),(11000,self.gx))

    def test_preview_shows_affected_branches_and_never_saves_rule(self):
        self.configure(self.gx,'110')
        with self.database.begin() as db:
            result=pricing.prepare(db,self.user_id,self.parent,'custom','90')
            self.assertEqual(result['scope_count'],2)
            self.assertEqual(result['sample'][0]['after'],'90.00')
            self.assertEqual(result['sample'][0]['cost'],'45.00')
            other=next(r for r in result['impacts'] if r['code']=='VIRTUAL-GX')
            self.assertFalse(other['affected']);self.assertEqual(other['after'],'110.00%')
            self.assertIsNone(db.get(m.ClientPriceRule,self.parent))

    def test_save_is_immediate_with_append_only_history_and_idempotent_retry(self):
        started=utc8_now().replace(tzinfo=None)
        token=self.prepare();self.assertEqual(self.save(token),(1,True))
        self.assertEqual(self.save(token),(1,False))
        self.configure(percent='95')
        self.assertEqual(self.save(token),(1,False))
        with self.database() as db:
            rule=db.get(m.ClientPriceRule,self.parent)
            self.assertEqual((rule.ratio_bps,rule.revision),(9500,2))
            events=db.query(m.ClientPriceRuleEvent).order_by(m.ClientPriceRuleEvent.revision).all()
            self.assertEqual([(e.old_ratio_bps,e.ratio_bps) for e in events],[(None,9000),(9000,9500)])
            self.assertGreaterEqual(events[0].effective_at.replace(tzinfo=None),started)
            self.assertLessEqual(events[-1].effective_at.replace(tzinfo=None),utc8_now().replace(tzinfo=None))
            self.assertEqual(db.query(m.AdminActionLog).filter_by(action_type='client_price_rule_change').count(),2)

    def test_rule_change_after_preview_is_rejected(self):
        old=self.prepare();self.configure(percent='110')
        with self.assertRaisesRegex(ValueError,'已变化'):self.save(old)
        self.assertEqual(self.effective(self.parent)[0],11000)

    def test_parent_change_invalidates_child_preview(self):
        child=self.prepare(self.hrb,mode='inherit')
        self.configure(percent='80')
        with self.assertRaisesRegex(ValueError,'已变化'):self.save(child)

    def test_sku_changes_or_new_sku_invalidate_preview(self):
        token=self.prepare()
        with self.database.begin() as db:db.get(m.ProductSku,self.sku).points_price=Decimal('101.00')
        with self.assertRaisesRegex(ValueError,'已变化'):self.save(token)
        token=self.prepare()
        with self.database.begin() as db:
            first=db.get(m.ProductSku,self.sku)
            db.add(m.ProductSku(product_id=first.product_id,supplier_id=first.supplier_id,sku_code='VIRTUAL-02',
                name='加大款',points_price=200,cost_price=90))
        with self.assertRaisesRegex(ValueError,'已变化'):self.save(token)

    def test_expired_preview_and_foreign_actor_cannot_save(self):
        token=self.prepare()
        with self.database.begin() as db:
            db.query(m.ClientPricePreview).update({'expires_at':utc8_now()-timedelta(seconds=1)})
        with self.assertRaisesRegex(ValueError,'10分钟'):self.save(token)
        with self.database.begin() as db:
            other=m.User(username='virtual-other-operator',password_hash='synthetic-only',role='admin',admin_level='operator',is_active=True)
            db.add(other);db.flush();other_id=other.id
        with self.assertRaisesRegex(ValueError,'预览无效'):self.save(self.prepare(),actor=other_id)

    def test_disabled_parent_or_revoked_admin_refuses_save(self):
        token=self.prepare(self.hrb)
        with self.database.begin() as db:orgs.toggle(db,self.user_id,self.parent,1,False)
        with self.assertRaisesRegex(ValueError,'停用'):self.save(token)
        with self.database.begin() as db:
            orgs.toggle(db,self.user_id,self.parent,2,True)
            db.get(m.User,self.user_id).admin_level='primary_reviewer'
        with self.assertRaises(PermissionError):self.save(token)

    def test_decimal_rounding_range_and_invalid_inputs(self):
        self.assertEqual(pricing.price_for_ratio('1.01',5000),Decimal('.51'))
        self.assertEqual(pricing.parse_ratio('custom','90.25'),9025)
        for value in ['0','0.99','300.01','-1','nan','inf','1e2','100.001','90%','９０']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):pricing.parse_ratio('custom',value)
        for base,ratio in [('.01',100),('9999999999999999.99',30000)]:
            with self.assertRaises(ValueError):pricing.price_for_ratio(base,ratio)

    def test_all_skus_validated_even_inactive_or_outside_first_twenty(self):
        with self.database.begin() as db:
            first=db.get(m.ProductSku,self.sku)
            for n in range(22):
                db.add(m.ProductSku(product_id=first.product_id,supplier_id=first.supplier_id,
                    sku_code=f'VIRTUAL-BULK-{n}',name=f'虚拟规格{n}',points_price=100 if n<21 else Decimal('.01'),cost_price=0,is_active=False))
        with self.assertRaisesRegex(ValueError,'为零'):self.prepare(percent='1')
        with self.database() as db:self.assertEqual(db.query(m.ClientPricePreview).count(),0)

    def test_failed_save_rolls_back_rule_event_audit_and_consumption(self):
        token=self.prepare()
        with patch.object(orgs,'audit',side_effect=RuntimeError('synthetic audit failure')):
            with self.assertRaises(RuntimeError):self.save(token)
        with self.database() as db:
            self.assertEqual(db.query(m.ClientPriceRule).count(),0)
            self.assertEqual(db.query(m.ClientPriceRuleEvent).count(),0)
            self.assertIsNone(db.query(m.ClientPricePreview).one().applied_revision)
        self.assertEqual(self.save(token),(1,True))

    def test_member_catalog_cost_and_points_are_unchanged_in_phase_two(self):
        with self.database() as db:
            before=list_public_products(db)
            sku=db.get(m.ProductSku,self.sku);prices=(sku.points_price,sku.cost_price)
        self.configure(percent='90')
        with self.database() as db:
            self.assertEqual(list_public_products(db),before)
            sku=db.get(m.ProductSku,self.sku)
            self.assertEqual((sku.points_price,sku.cost_price),prices)
            for model in (m.PointsAccount,m.PointsGrant,m.Order):self.assertEqual(db.query(model).count(),0)

    def test_real_form_preview_confirm_and_private_history(self):
        token=self.csrf()
        response=self.client.post('/mall-price-rules/preview',data=dict(csrf_token=token,
            organization_id=self.parent,mode='custom',percent='90'))
        self.assertEqual(response.status_code,200,response.text[-500:])
        self.assertEqual(response.headers['cache-control'],'no-store, private')
        self.assertIn('90.00 积分',response.text.replace('</strong>',''))
        preview=re.search(r'name="preview_token" value="([\w-]+)"',response.text).group(1)
        payload=dict(csrf_token=token,preview_token=preview,confirmed='true')
        response=self.client.post('/mall-price-rules/save',data=payload)
        self.assertEqual(response.status_code,303)
        page=self.client.get(response.headers['location'])
        self.assertIn('立即生效',page.text);self.assertIn('第1版',page.text)
        self.assertIn('继承第1版',page.text)
        self.assertIn('虚拟保险总公司',page.text)
        self.assertEqual(self.effective(self.hrb)[0],9000)

    def test_csrf_confirmation_and_role_boundaries(self):
        token=self.csrf()
        self.assertEqual(self.client.post('/mall-price-rules/preview',data=dict(organization_id=self.parent,mode='custom',percent='90')).status_code,403)
        self.assertEqual(self.client.post('/mall-price-rules/save',data=dict(csrf_token=token,preview_token=self.prepare())).status_code,400)
        with self.database.begin() as db:db.get(m.User,self.user_id).admin_level='primary_reviewer'
        self.assertEqual(self.client.get('/mall-price-rules').status_code,403)
        with self.database.begin() as db:db.get(m.User,self.user_id).role='partner'
        self.assertEqual(self.client.get('/mall-price-rules').status_code,403)
        self.assertEqual(self.client.post('/mall-price-rules/save',data=dict(csrf_token=token,confirmed='true')).status_code,403)


class ClientPricingMigrationTests(unittest.TestCase):
    def test_additive_upgrade_empty_roundtrip_and_nonempty_guard(self):
        with TemporaryDirectory() as folder:
            url='sqlite:///'+(Path(folder)/'virtual.db').as_posix()
            config=Config('alembic.ini');config.attributes['database_url']=url
            engine=create_engine(url)
            try:
                command.upgrade(config,'0023_client_organizations')
                factory=sessionmaker(bind=engine)
                with factory.begin() as db:
                    actor=m.User(username='virtual-pricing-migration',password_hash='synthetic-only',role='admin',admin_level='operator')
                    db.add(actor);db.flush();actor_id=actor.id
                    org_id=orgs.create(db,actor.id,code='VIRTUAL-PG',name='虚拟迁移公司',region='测试地').id
                old_tables=[n for n in inspect(engine).get_table_names() if n!='alembic_version']
                before=fingerprint(engine,old_tables)
                command.upgrade(config,'head');command.check(config)
                self.assertEqual(fingerprint(engine,old_tables),before)
                command.downgrade(config,'0023_client_organizations');command.upgrade(config,'head')
                with factory.begin() as db:token=pricing.prepare(db,actor_id,org_id,'custom','90')['preview_token']
                with factory.begin() as db:pricing.save(db,actor_id,token)
                all_tables=[n for n in inspect(engine).get_table_names() if n!='alembic_version']
                persisted=fingerprint(engine,all_tables)
                with self.assertRaisesRegex(RuntimeError,'拒绝删除留痕'):command.downgrade(config,'0023_client_organizations')
                self.assertEqual(fingerprint(engine,all_tables),persisted)
                assert_database_schema_ready(engine)
            finally:engine.dispose()


if __name__=='__main__':unittest.main()
