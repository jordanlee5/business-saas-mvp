"""Exercise real HTTP handlers and isolated PostgreSQL; never call WeChat."""
import os,json,secrets,hashlib
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
import httpx
from tools.staging_postgres import configure,preflight
assert os.environ.get('COLD_RECOVERY_REHEARSAL') == '1', 'Explicit disposable recovery acknowledgement required'
assert os.environ.get('RECOVERY_PROJECT', '').startswith('saas-pg-test-cold-'), 'Disposable cold project required'
url=configure();preflight(url)
assert os.environ.get('WECHAT_MINIPROGRAM_APP_ID')=='virtual-cvm-staging-app'
from app.database import SessionLocal
from app import models as m
from app.mall.miniprogram_session_service import issue_session
from app.mall import qualification_service as qualification
from app.mall import rights_service
from app.time_utils import utc8_now
state=json.loads(Path('/data/cvm-virtual-state.json').read_text())
assert state['virtual_only'] is True
tokens=[]
with SessionLocal() as db:
    assert {x.member_public_id for x in db.query(m.Member)} == {'MEM-CVM-TEST-001', 'MEM-CVM-TEST-002'}
    original_passwords = {x.id: x.password_hash for x in db.query(m.User)}
    for item in state['members']:
        binding=db.get(m.MemberWechatBinding,item['binding_id'])
        assert binding.wechat_app_id=='virtual-cvm-staging-app'
        tokens.append(issue_session(binding.id,binding.wechat_app_id,binding.openid)[0])
client=httpx.Client(base_url='http://127.0.0.1:8000',timeout=20,follow_redirects=False)
checks=[]
prefix='/api/miniprogram/v1'
def request(label,method,path,status,owner=0,**kwargs):
    headers={} if owner is None else {'Authorization':'Bearer '+tokens[owner]}
    response=client.request(method,path,headers=headers,**kwargs)
    assert response.status_code==status,(label,response.status_code,status)
    checks.append({'check':label,'status':response.status_code})
    return response
def balances():
    with SessionLocal() as db:
        return {'points':[[str(x.available_points),str(x.reserved_points)] for x in db.query(m.PointsAccount).order_by(m.PointsAccount.id)],'inventory':[[x.on_hand_quantity,x.reserved_quantity] for x in db.query(m.InventoryBalance).order_by(m.InventoryBalance.id)]}
def change(action,**kwargs):
    with SessionLocal.begin() as db:
        row=db.query(m.MemberQualification).filter_by(member_id=state['members'][0]['member_id']).one()
        qualification.update(db,actor_id=state['admin_id'],qualification_id=row.id,version=row.version,action=action,reason='云端虚拟验收',**kwargs)
