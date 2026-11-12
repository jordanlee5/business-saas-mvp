"""Loopback-only harness for a committed application copy and synthetic DB.

Never imported by the production application. A new generated package and its
matching manifest are required. No externally supplied DB URL/config is used.
"""
import argparse
from datetime import timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time


def configure(root):
    root=Path(root).resolve()
    settings=json.loads((root/'private-local.json').read_text(encoding='utf-8'))
    manifest=json.loads((root/'acceptance-manifest.json').read_text(encoding='utf-8'))
    if settings['mode']!='LOCAL_SYNTHETIC_ACCEPTANCE_ONLY' or manifest['mode']!=settings['mode']:
        raise ValueError('Not a generated synthetic acceptance package')
    app_root=root/'application'
    if (app_root/'.git').exists() or (root/'.git').exists():raise ValueError('Refusing a working repository')
    for relative,digest in manifest['production_code'].items():
        path=(app_root/relative).resolve()
        if not path.is_relative_to(app_root) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('Candidate application source changed; create a new package')
    db_path=root/'data/acceptance.db'
    if not db_path.resolve().is_relative_to(root):raise ValueError('Acceptance database escapes its package')
    os.environ['DATABASE_URL']='sqlite:///'+db_path.as_posix()
    os.environ['ADMIN_CAPTCHA_CONFIG']=''
    os.environ['WECHAT_MINIPROGRAM_APP_ID']='local-synthetic-home-acceptance'
    os.environ['MINIPROGRAM_SESSION_SIGNING_KEY']=settings['signing_key']
    for key in list(os.environ):
        if any(name in key.upper() for name in ('WEBHOOK','APP_SECRET','APPSECRET','WECHAT_CONFIG')):
            os.environ.pop(key,None)
    os.chdir(app_root);sys.path.insert(0,str(app_root))
    # All HTTP provider integrations fail closed inside this harness.
    import requests
    def deny_network(*args,**kwargs):raise RuntimeError('External provider disabled in local acceptance')
    requests.sessions.Session.request=deny_network
    from app import browser_sessions
    browser_sessions.COOKIE_NAME='acceptance_session_'+settings['identity']
    from app import main
    from app.api import home_entries,home_banners,admin_phone
    home_entries.CSRF_COOKIE=home_banners.CSRF_COOKIE='acceptance_home_'+settings['identity']
    admin_phone.CSRF_COOKIE='acceptance_phone_'+settings['identity']
    return root,settings,main,db_path


def initialize(root,settings,main,db_path):
    if db_path.exists():raise ValueError('Existing acceptance data must never be initialized again')
    from alembic import command
    from alembic.config import Config
    from app import models
    from app.auth import get_password_hash
    from app.time_utils import utc8_now
    config=Config('alembic.ini');config.attributes['database_url']=os.environ['DATABASE_URL'];command.upgrade(config,'head')
    now=utc8_now()
    with main.SessionLocal.begin() as db:
        supervisor=models.User(username='acceptance-supervisor',password_hash=get_password_hash(secrets.token_urlsafe(24)),
            role='admin',admin_level='super_admin',is_active=True,must_change_password=False)
        operator=models.User(username='acceptance-operator',password_hash=get_password_hash(settings['operator_password']),
            role='admin',admin_level='operator',is_active=True,must_change_password=False)
        member=models.Member(member_public_id='MEM-ACCEPTANCE-001',is_active=True)
        db.add_all([supervisor,operator,member]);db.flush()
        record=models.BusinessRecord(user_id=operator.id,public_business_no='ACCEPTANCE-ONLY-001',
            name='虚拟验收会员',points_amount=0,redemption_mode='MALL_REDEMPTION',claim_status='ACTIVATED')
        db.add(record);db.flush()
        binding=models.MemberWechatBinding(member_id=member.id,wechat_app_id=os.environ['WECHAT_MINIPROGRAM_APP_ID'],openid='virtual-acceptance-only')
        db.add(binding)
        db.add(models.MemberQualification(business_record_id=record.id,member_id=member.id,enabled=True,
            valid_from=now-timedelta(days=1),valid_until=now+timedelta(days=30),version=1,
            confirmed_by=supervisor.id,confirmed_at=now,bound_at=now,updated_at=now))
    # Original green branding/default category illustrations are the initial state.
    recovery=root/'data/initial-restore-point';recovery.mkdir(exist_ok=False)
    with sqlite3.connect(db_path) as source,sqlite3.connect(recovery/'acceptance.db') as dest:source.backup(dest)
    print('Synthetic database migrated; initial restore point preserved',flush=True)


