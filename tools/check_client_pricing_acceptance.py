"""Exercise a disposable, explicitly selected synthetic package over real HTTP.

Never run on the user's fresh acceptance package: this intentionally creates
virtual orders/uploads and changes virtual rules. No database connection supplied
by the environment is used. Results contain labels/counts, never session tokens.
"""
import argparse
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit,parse_qs
import httpx
from openpyxl import load_workbook


class Fields(HTMLParser):
    def __init__(self,document,action):
        super().__init__();self.action=action;self.inside=False;self.values={};self.feed(document)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':self.inside=attrs.get('action')==self.action
        elif self.inside and tag=='input' and attrs.get('type')=='hidden':self.values[attrs['name']]=attrs.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.inside=False


def check(root):
    root=Path(root).resolve();settings=json.loads((root/'private-local.json').read_text(encoding='utf-8'))
    assert settings['mode']=='LOCAL_SYNTHETIC_CLIENT_PRICING_ONLY'
    base='http://127.0.0.1:'+str(settings['port']);evidence=[]
    fixture=json.loads((root/'data/fixture-index.json').read_text(encoding='utf-8'))
    recovery=root/'data/initial-restore-point/acceptance.db'
    digest=hashlib.sha256(recovery.read_bytes()).hexdigest()
    assert digest==(recovery.parent/'sha256.txt').read_text(encoding='ascii')
    def record(label):evidence.append(label);print('PASS '+label,flush=True)
    def read(sql,parameters=()):
        with sqlite3.connect((root/'data/acceptance.db').as_uri()+'?mode=ro',uri=True) as db:return db.execute(sql,parameters).fetchall()
    with httpx.Client(base_url=base,timeout=20,trust_env=False) as admin:
        health=admin.get('/__acceptance/health').json();assert health['identity']==settings['identity'] and health['sms']=='disabled'
        portal=admin.get('/__acceptance');nonce=re.search(r'name="nonce" value="([^"]+)"',portal.text)[1]
        assert all(key.startswith('uat_'+settings['identity']+'_') for key in admin.cookies.keys())
        assert admin.post('/__acceptance/enter',data={'nonce':'bad','target':'/mall-clients'}).status_code==403
        assert admin.get('/__acceptance',headers={'Host':'foreign.invalid'}).status_code==403
        assert admin.post('/__acceptance/member-session',headers={'Origin':'https://foreign.invalid'},json={'profile':'a1'}).status_code==403
        assert admin.get('/api/miniprogram/v1/products').status_code==401
        assert admin.post('/__acceptance/member-session',headers={'x-acceptance-csrf':nonce},json={'profile':'not-listed'}).status_code==400
        record('loopback, csrf, identity allowlist and unique cookies')
        def login(role='operator',target='/mall-clients'):
            response=admin.post('/__acceptance/enter',data={'nonce':nonce,'role':role,'target':target})
            assert response.status_code==303,response.text
        tokens={}
        for profile in fixture['profiles']:
            response=admin.post('/__acceptance/member-session',headers={'x-acceptance-csrf':nonce},json={'profile':profile})
            assert response.status_code==200;tokens[profile]=response.json()['token']
        def api(profile,path,method='GET',body=None,status=200):
            response=admin.request(method,'/api/miniprogram/v1'+path,headers={'Authorization':'Bearer '+tokens[profile]},**({'json':body} if body is not None else {}))
            assert response.status_code==status,(profile,path,response.status_code,response.text[:300])
            assert 'no-store' in response.headers['cache-control'];return response.json()
        def product(profile):return api(profile,'/products/PRD-CLIENT-UAT')
        def balance(profile):return api(profile,'/points/summary')['available_points']
        def body(profile):
            p=product(profile);s=p['skus'][0];address=api(profile,'/addresses')['items'][0]
            lines=[dict(product_public_id=p['product_public_id'],sku_code=s['sku_code'],quantity=1,expected_points_price=s['points_price'])]
            notice=api(profile,'/orders/fulfillment-preview','POST',dict(lines=lines))
            return dict(request_id=secrets.token_hex(16),address_public_id=address['address_public_id'],
                expected_address={k:v for k,v in address.items() if k not in ('address_public_id','is_default')},
                lines=lines,fulfillment_digest=notice['digest'],pricing_context=p['pricing_context'])
        def price(key,value=None):
            page=admin.get('/mall-price-rules');assert page.status_code==200
            form=Fields(page.text,'/mall-price-rules/preview').values
            preview=admin.post('/mall-price-rules/preview',data={**form,'organization_id':fixture['organizations'][key],
                'mode':'inherit' if value is None else 'custom','percent':value or ''})
            assert preview.status_code==200,preview.text[:300]
            assert '45.00' in preview.text
            saved=admin.post('/mall-price-rules/save',data={**Fields(preview.text,'/mall-price-rules/save').values,'confirmed':'true'})
            assert saved.status_code==303 and 'error' not in parse_qs(urlsplit(saved.headers['location']).query)
        login('uploader','/upload-excel')
        assert admin.get('/mall-clients').status_code==403
        assert admin.get('/upload-excel/mall-template/'+str(fixture['organizations']['a2'])).status_code==404
        template=admin.get('/upload-excel/mall-template/'+str(fixture['organizations']['a1']))
        assert template.status_code==200
        book=load_workbook(BytesIO(template.content));assert book.active['F2'].value=='UAT-A1';book.close()
        before=read('SELECT COUNT(*) FROM upload_batches')[0][0]
        for path in sorted((root/'虚拟名单').glob('*.xlsx')):
            response=admin.post('/upload-excel',data={'redemption_mode':'MALL_REDEMPTION','claim_deadline':'2099-12-31'},
                files={'file':(path.name,path.read_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
            assert response.status_code==(200 if path.name.startswith('01') else 400),(path.name,response.status_code)
        assert read('SELECT COUNT(*) FROM upload_batches')[0][0]==before+1
        assert read('SELECT organization_code FROM client_batch_ownerships ORDER BY batch_id DESC LIMIT 1')[0][0]=='UAT-A1'
        record('real template and uploads: correct accepted, missing/mixed/unauthorized rejected atomically')
        login()
        for profile,expected in [('a1','90.00'),('a2','110.00'),('b','100.00')]:
            p=product(profile);assert p['skus'][0]['points_price']==expected
            for field in ('cost_price','ratio_bps','base_price','organization_id'):assert field not in json.dumps(p)
            assert balance(profile)=='1000.00'
        record('three member prices 90/110/100 with no base/cost/rule leakage')
        stale=body('a1');count=read('SELECT COUNT(*) FROM orders')[0][0]
        price('a','80')
        for profile,expected in [('a1','80.00'),('a2','110.00'),('b','100.00')]:assert product(profile)['skus'][0]['points_price']==expected
        api('a1','/orders','POST',stale,status=409);assert read('SELECT COUNT(*) FROM orders')[0][0]==count
        record('immediate HQ inheritance, branch override and stale checkout refusal')
        price('a1','100');assert product('a1')['skus'][0]['points_price']=='100.00'
        price('a1');assert product('a1')['skus'][0]['points_price']=='80.00'
        assert read('SELECT points_price,cost_price FROM product_skus')==[(100,45)]
        record('explicit 100 percent versus inheritance; base and cost unchanged')
        for profile in ('limited','sibling'):
            assert balance(profile)=='70.00';api(profile,'/orders','POST',body(profile),status=409)
            assert api(profile,'/points/grants')['total']==1 and api(profile,'/points/ledger')['total']==1
        assert read('SELECT COUNT(*) FROM orders')[0][0]==count
        api('conflict','/products/PRD-CLIENT-UAT',status=403);api('conflict','/points/summary',status=403)
        record('both cross-client and sibling pooling denied; conflicting qualifications stop new trade')
        quote=body('a1');order=api('a1','/orders','POST',quote,status=201);oid=order['order_public_id']
        assert order['organization']['code']=='UAT-A1' and order['total_points']=='80.00'
        assert balance('a1')=='920.00';assert api('a1','/orders','POST',quote)['order_public_id']==oid
        api('a2','/orders/'+oid,status=404);api('a2','/orders/'+oid+'/cancel','POST',status=404)
        api('a1','/orders/'+oid+'/cancel','POST');assert balance('a1')=='1000.00' and balance('a2')=='1000.00' and balance('b')=='1000.00'
        record('real checkout, replay, private order access and original-institution cancellation')
        def action(oid,kind,**fields):
            path='/mall-orders/'+oid;page=admin.get(path);assert page.status_code==200
            endpoint=path+'/'+kind;form=Fields(page.text,endpoint).values;assert form,kind
            response=admin.post(endpoint,data={**form,**fields})
            assert response.status_code==303 and 'error' not in parse_qs(urlsplit(response.headers['location']).query),(kind,response.headers.get('location'))
        order=api('a1','/orders','POST',body('a1'),status=201);oid=order['order_public_id']
        action(oid,'fulfill');action(oid,'ship',shipping_carrier='虚拟物流',tracking_number='UAT-HTTP-ONLY');action(oid,'complete')
        price('a','120');action(oid,'refund',reason='独立虚拟退款')
        original=api('a1','/orders/'+oid);assert original['status']=='REFUNDED' and original['total_points']=='80.00'
        assert balance('a1')=='1000.00'
        record('fulfillment shipping completion and refund retain original 80 despite new 120 price')
        assert balance('history')=='200.00';historical=fixture['history_order']
        history=api('history','/orders/'+historical);assert history['organization']['code']=='UAT-A1' and history['total_points']=='90.00'
        def partitions():return read('SELECT ig.organization_code,pg.available_points FROM institution_grants ig JOIN points_grants pg ON pg.id=ig.grant_id JOIN points_accounts pa ON pa.id=pg.account_id WHERE pa.member_id=? ORDER BY ig.organization_code',(fixture['members']['history'],))
        assert partitions()==[('UAT-A1',910),('UAT-B',200)]
        action(historical,'refund',reason='虚拟历史订单退款')
        assert partitions()==[('UAT-A1',1000),('UAT-B',200)] and balance('history')=='200.00'
        record('historical A1 refund 90 returns to A1 while current B remains 200')
        detail=admin.get('/member-points/'+str(fixture['members']['history']));assert detail.status_code==200 and '按客户机构分别记账' in detail.text
        export=admin.get('/member-points/'+str(fixture['members']['history'])+'/export');assert export.status_code==200
        book=load_workbook(BytesIO(export.content));assert '机构分账' in book.sheetnames;book.close()
        assert admin.get('/mall-orders/export').status_code==200
        record('institution accounts and real admin exports')
        login_page=admin.get('/login');assert '未启用' in login_page.text
        assert admin.post('/login/phone/request-code',json={}).status_code==503
        assert read('SELECT COUNT(*) FROM admin_phone_challenges')[0][0]==0
        assert hashlib.sha256(recovery.read_bytes()).hexdigest()==digest
        record('SMS disabled and initial restore point unchanged')
    result=dict(mode='synthetic_http_acceptance',application_commit=settings['application_commit'],checks=evidence,
        passed=len(evidence),real_data_access=False,cloud_upgrade=False,sms_enabled=False,restore_point_unchanged=True)
    (root/'data/http-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True)
    parser.add_argument('--exercise-synthetic-data',required=True,action='store_true')
    args=parser.parse_args();check(args.root)
