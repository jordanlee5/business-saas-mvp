"""Synthetic checks mounted into an immutable image, never a production command."""
import json
import os
from pathlib import Path
import sys


def context():
    value = json.loads(Path('/checks/context.json').read_text())
    if (value.get('scope') != 'isolated-candidate-http'
            or value.get('nonce') != os.environ.get('CANDIDATE_REHEARSAL_NONCE')
            or len(value.get('nonce', '')) != 32):
        raise RuntimeError('Dedicated synthetic rehearsal marker required')
    from tools.staging_postgres import configure
    configure()
    os.environ['MINIPROGRAM_SESSION_SIGNING_KEY'] = Path('/data/session.key').read_text()
    return value


def seed(settings):
    from datetime import timedelta
    from PIL import Image
    from app.auth import get_password_hash
    from app.database import SessionLocal
    from app import models
    from app.admin_permissions import PRIMARY_REVIEWER
    from app.mall.miniprogram_session_service import issue_session
    from app.time_utils import utc8_now
    import test_order_service as fixtures
    now = utc8_now()
    fixtures.NOW = now
    with SessionLocal() as db:
        assert db.query(models.User).count() == 0, 'Seed only an empty new rehearsal database'
        f = fixtures.OrderServiceTests(); f.db = db; f.seed_order_fixture()
        # The domain fixture's relative media path is not a public catalog URL.
        db.query(models.ProductMedia).filter_by(product_id=f.product.id).one().image_path = '/uploads/mall_products/PRD-ORDER-001/main.webp'
        f.operator.password_hash = get_password_hash(settings['password'])
        f.operator.must_change_password = False
        supervisor = models.User(username='candidate-supervisor', password_hash=get_password_hash(settings['password']),
                                 role='admin', admin_level='super_admin', is_active=True, must_change_password=False)
        reviewer = models.User(username='candidate-reviewer', password_hash=get_password_hash(settings['password']),
                               role='admin', admin_level=PRIMARY_REVIEWER, is_active=True, must_change_password=False)
        db.add_all([supervisor, reviewer]); db.flush()
        db.add(models.MemberQualification(business_record_id=f.early_grant.business_record_id,member_id=f.member.id,
            enabled=True, valid_from=now-timedelta(days=1), valid_until=now+timedelta(days=20), version=1,
            confirmed_by=supervisor.id,confirmed_at=now,bound_at=now,updated_at=now))
        other = models.Member(member_public_id='MEM-CANDIDATE-OTHER',is_active=True)
        db.add(other); db.flush()
        bindings = [models.MemberWechatBinding(member_id=f.member.id,wechat_app_id=settings['app_id'],openid='candidate-virtual-only'),
                    models.MemberWechatBinding(member_id=other.id,wechat_app_id=settings['app_id'],openid='candidate-other-virtual-only')]
        db.add_all(bindings); db.flush()
        tokens = [issue_session(b.id,settings['app_id'],b.openid)[0] for b in bindings]
        fixture = dict(token=tokens[0],other_token=tokens[1],member_public_id=f.member.member_public_id,
                       username=f.operator.username,sku_id=f.sku_one.id)
        db.commit()
    target=Path('/data/uploads/mall_products/PRD-ORDER-001/main.webp');target.parent.mkdir(parents=True,exist_ok=True)
    Image.new('RGB',(60,60),'green').save(target,'WEBP')
    Path('/data/fulfillment.json').write_text(json.dumps({'operator_name':'隔离联验运营主体','operator_contact':'help@example.invalid',
        'suppliers':{'SUP-ORDER-001':{'enabled':True,'name':'虚拟发货方','agreement_ref':'candidate-http-only'}}}))
    Path('/data/client-fixture.json').write_text(json.dumps(fixture))
    print(json.dumps({'seeded':True,'virtual_members':2,'real_identity_or_sms_used':False}))