def application(root,settings,main):
    from fastapi import FastAPI,Request,HTTPException
    from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse,FileResponse,Response
    from app import models,admin_phone_login as phone
    from app.api import admin_phone as api
    from app.mall.miniprogram_session_service import issue_session
    from app.browser_sessions import COOKIE_NAME,LIFETIME
    origin=f"http://127.0.0.1:{settings['port']}";nonce_name='acceptance_gate_'+settings['identity']
    prefix='/__acceptance/virtual'

    def nonce(request):
        value=request.cookies.get(nonce_name,'')
        return value if len(value)==43 else secrets.token_urlsafe(32)

    def check_nonce(request,value):
        cookie=request.cookies.get(nonce_name,'')
        if len(cookie)!=43 or not secrets.compare_digest(cookie,value):raise HTTPException(403,'请从验收首页重新进入')

    def page(request,name):
        token=nonce(request)
        content=(root/'harness'/name).read_text(encoding='utf-8')
        for key,value in {'NONCE':token,'PROJECT':str(root/'miniprogram'),'SOURCE':settings['source_commit'][:7]}.items():
            content=content.replace('{{'+key+'}}',html.escape(value))
        response=HTMLResponse(content,headers={'Cache-Control':'no-store, private'})
        response.set_cookie(nonce_name,token,httponly=True,samesite='strict',path='/__acceptance')
        return response

    @main.app.middleware('http')
    async def local_boundary(request,call_next):
        if (not request.client or request.client.host not in ('127.0.0.1','::1') or
            request.headers.get('host')!=origin.removeprefix('http://') or
            request.headers.get('origin',origin)!=origin):
            return Response('仅限本机独立验收入口',status_code=403)
        path=request.url.path
        allowed=(path in ('/login','/logout','/dashboard','/health','/mall-home','/login/phone/request-code','/login/phone/verify') or
                 path.startswith(('/__acceptance','/static/','/mall-home/','/uploads/mall_home/','/uploads/mall_banners/')) or
                 path in ('/api/miniprogram/v1/home-entries','/api/miniprogram/v1/home-banners'))
        if not allowed:return HTMLResponse('此独立验收包只开放本轮三项功能。<a href="/__acceptance">返回验收首页</a>',status_code=403)
        response=await call_next(request)
        if 'text/html' in response.headers.get('content-type','') and not path.startswith('/__acceptance'):
            body=b''.join([part async for part in response.body_iterator]).decode('utf-8')
            badge='<div style="padding:10px;background:#fff1cd;color:#614719;text-align:center;font:14px Microsoft YaHei">独立验收 · 仅虚拟数据 · <a href="/__acceptance">返回验收首页</a></div>'
            body=body.replace('<body>','<body>'+badge,1)
            changed=HTMLResponse(body,status_code=response.status_code)
            changed.raw_headers=[(k,v) for k,v in response.raw_headers if k.lower()!=b'content-length']+[(b'content-length',str(len(body.encode())).encode())]
            return changed
        return response

    @main.app.get('/__acceptance')
    def index(request:Request):return page(request,'portal.html')

    @main.app.get('/__acceptance/view')
    def view(request:Request):return page(request,'viewer.html')

    @main.app.get('/__acceptance/health')
    def health():return {'mode':'LOCAL_SYNTHETIC_ACCEPTANCE_ONLY','identity':settings['identity'],'source':settings['source_commit'],'sms':'disabled'}

    @main.app.get('/__acceptance/samples.zip')
    def samples():return FileResponse(root/'素材包.zip',filename='三项验收素材包.zip',headers={'Cache-Control':'no-store'})

    @main.app.post('/__acceptance/enter')
    async def enter(request:Request):
        form=await request.form();check_nonce(request,str(form.get('nonce','')))
        with main.SessionLocal() as db:
            user=db.query(models.User).filter_by(username='acceptance-operator').one()
            token=main.browser_sessions.issue(user.id,user.password_hash)
        response=RedirectResponse('/mall-home',status_code=303)
        response.set_cookie(COOKIE_NAME,token,max_age=LIFETIME,httponly=True,samesite='strict')
        return response

    @main.app.post('/__acceptance/member-session')
    def member_session(request:Request):
        bootstrap=request.headers.get('x-acceptance-bootstrap','')
        if not secrets.compare_digest(bootstrap,settings['bootstrap']):check_nonce(request,request.headers.get('x-acceptance-csrf',''))
        with main.SessionLocal() as db:
            binding=db.query(models.MemberWechatBinding).filter_by(openid='virtual-acceptance-only').one()
            token,ttl=issue_session(binding.id,binding.wechat_app_id,binding.openid)
        return JSONResponse({'token':token,'expires_in':ttl},headers={'Cache-Control':'no-store'})

    # A separate mounted application is the ONLY place with a virtual phone adapter.
    # The main application's get_phone_service() remains None, even while this demo runs.
    proofs={};deliveries={}
    class VirtualHuman:
        def verify(self,ticket,randstr,client_ip):
            expires=proofs.pop(ticket,0)
            return expires>time.time() and randstr=='local-virtual-proof' and client_ip in ('127.0.0.1','::1')
    class VirtualSms:
        def enqueue(self,*,phone,code,request_id):
            if phone!='13800000001':return False
            if len(deliveries)>=200:deliveries.clear()
            deliveries[request_id]=code
            return True
    service=phone.PhoneLoginService(main.SessionLocal,key=bytes.fromhex(settings['phone_key']),sms=VirtualSms(),human=VirtualHuman())
    with main.SessionLocal() as db:
        supervisor=db.query(models.User).filter_by(username='acceptance-supervisor').one()
        operator=db.query(models.User).filter_by(username='acceptance-operator').one()
        bound=db.get(models.AdminPhoneBinding,operator.id)
        ids=(supervisor.id,operator.id)
    if bound is None:service.bind(*ids,'13800000001',0)
    demo=FastAPI();demo.include_router(api.router);demo.dependency_overrides[api.get_phone_service]=lambda:service

    @demo.get('/login')
    def virtual_login(request:Request):
        csrf=secrets.token_urlsafe(32)
        rendered=main.templates.get_template('login.html').render(request=request,error=None,captcha_required=False,
            captcha_unavailable=False,captcha_app_id='',phone_login_enabled=True,phone_captcha_app_id='virtual-only',phone_csrf=csrf)
        rendered=rendered.replace('https://turing.captcha.qcloud.com/TJCaptcha.js',prefix+'/captcha.js')
        rendered=rendered.replace('/static/phone-login.js',prefix+'/phone.js')
        start=rendered.index('<form id="login-form"');end=rendered.index('</form>',start)+len('</form>')
        rendered=rendered[:start]+rendered[end:]
        rendered=rendered.replace('系统登录','虚拟手机号验收').replace('业务数据管理 SaaS MVP','仅示例号码 13800000001 · 不发送真实短信')
        rendered=rendered.replace('<details class="phone-login"','<details open class="phone-login"')
        rendered=rendered.replace('</body>', '<p style="text-align:center"><a href="/__acceptance">返回验收首页</a></p><script src="'+prefix+'/inbox.js" defer></script></body>')
        response=HTMLResponse(rendered,headers={'Cache-Control':'no-store'})
        response.set_cookie(api.CSRF_COOKIE,csrf,httponly=True,samesite='strict',path=prefix)
        return response

    @demo.get('/phone.js')
    def phone_js():
        script=(root/'application/app/static/phone-login.js').read_text(encoding='utf-8')
        return Response(script.replace("'/login/phone/","'"+prefix+"/login/phone/"),media_type='text/javascript')

    @demo.get('/captcha.js')
    def captcha_js():return FileResponse(root/'harness/virtual-captcha.js',media_type='text/javascript')

    @demo.get('/inbox.js')
    def inbox_js():return FileResponse(root/'harness/virtual-inbox.js',media_type='text/javascript')

    @demo.post('/proof')
    def proof(request:Request):
        api.browser_nonce(request)
        for key in list(proofs):
            if proofs[key]<=time.time():proofs.pop(key,None)
        if len(proofs)>=100:raise HTTPException(429,'请稍后重试')
        ticket=secrets.token_urlsafe(32);proofs[ticket]=time.time()+120
        return JSONResponse({'ret':0,'ticket':ticket,'randstr':'local-virtual-proof'},headers={'Cache-Control':'no-store'})

    @demo.get('/inbox')
    def inbox(request:Request):
        browser=api.browser_nonce(request)
        with main.SessionLocal() as db:
            latest=db.query(models.AdminPhoneChallenge).filter_by(browser_digest=service.digest('browser',browser)).order_by(models.AdminPhoneChallenge.created_at.desc()).first()
            value=deliveries.get(latest.challenge_id,'') if latest and latest.expires_at>time.time() and latest.state=='ready' else ''
        return JSONResponse({'code':value},headers={'Cache-Control':'no-store'})

    main.app.mount(prefix,demo)
    return main.app


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True);parser.add_argument('--initialize',action='store_true')
    args=parser.parse_args();root,settings,main,db_path=configure(args.root)
    if args.initialize:initialize(root,settings,main,db_path)
    else:
        if not db_path.is_file():raise ValueError('Initialize a new synthetic package first')
        import uvicorn
        uvicorn.run(application(root,settings,main),host='127.0.0.1',port=settings['port'],access_log=False,log_level='warning')
