"""Local acceptance identity adapter; production pricing/ledger/routes stay unmodified."""
import argparse
import hashlib
import html
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys

MODE='LOCAL_SYNTHETIC_CLIENT_PRICING_ONLY'


def configure(root):
    root=Path(root).resolve()
    settings=json.loads((root/'private-local.json').read_text(encoding='utf-8'))
    manifest=json.loads((root/'acceptance-manifest.json').read_text(encoding='utf-8'))
    if settings.get('mode')!=MODE or manifest.get('mode')!=MODE:
        raise ValueError('Not a generated customer-pricing acceptance package')
    app_root=(root/'application').resolve()
    if (root/'.git').exists() or (app_root/'.git').exists() or not app_root.is_relative_to(root):
        raise ValueError('Refusing a repository or external application path')
    for base,group in [(app_root,'production_code'),(root/'harness','harness_sha256')]:
        for relative,digest in manifest[group].items():
            path=(base/relative).resolve()
            if not path.is_relative_to(base) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('Package source changed; create a new package')
    db_path=(root/'data/acceptance.db').resolve()
    if not db_path.is_relative_to(root):raise ValueError('Database escapes its package')
    for key in list(os.environ):
        if any(word in key.upper() for word in ('DATABASE','WEBHOOK','SECRET','WECHAT','MINIPROGRAM','MALL_','CAPTCHA')):
            os.environ.pop(key,None)
    os.environ.update(DATABASE_URL='sqlite:///'+db_path.as_posix(),ADMIN_CAPTCHA_CONFIG='',
        WECHAT_MINIPROGRAM_APP_ID='local-client-pricing-acceptance',MINIPROGRAM_SESSION_SIGNING_KEY=settings['signing_key'],
        MALL_FULFILLMENT_SETTINGS=str(root/'data/fulfillment.json'))
    os.chdir(app_root);sys.path.insert(0,str(app_root))
    # Windows asyncio uses a loopback socket pair internally; preserve that while
    # blocking off-host connections and all application HTTP provider calls.
    def deny(*args,**kwargs):raise RuntimeError('External connections disabled in local acceptance')
    original_connect,original_connect_ex=socket.socket.connect,socket.socket.connect_ex
    def local_connect(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1'):return deny()
        return original_connect(sock,address)
    def local_connect_ex(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1'):return deny()
        return original_connect_ex(sock,address)
    socket.socket.connect=local_connect;socket.socket.connect_ex=local_connect_ex
    import requests
    requests.sessions.Session.request=deny
    from app import main
    return root,settings,main,db_path


def initialize(root,settings,main,db_path):
    if db_path.exists() or (root/'data/initial-restore-point').exists():
        raise ValueError('Existing data or recovery point: initialization refused')
    from alembic import command
    from alembic.config import Config
    config=Config('alembic.ini');config.attributes['database_url']=os.environ['DATABASE_URL']
    command.upgrade(config,'head')
    (root/'data/fulfillment.json').write_text(json.dumps(dict(operator_name='虚拟验收兑换公司',
        operator_contact='acceptance@example.invalid',suppliers={'SUP-CLIENT-UAT':dict(name='虚拟验收受托方',
            agreement_ref='local-acceptance-only',enabled=True)}),ensure_ascii=False),encoding='utf-8')
    from seed import seed
    seed(root,main)
    recovery=root/'data/initial-restore-point';recovery.mkdir(exist_ok=False)
    with sqlite3.connect(db_path) as source,sqlite3.connect(recovery/'acceptance.db') as dest:source.backup(dest)
    digest=hashlib.sha256((recovery/'acceptance.db').read_bytes()).hexdigest()
    (recovery/'sha256.txt').write_text(digest,encoding='ascii')
    print('New synthetic package initialized; initial recovery point preserved',flush=True)


def application(root,settings,main):
    from fastapi import Request,HTTPException
    from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse,FileResponse,Response
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from app import models as m
    from app.mall.miniprogram_session_service import issue_session
    from app.browser_sessions import COOKIE_NAME,LIFETIME
    from seed import PROFILES
    origin=f"http://127.0.0.1:{settings['port']}";cookie_prefix='uat_'+settings['identity']+'_'
    fixture=json.loads((root/'data/fixture-index.json').read_text(encoding='utf-8'))

    def check_nonce(request,value):
        cookie=request.cookies.get('gate','')
        if len(cookie)!=43 or not secrets.compare_digest(cookie,value):
            raise HTTPException(403,'请从验收首页重新进入')

    def page(request,name):
        nonce=request.cookies.get('gate','')
        if len(nonce)!=43:nonce=secrets.token_urlsafe(32)
        content=(root/'harness'/name).read_text(encoding='utf-8')
        for key,value in {'NONCE':nonce,'COMMIT':settings['application_commit'][:7],
            'HISTORY_ORDER':fixture['history_order'],'PORT':str(settings['port'])}.items():
            content=content.replace('{{'+key+'}}',html.escape(value))
        response=HTMLResponse(content)
        response.set_cookie('gate',nonce,httponly=True,samesite='strict',path='/__acceptance')
        return response

    @main.app.middleware('http')
    async def boundary(request,call_next):
        if (not request.client or request.client.host not in ('127.0.0.1','::1') or
            request.headers.get('host')!=origin.removeprefix('http://') or
            request.headers.get('origin',origin)!=origin or request.headers.get('sec-fetch-site')=='cross-site'):
            return Response('仅限本机独立验收入口',status_code=403)
        # Browsers share cookies between ports. Namespace ALL cookies so old tests keep theirs.
        parsed=SimpleCookie();parsed.load(request.headers.get('cookie',''))
        supplied='; '.join(name[len(cookie_prefix):]+'='+item.coded_value for name,item in parsed.items() if name.startswith(cookie_prefix))
        request.scope['headers']=[(k,v) for k,v in request.scope['headers'] if k.lower()!=b'cookie']+[(b'cookie',supplied.encode())]
        path=request.url.path
        if path.startswith(('/api/miniprogram/v1/auth/','/api/miniprogram/v1/activation/')):
            return Response('本包仅使用预置虚拟身份，不连接微信',status_code=403)
        allowed=(path in ('/login','/logout','/dashboard','/health','/upload-excel','/upload-batches','/business-records') or
            path.startswith(('/__acceptance','/static/','/mall-clients','/mall-price-rules','/mall-qualifications',
                '/member-points','/mall-orders','/mall-products','/product-categories','/upload-excel/mall-template/',
                '/upload-batches/','/business-records/','/api/miniprogram/v1/','/login/phone/')))
        if not allowed:return HTMLResponse('此包只开放客户定价验收。<a href="/__acceptance">返回验收首页</a>',status_code=403)
        try:
            response=await call_next(request)
        except StarletteHTTPException as exc:
            response=JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers=exc.headers)
        if 'text/html' in response.headers.get('content-type','') and not path.startswith('/__acceptance'):
            body=b''.join([part async for part in response.body_iterator]).decode('utf-8')
            badge='<div style="padding:10px;background:#fff1cd;text-align:center;color:#614719">客户定价独立验收 · 全部虚拟资料 · <a href="/__acceptance">返回验收首页</a></div>'
            body=re.sub(r'(<body\b[^>]*>)',lambda hit:hit[0]+badge,body,count=1)
            changed=HTMLResponse(body,status_code=response.status_code)
            changed.raw_headers=[(k,v) for k,v in response.raw_headers if k.lower()!=b'content-length']+[(b'content-length',str(len(body.encode())).encode())]
            response=changed
        response.raw_headers=[(k,(cookie_prefix.encode()+v) if k.lower()==b'set-cookie' else v) for k,v in response.raw_headers]
        response.headers['Cache-Control']='no-store, private'
        # Keep same-origin form Origin observable; no-referrer can produce Origin:null
        # on browser form POSTs, which our local-origin boundary correctly rejects.
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @main.app.get('/__acceptance')
    def index(request:Request):return page(request,'portal.html')

    @main.app.get('/__acceptance/member')
    def member(request:Request):return page(request,'member.html')

    @main.app.get('/__acceptance/health')
    def health():return {'mode':MODE,'identity':settings['identity'],'source':settings['application_commit'],'sms':'disabled'}

    @main.app.get('/__acceptance/samples.zip')
    def samples():return FileResponse(root/'虚拟名单.zip',filename='虚拟名单.zip')

    @main.app.get('/__acceptance/guide')
    def guide():return Response((root/'验收说明.txt').read_text(encoding='utf-8'),media_type='text/plain')

    @main.app.get('/__acceptance/member.js')
    def script():return FileResponse(root/'harness/member.js',media_type='text/javascript')

    @main.app.post('/__acceptance/enter')
    async def enter(request:Request):
        form=await request.form();check_nonce(request,str(form.get('nonce','')))
        role=str(form.get('role','operator'));target=str(form.get('target','/mall-clients'))
        if role not in ('operator','uploader'):raise HTTPException(400,'角色无效')
        if target not in ('/mall-clients','/mall-price-rules','/mall-qualifications','/member-points','/mall-orders','/upload-excel','/upload-batches'):
            raise HTTPException(400,'入口无效')
        if role=='uploader' and target not in ('/upload-excel','/upload-batches'):raise HTTPException(400,'请使用上传入口')
        with main.SessionLocal() as db:
            user=db.query(m.User).filter_by(username='pricing-acceptance-'+('operator' if role=='operator' else 'a1-uploader')).one()
            token=main.browser_sessions.issue(user.id,user.password_hash)
        response=RedirectResponse(target,status_code=303)
        response.set_cookie(COOKIE_NAME,token,max_age=LIFETIME,httponly=True,samesite='strict')
        return response

    @main.app.post('/__acceptance/member-session')
    async def member_session(request:Request):
        check_nonce(request,request.headers.get('x-acceptance-csrf',''))
        body=await request.json();profile=body.get('profile')
        if profile not in PROFILES:raise HTTPException(400,'只能选择预置虚拟会员')
        with main.SessionLocal() as db:
            binding=db.query(m.MemberWechatBinding).filter_by(openid='uat-'+profile).one()
            token,ttl=issue_session(binding.id,binding.wechat_app_id,binding.openid)
        return JSONResponse({'token':token,'expires_in':ttl,'label':PROFILES[profile]})
    # Apply the privacy header outside FastAPI's exception handling as well:
    # dependency errors may be rendered after an HTTP middleware unwinds.
    async def private_app(scope,receive,send):
        async def private_send(message):
            if message['type']=='http.response.start':
                message=dict(message)
                message['headers']=[(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'cache-control']+[(b'cache-control',b'no-store, private')]
            await send(message)
        await main.app(scope,receive,private_send)
    return private_app


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True);parser.add_argument('--initialize',action='store_true')
    args=parser.parse_args();root,settings,main,db_path=configure(args.root)
    if args.initialize:initialize(root,settings,main,db_path)
    else:
        if not db_path.is_file():raise ValueError('Initialize a NEW package first')
        import uvicorn
        uvicorn.run(application(root,settings,main),host='127.0.0.1',port=settings['port'],access_log=False,log_level='warning')