def check(settings, restart=False):
    from io import BytesIO
    from urllib.parse import parse_qs, urlsplit
    import httpx
    from PIL import Image
    from app.database import SessionLocal
    from app import models
    from app.mall import home_entries, home_banners
    from tools.rehearse_member_fulfillment import journey
    fixture=json.loads(Path('/data/client-fixture.json').read_text())
    result={'scope':'isolated-fixed-image-http','checks':[]}
    def mark(name): result['checks'].append(name)
    def image(color='green',size=(300,300)):
        stream=BytesIO();Image.new('RGB',size,color).save(stream,'PNG');return stream.getvalue()
    prefix='/api/miniprogram/v1'
    with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=20) as admin, \
         httpx.Client(base_url=admin.base_url,trust_env=False,timeout=20,headers={'Authorization':'Bearer '+fixture['token']}) as member, \
         httpx.Client(base_url=admin.base_url,trust_env=False,timeout=20) as anon:
        assert anon.get('/health').status_code==200
        assert anon.get('/mall-home').status_code==401
        login=anon.get('/login');assert 'data-enabled="false"' in login.text and '暂未启用' in login.text
        for path,body in [('request-code',dict(phone='13800000001',captcha_ticket='virtual',captcha_randstr='virtual')),
                          ('verify',dict(challenge_id='virtual',code='000000'))]:
            response=anon.post('/login/phone/'+path,json=body)
            assert response.status_code==503 and response.headers['cache-control']=='no-store, private'
        with SessionLocal() as db:
            assert all(db.query(model).count()==0 for model in (models.AdminPhoneBinding,models.AdminPhoneChallenge,models.AdminPhoneLimit))
        mark('health_anonymous_admin_denied_and_real_phone_factory_closed')
        assert admin.post('/login',data={'username':fixture['username'],'password':settings['password']}).status_code==302
        assert admin.get('/mall-home').status_code==200
        csrf=admin.cookies['mall_home_csrf']
        if restart:
            assert len(member.get(prefix+'/home-entries').json())==8
            assert [x['slot'] for x in member.get(prefix+'/home-banners').json()]==[2,1]
            for row in member.get(prefix+'/home-entries').json():
                assert row['image_url'] and member.get(row['image_url']).status_code==200
            assert member.get(prefix+'/orders').json()['total']>=4
            assert member.get(prefix+'/points/summary').json()['available_points']=='170.00'
            mark('isolated_app_restart_preserves_media_orders_and_balances')
        else:
            assert all(x['image_url'] is None for x in member.get(prefix+'/home-entries').json())
            assert member.get(prefix+'/home-banners').json()==[]
            mark('empty_configuration_preserves_default_entrances_and_green_fallback')
            for key,_ in home_entries.ENTRIES:
                response=admin.post('/mall-home/'+key,data={'revision':0,'action':'upload','csrf_token':csrf},
                                    files={'image_file':('virtual.png',image(),'image/png')})
                assert response.status_code==303 and 'error=' not in response.headers['location']
            rows=member.get(prefix+'/home-entries').json();assert len(rows)==8
            for row in rows:
                response=member.get(row['image_url']);assert response.status_code==200 and response.headers['cache-control']=='no-store, private'
                assert anon.get(row['image_url']).status_code in (302,401)
                assert admin.get(row['image_url'],headers={'Authorization':'Bearer invalid'}).status_code==401
            original=rows[0]['image_url']
            response=admin.post('/mall-home/home',data={'revision':1,'action':'upload','csrf_token':csrf},
                                files={'image_file':('replacement.png',image('red'),'image/png')})
            assert 'error=' not in response.headers['location']
            response=admin.post('/mall-home/home',data={'revision':1,'action':'reset','csrf_token':csrf})
            assert 'error=' in response.headers['location'] and member.get(original).status_code==404
            assert home_entries.image_target(original).is_file()
            assert admin.post('/mall-home/home',data={'revision':2,'action':'reset','csrf_token':'invalid'}).status_code==403
            mark('eight_uploads_replace_stale_edit_csrf_and_private_media')
            for slot,sort in [(1,20),(2,10)]:
                response=admin.post('/mall-home/banners/'+str(slot),data={'revision':0,'title':'虚拟轮播 '+str(slot),
                    'sort_order':sort,'enabled':'true','target_category':'home','csrf_token':csrf},
                    files={'image_file':('banner.png',image(size=(1000,400)),'image/png')})
                assert response.status_code==303 and 'error=' not in response.headers['location']
            banners=member.get(prefix+'/home-banners').json();assert [b['slot'] for b in banners]==[2,1]
            url=banners[0]['image_url'];assert member.get(url).status_code==200
            for version,enabled in [(1,'false'),(2,'true')]:
                response=admin.post('/mall-home/banners/2',data={'revision':version,'action':'toggle','enabled':enabled,'csrf_token':csrf})
                assert 'error=' not in response.headers['location']
                if enabled=='false':assert member.get(url).status_code==404 and len(member.get(prefix+'/home-banners').json())==1
            with httpx.Client(base_url=admin.base_url,trust_env=False) as reviewer:
                reviewer.post('/login',data={'username':'candidate-reviewer','password':settings['password']})
                assert reviewer.get('/mall-home').status_code in (302,403)
            mark('carousel_upload_sort_disable_enable_and_operator_permission')
            result['business_checkpoints']=journey(admin,member,fixture['username'],settings['password'],fixture['sku_id'])
            order_id=member.get(prefix+'/orders').json()['items'][0]['order_public_id']
            assert anon.get(prefix+'/orders/'+order_id).status_code==401
            headers={'Authorization':'Bearer '+fixture['other_token']}
            assert anon.get(prefix+'/orders/'+order_id,headers=headers).status_code==404
            assert anon.get(prefix+'/home-entries',headers=headers).status_code==403
            mark('real_HTTP_redemption_idempotency_fulfill_ship_complete_refund_cancel_and_member_isolation')
        admin.get('/logout')
        assert admin.get('/mall-home').status_code==401
        mark('admin_logout_revokes_access')
    Path('/data/'+('restart-result.json' if restart else 'http-result.json')).write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2))
    print(json.dumps({'checks':result['checks'],'business_checkpoints':len(result.get('business_checkpoints',[]))}))


if __name__=='__main__':
    settings=context()
    if sys.argv[1]=='seed':seed(settings)
    elif sys.argv[1] in ('check','restart'):check(settings,sys.argv[1]=='restart')
    else:raise SystemExit('Choose seed, check or restart')