request('health','GET','/health',200,owner=None)
request('anonymous_catalog_denied','GET',prefix+'/products',401,owner=None)
request('anonymous_image_denied','GET',state['image'],401,owner=None)
request('anonymous_thumbnail_denied','GET',state['thumbnail'],401,owner=None)
request('eligible_catalog','GET',prefix+'/products',200)
request('eligible_detail','GET',prefix+'/products/PRD-CVM-TEST',200)
image=request('eligible_image','GET',state['image'],200)
assert image.headers['content-type']=='image/webp'
request('eligible_thumbnail','GET',state['thumbnail'],200)
before=balances()
assert before['points'][0]==['500.00','0.00'],before
lines=[{'product_public_id':'PRD-CVM-TEST','sku_code':'CVM-CUP-1','quantity':1,'expected_points_price':'30.00'}]
notice=request('fulfillment_preview','POST',prefix+'/orders/fulfillment-preview',200,json={'lines':lines}).json()
body={'request_id':secrets.token_hex(16),'address_public_id':state['members'][0]['address_public_id'],'expected_address':state['address'],'lines':lines,'fulfillment_digest':notice['digest']}
order=request('checkout','POST',prefix+'/orders',201,json=body).json()
oid=order['order_public_id']
assert request('idempotent_checkout','POST',prefix+'/orders',200,json=body).json()['order_public_id']==oid
request('owner_order','GET',prefix+'/orders/'+oid,200)
request('other_member_order_denied','GET',prefix+'/orders/'+oid,404,owner=1)
change('DISABLE')
request('disabled_catalog_denied','GET',prefix+'/products',403)
request('disabled_image_denied','GET',state['image'],403)
request('disabled_history_retained','GET',prefix+'/orders/'+oid,200)
newbody={**body,'request_id':secrets.token_hex(16)}
disabled_balances=balances()
request('disabled_checkout_denied','POST',prefix+'/orders',403,json=newbody)
assert balances()==disabled_balances
rights=request('disabled_rights_submit','POST',prefix+'/rights-requests',201,json={'request_key':secrets.token_hex(16),'kind':'ACCESS','description':'云端虚拟会员申请查询测试资料'}).json()
rid=rights['public_id']
request('other_member_rights_denied','GET',prefix+'/rights-requests/'+rid,404,owner=1)
with SessionLocal.begin() as db:
    for action,note in [('verify','虚拟人工核验通过'),('progress','虚拟处理进度更新'),('finish','虚拟申请处理完成')]:
        case=db.query(m.MemberRightsRequest).filter_by(public_id=rid).one()
        rights_service.update(db,rid,state['admin_id'],case.version,action,'IN_PERSON',note)
result=request('owner_rights_result','GET',prefix+'/rights-requests/'+rid,200).json()
assert result['status']=='已办结'
change('ENABLE')
now=utc8_now()
change('VALIDITY',valid_from=now-timedelta(days=2),valid_until=now-timedelta(seconds=1))
request('expired_catalog_denied','GET',prefix+'/products',403)
request('expired_image_denied','GET',state['image'],403)
request('expired_checkout_denied','POST',prefix+'/orders',403,json=newbody)
assert balances()==disabled_balances
request('expired_history_retained','GET',prefix+'/orders/'+oid,200)
request('expired_rights_retained','GET',prefix+'/rights-requests/'+rid,200)
change('VALIDITY',valid_from=now-timedelta(days=1),valid_until=now+timedelta(days=30))
request('qualification_restored','GET',prefix+'/products',200)
request('cancel_order','POST',prefix+'/orders/'+oid+'/cancel',200)
assert balances()==before
admin=httpx.Client(base_url='http://127.0.0.1:8000',timeout=20,follow_redirects=False)
from app.auth import get_password_hash
password=secrets.token_urlsafe(32)
username='cold-check-'+secrets.token_hex(6)
with SessionLocal.begin() as db:
    db.add(m.User(username=username,password_hash=get_password_hash(password),role='admin',
                  admin_level='super_admin',is_active=True,must_change_password=True))
response=admin.post('/login',data={'username':username,'password':password})
assert response.status_code==302 and response.headers['location']=='/change-password'
assert admin.get('/change-password').status_code==200
assert admin.get('/mall-catalog').headers.get('location')=='/change-password'
checks.append({'check':'admin_login_requires_private_password_change','status':302})
logout=admin.get('/logout')
assert admin.get('/dashboard').headers.get('location')=='/login'
checks.append({'check':'logout_revokes_session','status':302})
with SessionLocal() as db:
    assert {key: db.get(m.User, key).password_hash for key in original_passwords} == original_passwords
report={'original_passwords_unchanged':True,'result':'PASS','checks':checks,'checks_count':len(checks),'balances_restored':balances(),'image_sha256':hashlib.sha256(image.content).hexdigest(),'order_id':oid,'rights_id':rid,'real_wechat_called':False,'real_logistics_called':False,'scope':'Actual HTTP in disposable cold recovery from cloud virtual backup; no external network or real provider calls'}

print(json.dumps(report,ensure_ascii=False))
