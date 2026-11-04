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

    def test_beijing_month_boundary_september_then_october(self):
        self._check_adjacent_settlement_months([9, 10])

    def test_beijing_month_boundary_october_then_september(self):
        self._check_adjacent_settlement_months([10, 9])

    def test_monthly_batch_first_then_overlapping_supplement(self):
        self._check_overlapping_settlements(monthly_first=True)

    def test_overlapping_supplement_first_then_monthly_batch(self):
        self._check_overlapping_settlements(monthly_first=False)

    def _submit_overlap_pair(self, first_action, second_action, second_actor):
        first_action()
        second_action()

    def _check_overlapping_settlements(self, *, monthly_first):
        from datetime import datetime, timedelta
        from io import BytesIO
        from openpyxl import load_workbook
        from app.admin_permissions import OPERATOR, SUPER_ADMIN
        from app.main import mall_settlement_detail_page
        from app.mall import (OrderLineRequest, execute_supplier_settlement_confirmation,
                              execute_supplier_settlement_export, execute_supplier_settlement_generation)
        from app.mall.supplier_settlement_reporting_service import get_supplier_settlement_detail
        from app.models import AdminActionLog, SupplierSettlementBatch, SupplierSettlementItem, User
        from test_supplier_settlement_routes import request

        f = self.f
        a, b = f.sku_one, f.sku_two
        monthly = (datetime(2026, 9, 1), datetime(2026, 10, 1))
        supplement = (datetime(2026, 9, 22), datetime(2026, 10, 8))
        now = datetime(2026, 10, 9)
        orders = []
        for i, (completed, lines) in enumerate([
            (datetime(2026, 9, 20), [(a.id, 1)]),
            (datetime(2026, 9, 27), [(a.id, 1), (b.id, 1)]),
            (datetime(2026, 10, 4), [(b.id, 2)]),
        ]):
            placed = f.place(key=f"overlap-{i}", lines=[OrderLineRequest(sku, qty) for sku, qty in lines])
            oid = placed.order_public_id
            f.fulfill(oid)
            f.ship(oid, tracking_number=f"OVERLAP-{i}")
            f.complete(oid, now=completed)
            orders.append(oid)

        with f.Session.begin() as db:
            other = User(username="supplement-operator", role="admin", admin_level=OPERATOR, is_active=True)
            reviewer = User(username="supplement-reviewer", role="admin", admin_level=SUPER_ADMIN, is_active=True)
            db.add_all([other, reviewer])
            db.flush()
            other_id, reviewer_id = other.id, reviewer.id
        supplier_id, operator_id = f.supplier.id, f.operator.id

        def generate(period, actor):
            return execute_supplier_settlement_generation(
                f.engine, actor_admin_id=actor, supplier_id=supplier_id,
                period_start=period[0], period_end=period[1], now=now)

        first, second = (monthly, supplement) if monthly_first else (supplement, monthly)
        self._submit_overlap_pair(lambda: generate(first, operator_id), lambda: generate(second, other_id), other_id)
        left, shared, right = orders
        shared_lines = [(shared, a.sku_code, 1, 12), (shared, b.sku_code, 1, 5)]
        expected = {monthly: [(left, a.sku_code, 1, 12)], supplement: [(right, b.sku_code, 2, 10)]}
        expected[first].extend(shared_lines)

        for period, actor in ((first, operator_id), (second, other_id)):
            with self.assertRaisesRegex(ValueError, "没有未结算"):
                generate(period, actor)
            with f.Session() as db:
                batch = db.query(SupplierSettlementBatch).filter_by(generated_by_admin_id=actor).one()
                public_id = batch.settlement_public_id
                detail = get_supplier_settlement_detail(db, settlement_public_id=public_id)
                lines = expected[period]
                self.assertCountEqual([(i.order_public_id_snapshot, i.sku_code_snapshot, i.quantity, i.line_cost_amount)
                                       for i in detail.items], lines)
                self.assertEqual((detail.period_start, detail.period_end), period)
                self.assertEqual((detail.order_count, detail.item_count, detail.total_quantity, detail.total_cost_amount),
                                 (len({line[0] for line in lines}), len(lines),
                                  sum(line[2] for line in lines), sum(line[3] for line in lines)))
            with patch("app.main.SessionLocal", f.Session), patch("app.main.get_current_user", return_value=f.operator):
                page = mall_settlement_detail_page(request(f"/mall-settlements/{public_id}"), public_id)
            self.assertEqual(page.status_code, 200)
            for oid in orders:
                self.assertEqual(oid in page.body.decode(), oid in {line[0] for line in lines})
            execute_supplier_settlement_confirmation(f.engine, actor_admin_id=reviewer_id,
                settlement_public_id=public_id, now=now + timedelta(minutes=1))
            exported = execute_supplier_settlement_export(f.engine, actor_admin_id=actor,
                settlement_public_id=public_id, now=now + timedelta(minutes=2))
            book = load_workbook(BytesIO(exported.content), read_only=True)
            try:
                summary = dict(book["结算汇总"].values)
                self.assertEqual((summary["结算编号"], summary["订单数"], summary["人民币成本总额"]),
                                 (public_id, detail.order_count, detail.total_cost_amount))
                rows = book["结算明细"].values
                headers = next(rows)
                indices = [headers.index(key) for key in ("订单公开编号", "SKU编码", "数量", "行成本")]
                self.assertCountEqual([tuple(row[i] for i in indices) for row in rows], lines)
            finally:
                book.close()

        with self.assertRaisesRegex(ValueError, "没有未结算"):
            generate((monthly[0], supplement[1]), operator_id)
        with f.Session() as db:
            batches = db.query(SupplierSettlementBatch).all()
            self.assertEqual(len(batches), 2)
            self.assertEqual(sum(batch.total_cost_amount for batch in batches), 39)
            items = db.query(SupplierSettlementItem).all()
            self.assertEqual(len(items), 4)
            self.assertEqual(len({item.order_item_id for item in items}), 4)
            self.assertEqual(db.query(AdminActionLog).filter_by(action_type="mall_supplier_settlement_generate").count(), 2)

    def _check_adjacent_settlement_months(self, months):
        from datetime import datetime, timedelta, timezone
        from io import BytesIO
        from secrets import token_urlsafe
        from urllib.parse import parse_qs, urlsplit
        from openpyxl import load_workbook
        from app.admin_permissions import SUPER_ADMIN
        from app.auth import get_password_hash
        from app.main import app
        from app.mall import OrderLineRequest
        from app.models import SupplierSettlementItem, User
        from app.time_utils import UTC8_TIMEZONE
        from tools.rehearse_member_fulfillment import FormFields

        f = self.f
        midnight = datetime(2026, 10, 1, tzinfo=UTC8_TIMEZONE)
        expected = {9: {}, 10: {}}
        for offset, sku, qty in [(-1, f.sku_one, 1), (0, f.sku_two, 1), (1, f.sku_one, 2)]:
            placed = f.place(key=f"boundary-{offset}", lines=[OrderLineRequest(sku.id, qty)])
            oid = placed.order_public_id
            f.fulfill(oid)
            f.ship(oid, tracking_number=f"TEST-{placed.order_id}")
            completed = midnight + timedelta(microseconds=offset)
            f.complete(oid, now=completed.astimezone(timezone.utc))
            expected[completed.month][oid] = (completed.replace(tzinfo=None), qty, qty * sku.cost_price)

        periods = {9: ("2026-09-01T00:00", "2026-10-01T00:00"),
                   10: ("2026-10-01T00:00", "2026-11-01T00:00")}
        password = token_urlsafe(24)
        with f.Session.begin() as db:
            db.add(User(username="month-reviewer", password_hash=get_password_hash(password),
                        role="admin", admin_level=SUPER_ADMIN, is_active=True))
        clock = datetime(2026, 11, 2, 12)
        client = TestClient(app, follow_redirects=False)
        self.addCleanup(client.close)
        with (patch("app.main.SessionLocal", f.Session), patch("app.main.engine", f.engine),
              patch("app.mall.supplier_settlement_service.utc8_now", return_value=clock),
              patch("app.mall.supplier_settlement_reporting_service.utc8_now", return_value=clock)):
            login = client.post("/login", data={"username": "month-reviewer", "password": password})
            self.assertEqual((login.status_code, login.headers["location"]), (302, "/dashboard"))
            for month in months:
                start, end = periods[month]
                action = "/mall-settlements/generate"
                form = FormFields(client.get("/mall-settlements").text, action).values
                form.update(supplier_id=f.supplier.id, period_start=start, period_end=end)
                result = client.post(action, data=form)
                self.assertEqual(result.status_code, 303)
                path = result.headers["location"]
                self.assertTrue(path.startswith("/mall-settlements/STL-"), path)
                page = client.get(path)
                self.assertEqual(page.status_code, 200)
                for oid, (when, _, _) in expected[month].items():
                    self.assertIn(oid, page.text)
                    self.assertIn(when.strftime("%Y-%m-%d %H:%M:%S"), page.text)
                other_month = 10 if month == 9 else 9
                for oid in expected[other_month]:
                    self.assertNotIn(oid, page.text)
                confirm = path + "/confirm"
                self.assertEqual(client.post(confirm, data=FormFields(page.text, confirm).values).headers["location"], path)
                repeat = client.post(action, data=form)
                self.assertEqual(parse_qs(urlsplit(repeat.headers["location"]).query).get("error"),
                                 ["结算区间内没有未结算的已完成订单项"])
                data = client.get(path + "/export")
                self.assertEqual(data.status_code, 200)
                book = load_workbook(BytesIO(data.content), read_only=True)
                try:
                    summary = dict(book["结算汇总"].values)
                    self.assertEqual((summary["结算开始时间"], summary["结算结束时间"]),
                                     (datetime.fromisoformat(start), datetime.fromisoformat(end)))
                    self.assertEqual((summary["订单数"], summary["订单项数"], summary["商品数量"], summary["人民币成本总额"]),
                                     (1, 1, 1, 12) if month == 9 else (2, 2, 3, 29))
                    rows = book["结算明细"].values
                    header = next(rows)
                    columns = [header.index(k) for k in ("订单公开编号", "订单完成时间", "数量", "行成本")]
                    self.assertCountEqual([tuple(row[i] for i in columns) for row in rows],
                                          [(oid, when.replace(microsecond=0), qty, cost)
                                           for oid, (when, qty, cost) in expected[month].items()])
                finally:
                    book.close()
            client.get("/logout")
        with f.Session() as db:
            lines = db.query(SupplierSettlementItem).all()
            self.assertEqual(len(lines), 3)
            for line in lines:
                when = line.order_completed_at
                if when.tzinfo:
                    when = when.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
                self.assertEqual(when, expected[when.month][line.order_public_id_snapshot][0])

    def test_refund_rejects_inconsistent_completed_parcel(self):
        from app.models import OrderParcelItem
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        self.f.ship(oid)
        self.f.complete(oid)
        with self.f.Session.begin() as db:
            db.query(OrderParcelItem).one().quantity -= 1
        with self.assertRaisesRegex(ValueError, '包裹'):
            self.f.refund(oid)
        self.assertEqual(self.call('GET', '/' + oid).json()['status'], 'COMPLETED')

    def test_refund_follows_settled_item_even_if_order_reference_disagrees(self):
        from datetime import timedelta
        from app.mall import execute_supplier_settlement_generation
        from app.models import SupplierSettlementItem
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        self.f.ship(oid)
        self.f.complete(oid)
        self.body['request_id'] = 'b' * 32
        other = self.place()['order_public_id']
        execute_supplier_settlement_generation(
            self.f.engine, actor_admin_id=self.f.operator.id, supplier_id=self.f.supplier.id,
            period_start=fixtures.NOW, period_end=fixtures.NOW + timedelta(days=1),
            now=fixtures.NOW + timedelta(days=2))
        with self.f.Session.begin() as db:
            db.query(SupplierSettlementItem).one().order_id = db.query(Order.id).filter_by(order_public_id=other).scalar()
        with self.assertRaisesRegex(ValueError, '已进入供应商结算'):
            self.f.refund(oid)
        self.assertEqual(self.call('GET', '/' + oid).json()['status'], 'COMPLETED')
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

    def test_members_retry_same_request_id_after_competing_for_stock(self):
        from datetime import timedelta
        from threading import Barrier

        f = self.f
        other = f.db.query(Member).filter_by(member_public_id='MEM-OTHER-ORDER').one()
        account = PointsAccount(member_id=other.id)
        f.db.add(account)
        f.db.flush()
        f.add_grant(public_no='BR-OTHER-STOCK', points='100.00', account_id=account.id,
                    expires_at=fixtures.NOW + timedelta(days=10))
        other_address = dict(BODY, detail='第二位会员的测试地址')
        address = create_member_address(f.db, other.id, other_address)
        member_ids = [f.member.id, other.id]
        sku_id = f.sku_two.id
        f.db.commit()

        lines = [dict(self.body['lines'][0], sku_code=f.sku_two.sku_code, quantity=4, expected_points_price='25.00')]
        requests = [dict(self.body, lines=lines),
                    dict(self.body, lines=lines, expected_address=other_address, address_public_id=address['address_public_id'])]
        for owner, body in enumerate(requests):
            preview = self.call('POST', '/fulfillment-preview', {'lines': lines}, owner=owner)
            self.assertEqual(preview.status_code, 200)
            body['fulfillment_digest'] = preview.json()['digest']

        def check_reservations(active_owner, order_count):
            with Session(f.engine) as db:
                balance = db.query(InventoryBalance).filter_by(sku_id=sku_id).one()
                self.assertEqual((balance.on_hand_quantity, balance.reserved_quantity),
                                 (4, 0 if active_owner is None else 4))
                for owner, starting in enumerate([170, 100]):
                    points = db.query(PointsAccount).filter_by(member_id=member_ids[owner]).one()
                    reserved = 100 if owner == active_owner else 0
                    self.assertEqual((points.available_points, points.reserved_points), (starting - reserved, reserved))
                self.assertEqual(db.query(Order).count(), order_count)
                self.assertEqual(db.query(OrderDeliverySnapshot).count(), order_count)

        start = Barrier(2)
        def checkout(owner):
            start.wait(timeout=10)
            return self.call('POST', body=requests[owner], owner=owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(checkout, range(2)))
        codes = [response.status_code for response in responses]
        self.assertCountEqual(codes, [201, 409])
        winner, waiting = codes.index(201), codes.index(409)
        first = responses[winner].json()['order_public_id']
        lookup = '/request/' + self.body['request_id']
        check_reservations(winner, 1)
        self.assertEqual(self.call('GET', lookup, owner=waiting).status_code, 404)
        self.assertEqual(self.call('POST', '/' + first + '/cancel', owner=waiting).status_code, 404)
        self.assertEqual(self.call('POST', '/' + first + '/cancel', owner=winner).json()['status'], 'CANCELLED')
        check_reservations(None, 1)

        retried = self.call('POST', body=requests[waiting], owner=waiting)
        self.assertEqual(retried.status_code, 201)
        second = retried.json()['order_public_id']
        self.assertNotEqual(first, second)
        for owner, oid, status in ((winner, first, 'CANCELLED'), (waiting, second, 'CREATED')):
            replay = self.call('POST', body=requests[owner], owner=owner)
            self.assertEqual(replay.status_code, 200)
            found = self.call('GET', lookup, owner=owner)
            self.assertEqual(found.status_code, 200)
            self.assertEqual(replay.json(), found.json())
            self.assertEqual((found.json()['order_public_id'], found.json()['status'], found.json()['address']),
                             (oid, status, requests[owner]['expected_address']))
            self.assertEqual(self.call('GET', '/' + oid, owner=1-owner).status_code, 404)
        check_reservations(waiting, 2)


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

    def test_preparation_and_manual_handoff_are_scoped_and_replay_safe(self):
        from app.mall.supplier_handoff import handle_packet, packet_history, CONFIRM
        from app.models import AdminActionLog
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        supplier = self.f.supplier.supplier_public_id
        args = (self.f.engine, self.f.operator.id, oid)
        receipt, packet = handle_packet(*args, supplier)
        self.assertEqual(packet['address'], BODY)
        with Session(self.f.engine) as db:
            row, = packet_history(db, oid)
            self.assertIsNone(row.confirmed_at)
            self.assertEqual(row.supplier, supplier)
        with self.assertRaises(ValueError):
            handle_packet(*args, 'OTHER', preparation_id=receipt)
        self.f.ship(oid)
        def confirm(_):
            return handle_packet(*args, supplier, preparation_id=receipt)
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(list(workers.map(confirm, range(2))), [(receipt, None)] * 2)
        with Session(self.f.engine) as db:
            row, = packet_history(db, oid)
            self.assertIsNotNone(row.confirmed_at)
            log = db.query(AdminActionLog).filter_by(action_type=CONFIRM.value).one()
            self.assertEqual((log.target_id, log.description, log.admin_id), (receipt, supplier, args[1]))

    def test_supplier_receipts_follow_confirmed_handoff_and_keep_first_registration(self):
        from datetime import datetime
        from app.mall.order_privacy import aware
        from app.time_utils import UTC8_TIMEZONE
        from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER
        from app.mall.supplier_handoff import handle_packet, packet_history, record_disposition, receipts_by_handoff
        from app.models import User

        f = self.f
        first = self.place()['order_public_id']
        self.body['request_id'] = 'b' * 32
        other = self.place()['order_public_id']
        f.fulfill(first)
        supplier = f.supplier.supplier_public_id
        actor = f.operator.id
        args = f.engine, actor, first, supplier
        with f.Session.begin() as db:
            peer = User(username='receipt-operator', role='admin', admin_level=OPERATOR, is_active=True)
            reviewer = User(username='receipt-reviewer', role='admin', admin_level=PRIMARY_REVIEWER, is_active=True)
            db.add_all([peer, reviewer])
            db.flush()
            peer_id, reviewer_id = peer.id, reviewer.id
        started = datetime.now(UTC8_TIMEZONE)
        prep, _ = handle_packet(*args)
        with self.assertRaises(ValueError):
            record_disposition(*args, prep, 'returned', 'RET-1')
        handle_packet(*args, preparation_id=prep)
        with f.Session() as db:
            row, = packet_history(db, first)
            handoff = row.handoff_id
        another_prep, _ = handle_packet(*args)
        handle_packet(*args, preparation_id=another_prep)
        with f.Session() as db:
            another_handoff, = [r.handoff_id for r in packet_history(db, first) if r.id == another_prep]
        f.ship(first)
        for target_order, target_supplier in [(other, supplier), (first, 'SUP-WRONG')]:
            with self.assertRaises(ValueError):
                record_disposition(f.engine, peer_id, target_order, target_supplier, handoff, 'returned', 'RET-1')
        with self.assertRaises(PermissionError):
            record_disposition(f.engine, reviewer_id, first, supplier, handoff, 'returned', 'RET-1')
        for outcome, ref in [('unknown', 'RET-1'), ('returned', ' '), ('returned', 'X' * 81)]:
            with self.assertRaises(ValueError):
                record_disposition(*args, handoff, outcome, ref)

        def register(operator):
            return record_disposition(f.engine, operator, first, supplier, handoff, 'returned', 'RET-1')
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(register, (peer_id, actor)))
        self.assertEqual(ids[0], ids[1])
        with self.assertRaisesRegex(ValueError, '其他回执编号'):
            record_disposition(*args, handoff, 'returned', 'RET-2')
        record_disposition(f.engine, peer_id, first, supplier, handoff, 'deleted', 'DEL-1')
        with f.Session() as db:
            by_handoff = receipts_by_handoff(db, packet_history(db, first))
            self.assertEqual(by_handoff[another_handoff], {})
            records = by_handoff[handoff]
            self.assertEqual(set(records), {'returned', 'deleted'})
            self.assertIn(records['returned'].admin_id, (peer_id, actor))
            self.assertEqual(records['returned'].description, 'RET-1')
            self.assertEqual((records['deleted'].username, records['deleted'].admin_id, records['deleted'].description),
                             ('receipt-operator', peer_id, 'DEL-1'))
            self.assertTrue(all(started <= aware(r.created_at) <= datetime.now(UTC8_TIMEZONE) for r in records.values()))
            self.assertFalse(receipts_by_handoff(db, packet_history(db, other)))

    def test_two_waybills_split_one_sku_and_retries_do_not_overship(self):
        from app.mall.order_lifecycle_service import execute_order_shipping, execute_order_completion
        from app.models import OrderItem
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        with Session(self.f.engine) as db:
            item_id = db.query(OrderItem.id).join(Order).filter(Order.order_public_id == oid).scalar()
        args = dict(actor_admin_id=self.f.operator.id, order_public_id=oid)
        def ship(number, quantity=1):
            return execute_order_shipping(self.f.engine, **args, shipping_carrier='测试',
                                          tracking_number=number, quantities={item_id: quantity})
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(ship, ['A', 'A']))
        self.assertEqual(sorted(x.replayed for x in results), [False, True])
        with self.assertRaises(ValueError):
            execute_order_completion(self.f.engine, **args)
        with self.assertRaises(ValueError):
            ship('B', 2)
        ordered = self.call('GET', '/' + oid).json()
        self.assertEqual((ordered['status'], len(ordered['parcels'])), ('PARTIALLY_SHIPPED', 1))
        from app.mall.fulfillment_notice import supplier_packet
        with Session(self.f.engine) as db:
            order = db.query(Order).filter_by(order_public_id=oid).one()
            packet = supplier_packet(db, order, self.f.supplier.supplier_public_id)
            self.assertEqual(packet['items'][0]['quantity'], 1)
        self.assertEqual(ship('B').status, 'SHIPPED')
        self.assertTrue(ship('A').replayed)
        with self.assertRaises(ValueError):
            # The last parcel's number does not describe all units in this order.
            execute_order_shipping(self.f.engine, **args, shipping_carrier='测试', tracking_number='B')
        final = self.call('GET', '/' + oid).json()
        self.assertEqual([p['items'][0]['quantity'] for p in final['parcels']], [1, 1])
        self.assertEqual([p['tracking_number'] for p in final['parcels']], ['A','B'])

    def test_disabled_actor_cannot_prepare_or_attest(self):
        from app.mall.supplier_handoff import handle_packet, packet_history
        from app.models import User
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        args = (self.f.engine, self.f.operator.id, oid, self.f.supplier.supplier_public_id)
        receipt, _ = handle_packet(*args)
        with Session(self.f.engine) as db:
            db.get(User, self.f.operator.id).is_active = False
            db.commit()
        for prep in (None, receipt):
            with self.assertRaises(PermissionError):
                handle_packet(*args, preparation_id=prep)
        with Session(self.f.engine) as db:
            row, = packet_history(db, oid)
            self.assertIsNone(row.confirmed_at)

    def test_packet_denies_inactive_supplier_even_with_enabled_profile(self):
        from app.models import Supplier
        from app.mall.fulfillment_notice import supplier_packet
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        with Session(self.f.engine) as db:
            supplier = db.get(Supplier, self.f.supplier.id)
            supplier.is_active = False
            db.commit()
            order = db.query(Order).filter_by(order_public_id=oid).one()
            with self.assertRaises(ValueError):
                supplier_packet(db, order, supplier.supplier_public_id)

    def test_packet_denies_stale_fulfilling_order_after_shipping(self):
        from app.mall.fulfillment_notice import supplier_packet
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        with Session(self.f.engine) as reader:
            cached = reader.query(Order).filter_by(order_public_id=oid).one()
        self.assertEqual(cached.status, 'FULFILLING')
        self.f.ship(oid)
        with Session(self.f.engine) as db, self.assertRaises(ValueError):
            supplier_packet(db, cached, self.f.supplier.supplier_public_id)

    def test_packet_denies_restored_address_when_erasure_receipt_exists(self):
        from app.models import OrderPrivacyReceipt
        from app.mall.fulfillment_notice import supplier_packet
        oid = self.place()['order_public_id']
        self.f.fulfill(oid)
        with Session(self.f.engine) as db:
            order = db.query(Order).filter_by(order_public_id=oid).one()
            snapshot = db.get(OrderDeliverySnapshot, order.id)
            db.add(OrderPrivacyReceipt(order_id=order.id, request_digest=snapshot.request_digest,
                                       policy_version='test-only', cleared_at=fixtures.NOW))
            db.flush()
            with self.assertRaises(ValueError):
                supplier_packet(db, order, self.f.supplier.supplier_public_id)

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
        from datetime import datetime, timedelta, timezone
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
            cleared_at = datetime.fromisoformat(journal[0]['cleared_at'])
            utc_journal = [dict(journal[0], cleared_at=cleared_at.astimezone(timezone.utc).isoformat())]
            restore_receipts(db, utc_journal)
            self.assertIsNone(db.get(OrderDeliverySnapshot,order.id));db.commit()
        with Session(self.f.engine) as db:
            self.assertEqual(export_receipts(db), journal)
            restore_receipts(db, journal)
            with self.assertRaisesRegex(ValueError, '清理凭据冲突'):
                restore_receipts(db, [dict(journal[0], cleared_at=(cleared_at + timedelta(seconds=1)).isoformat())])
        self.assertEqual(self.call('POST',body=self.body).status_code,200)
