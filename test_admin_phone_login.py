"""Isolated synthetic admin-phone protocol, transactional security and reserved HTTP routes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import json
import os
import unittest
from unittest.mock import patch
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from app import main, models, admin_phone_login as phone, login_captcha
from app.api import admin_phone as api
from app.auth import get_password_hash
from app.browser_sessions import COOKIE_NAME
from app.database import Base

NUMBER='13800000001'
OTHER='13800000002'
IP='192.0.2.11'
NONCE='b'*43
KEY=b'virtual-phone-private-material-only-'*2


class VirtualSms:
    def __init__(self): self.deliveries=[]; self.accept=True; self.fail=False
    def enqueue(self, **values):
        self.deliveries.append(values)
        if self.fail: raise RuntimeError('virtual private provider error')
        return self.accept


class VirtualHuman:
    def __init__(self): self.calls=[]; self.result=True
    def verify(self,*args): self.calls.append(args); return self.result


class PhoneFixture(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(TemporaryDirectory())
        self.engine=create_engine('sqlite:///'+(Path(self.temp)/'virtual.db').as_posix(),connect_args={'check_same_thread':False})
        self.addCleanup(self.engine.dispose); Base.metadata.create_all(self.engine)
        self.factory=sessionmaker(bind=self.engine,autoflush=False,expire_on_commit=False)
        self.password='virtual-password-only'
        with self.factory.begin() as db:
            self.supervisor=models.User(username='virtual-supervisor',password_hash=get_password_hash(self.password),role='admin',admin_level='super_admin',is_active=True)
            self.operator=models.User(username='virtual-operator',password_hash=get_password_hash(self.password),role='admin',admin_level='operator',is_active=True)
            self.partner=models.User(username='virtual-partner',password_hash='not-a-real-password',role='partner',is_active=True)
            db.add_all([self.supervisor,self.operator,self.partner]);db.flush()
        self.now=1800000000; self.sequence=0
        self.sms=VirtualSms();self.human=VirtualHuman()
        self.service=phone.PhoneLoginService(self.factory,key=KEY,sms=self.sms,human=self.human,clock=lambda:self.now)
        self.service.bind(self.supervisor.id,self.operator.id,NUMBER,0)
        self.enterContext(patch.dict(os.environ,{'ADMIN_CAPTCHA_CONFIG':''}))
        self.network=self.enterContext(patch.object(login_captcha.requests,'post',side_effect=AssertionError('No provider network allowed')))
        self.addCleanup(self.network.assert_not_called)

    def issue(self, number=NUMBER, browser=NONCE, ip=IP, ticket=None):
        self.sequence+=1
        result=self.service.request_code(number,browser,ip,ticket or f'virtual-ticket-{self.sequence}','virtual-rand')
        return result, self.sms.deliveries[-1]['code'] if self.sms.deliveries else None

    def verify(self,result,code,**values):
        return self.service.verify_code(result['challenge_id'],code,values.get('browser',NONCE),values.get('ip',IP))

    def count_sessions(self):
        with self.factory() as db:return db.query(models.BackofficeSession).count()


class PhoneDomainTests(PhoneFixture):
    def test_success_single_use_session_and_no_plaintext_persistence(self):
        result,code=self.issue(); grant=self.verify(result,code)
        self.assertEqual(grant.user_id,self.operator.id)
        self.assertNotIn(grant.token,repr(grant))
        self.assertIsNotNone(self.service.browser_sessions.resolve(grant.token))
        with self.assertRaises(phone.PhoneRejected):self.verify(result,code)
        with self.factory() as db:
            challenge=db.get(models.AdminPhoneChallenge,result['challenge_id'])
            self.assertEqual(challenge.state,'used')
            self.assertNotEqual(challenge.code_digest,code)
            for table in (models.AdminPhoneBinding,models.AdminPhoneChallenge,models.AdminPhoneLimit):
                serialized=json.dumps([{c.name:getattr(row,c.name) for c in table.__table__.columns} for row in db.query(table)])
                self.assertNotIn(NUMBER,serialized)
                self.assertNotIn('virtual-ticket',serialized)
                self.assertNotIn(IP,serialized)
            self.assertEqual(db.query(models.AdminActionLog).filter_by(action_type='admin_phone_login').count(),1)
        self.assertEqual(self.count_sessions(),1)

    def test_wrong_codes_persist_attempts_and_five_failures_lock_challenge(self):
        result,code=self.issue(); wrong='111111' if code!='111111' else '222222'
        for attempt in range(1,6):
            with self.assertRaises(phone.PhoneRejected): self.verify(result,wrong)
            with self.factory() as db:self.assertEqual(db.get(models.AdminPhoneChallenge,result['challenge_id']).attempts,attempt)
        with self.assertRaises(phone.PhoneRejected): self.verify(result,code)
        self.assertEqual(self.count_sessions(),0)

    def test_expiry_browser_binding_resend_and_ticket_replay(self):
        result,code=self.issue(ticket='virtual-one-use-ticket')
        with self.assertRaises(phone.PhoneRejected): self.verify(result,code,browser='c'*43)
        self.now+=61
        with self.assertRaises(phone.HumanRejected):self.service.request_code(NUMBER,NONCE,IP,'virtual-one-use-ticket','changed-rand')
        self.now+=61
        new,newcode=self.issue()
        with self.assertRaises(phone.PhoneRejected):self.verify(result,code)
        self.now+=300
        with self.assertRaises(phone.PhoneRejected):self.verify(new,newcode)

    def test_unknown_unbound_and_inactive_have_same_send_response_without_sms(self):
        known,code=self.issue()
        unknown,_=self.issue(OTHER)
        self.assertEqual({k:v for k,v in known.items() if k!='challenge_id'}, {k:v for k,v in unknown.items() if k!='challenge_id'})
        self.assertEqual(len(self.sms.deliveries),1)
        with self.assertRaises(phone.PhoneRejected):self.verify(unknown,code)
        self.now+=61
        with self.factory.begin() as db:db.get(models.User,self.operator.id).is_active=False
        inactive,_=self.issue();self.assertEqual(len(self.sms.deliveries),1)
        with self.assertRaises(phone.PhoneRejected):self.verify(inactive,'123456')

    def test_cooldown_phone_hour_source_limits_and_worker_restart_share_state(self):
        self.issue()
        peer=phone.PhoneLoginService(self.factory,key=KEY,sms=self.sms,human=self.human,clock=lambda:self.now)
        with self.assertRaises(phone.PhoneRateLimited):peer.request_code(NUMBER,NONCE,IP,'virtual-peer','r')
        self.assertEqual(len(self.human.calls),1)
        for _ in range(4):self.now+=61;self.issue()
        self.now+=61
        with self.assertRaises(phone.PhoneRateLimited):self.issue()
        self.now+=3600
        for i in range(5):self.issue(f'1380000010{i}')
        with self.assertRaises(phone.PhoneRateLimited):self.issue('13800000106')

    def test_mandatory_human_gate_failures_never_dispatch(self):
        self.human.result=None
        with self.assertRaises(phone.HumanRejected):self.issue()
        self.assertEqual(self.sms.deliveries,[])
        self.now+=61
        with patch.object(self.human,'verify',side_effect=RuntimeError('private')):
            with self.assertRaises(phone.PhoneUnavailable):self.issue()
        self.assertEqual(self.sms.deliveries,[])
        with self.assertRaises(phone.HumanRejected):self.issue(ticket='trerror_bad')

    def test_dispatch_failure_never_creates_usable_code(self):
        self.sms.fail=True
        result,code=self.issue()
        self.assertEqual(result['message'],'如该号码已绑定可用管理员账号，将收到验证码。')
        with self.assertRaises(phone.PhoneRejected):self.verify(result,code)
        self.assertEqual(self.count_sessions(),0)

    def test_only_super_admin_can_bind_and_no_business_phone_auto_binding(self):
        for target in (self.partner.id,999):
            with self.assertRaises(ValueError):self.service.bind(self.supervisor.id,target,OTHER,0)
        with self.assertRaises(PermissionError):self.service.bind(self.operator.id,self.operator.id,OTHER,1)
        with self.assertRaises(ValueError):self.service.bind(self.supervisor.id,self.supervisor.id,NUMBER,0)
        with self.assertRaises(ValueError):self.service.bind(self.supervisor.id,self.operator.id,OTHER,0)
        with self.factory() as db:self.assertEqual(db.query(models.User).count(),3)

    def test_rebinding_and_unbinding_invalidate_codes_and_existing_sessions(self):
        result,code=self.issue();grant=self.verify(result,code)
        self.now+=61; pending,pendingcode=self.issue()
        self.service.bind(self.supervisor.id,self.operator.id,OTHER,1)
        self.assertIsNone(self.service.browser_sessions.resolve(grant.token))
        with self.assertRaises(phone.PhoneRejected):self.verify(pending,pendingcode)
        nextcode,otp=self.issue(OTHER)
        with self.factory.begin() as db:db.get(models.User,self.operator.id).is_active=False
        self.service.bind(self.supervisor.id,self.operator.id,None,2)
        with self.assertRaises(phone.PhoneRejected):self.verify(nextcode,otp)

    def test_account_role_password_and_disabled_changes_invalidate_issued_code(self):
        for attribute,value in (('admin_level','primary_reviewer'),('password_hash','changed-virtual'),('is_active',False),('role','partner')):
            with self.subTest(attribute=attribute):
                with self.factory.begin() as db:
                    user=db.get(models.User,self.operator.id);user.role='admin';user.admin_level='operator';user.is_active=True;user.password_hash=self.operator.password_hash
                self.now+=61;result,code=self.issue()
                with self.factory.begin() as db:setattr(db.get(models.User,self.operator.id),attribute,value)
                with self.assertRaises(phone.PhoneRejected):self.verify(result,code)

    def test_concurrent_correct_code_has_one_session_and_one_rejection(self):
        result,code=self.issue();barrier=Barrier(2)
        def run(_):
            barrier.wait(timeout=10)
            try:self.verify(result,code);return 'success'
            except phone.PhoneRejected:return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sorted(pool.map(run,range(2))),['rejected','success'])
        self.assertEqual(self.count_sessions(),1)

    def test_session_insert_failure_rolls_back_consumption_and_never_grants_cookie(self):
        result,code=self.issue()
        with patch.object(self.service.browser_sessions,'issue_in_transaction',side_effect=OperationalError('virtual',{},Exception())):
            with self.assertRaises(OperationalError):self.verify(result,code)
        with self.factory() as db:self.assertEqual(db.get(models.AdminPhoneChallenge,result['challenge_id']).state,'ready')
        self.assertEqual(self.count_sessions(),0)
        self.verify(result,code);self.assertEqual(self.count_sessions(),1)

    def test_verification_phone_and_source_quotas_persist_after_rejections(self):
        result,code=self.issue()
        for _ in range(10):
            with self.assertRaises(phone.PhoneRejected):self.verify(result,'000000',browser='z'*43)
        with self.assertRaises(phone.PhoneRateLimited):self.verify(result,code)
        self.now+=601
        for _ in range(30):
            with self.assertRaises(phone.PhoneRejected):self.verify({'challenge_id':'f'*64},'000000')
        with self.assertRaises(phone.PhoneRateLimited):self.verify({'challenge_id':'e'*64},'000000')

    def test_late_dispatch_acknowledgement_cannot_revive_replaced_challenge(self):
        original=self.sms.enqueue
        def delayed(**values):
            original(**values)
            self.now+=61
            with patch.object(self.sms,'enqueue',side_effect=original):self.issue()
            return True
        with patch.object(self.sms,'enqueue',side_effect=delayed):
            stale,_=self.issue()
        with self.assertRaises(phone.PhoneRejected):self.verify(stale,self.sms.deliveries[0]['code'])
        fresh={'challenge_id':self.sms.deliveries[-1]['request_id']}
        self.verify(fresh,self.sms.deliveries[-1]['code'])

    def test_capacity_fails_closed_without_evicting_active_state(self):
        self.service.challenge_capacity=1;self.issue();self.now+=61
        with self.assertRaises(phone.PhoneUnavailable):self.issue(OTHER)
        with self.factory() as db:self.assertEqual(db.query(models.AdminPhoneChallenge).count(),1)
        self.now+=86401
        self.service.rate_capacity=1
        with self.assertRaises(phone.PhoneUnavailable):self.issue()
        self.assertEqual(len(self.sms.deliveries),1)


class PhoneHttpTests(PhoneFixture):
    def setUp(self):
        super().setUp()
        for name,value in (('SessionLocal',self.factory),('engine',self.engine),('browser_sessions',self.service.browser_sessions)):
            self.enterContext(patch.object(main,name,value))
        self.enterContext(patch.dict(main.app.dependency_overrides,{api.get_phone_service:lambda:self.service}))
        self.client=TestClient(main.app,base_url='https://testserver',client=(IP,1234),follow_redirects=False)
        self.addCleanup(self.client.close)
        self.client.get('/login');self.csrf=self.client.cookies.get(api.CSRF_COOKIE)

    def send(self,number=NUMBER,**headers):
        self.sequence+=1
        return self.client.post('/login/phone/request-code',headers={'X-Phone-CSRF':self.csrf,**headers},
            json={'phone':number,'captcha_ticket':f'http-virtual-ticket-{self.sequence}','captcha_randstr':'r'})

    def login(self,result,code):
        return self.client.post('/login/phone/verify',headers={'X-Phone-CSRF':self.csrf},json={'challenge_id':result['challenge_id'],'code':code})

    def test_default_runtime_closed_even_if_environment_claims_mock_or_live(self):
        with patch.dict(main.app.dependency_overrides,{api.get_phone_service:lambda:None}),patch.dict(os.environ,{'ADMIN_PHONE_LOGIN_ENABLED':'true','SMS_PROVIDER':'mock'}):
            with patch.object(main,'SessionLocal') as database:
                response=self.send()
                self.assertEqual(response.status_code,503);database.assert_not_called()
            self.assertEqual(self.sms.deliveries,[])
            page=self.client.get('/login')
            self.assertIn('data-enabled="false"',page.text);self.assertIn('暂未启用',page.text)
            self.assertNotIn('TJCaptcha.js',page.text)
        self.assertIsNone(api.get_phone_service())

    def test_success_reuses_private_cookie_contract_and_rotates_existing_session(self):
        initial=self.service.browser_sessions.issue(self.operator.id,self.operator.password_hash)
        self.client.cookies.set(COOKIE_NAME,initial,domain='testserver.local',path='/')
        result=self.send().json();code=self.sms.deliveries[-1]['code'];response=self.login(result,code)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json(),{'redirect':'/dashboard'})
        self.assertEqual(response.headers['cache-control'],'no-store, private')
        cookie=response.headers['set-cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie);self.assertIn('SameSite=lax',cookie)
        self.assertIsNone(self.service.browser_sessions.resolve(initial))
        self.assertEqual(self.client.get('/dashboard').status_code,200)
        replay=self.login(result,code);self.assertEqual(replay.status_code,401);self.assertNotIn(COOKIE_NAME,replay.cookies)

    def test_forced_password_change_is_preserved(self):
        with self.factory.begin() as db:db.get(models.User,self.operator.id).must_change_password=True
        result=self.send().json();response=self.login(result,self.sms.deliveries[-1]['code'])
        self.assertEqual(response.json(),{'redirect':'/change-password'})
        self.assertEqual(self.client.get('/dashboard').headers.get('location'),'/change-password')

    def test_csrf_schema_and_client_address_are_checked_without_echoing_sensitive_values(self):
        response=self.send(**{'X-Phone-CSRF':'wrong'});self.assertEqual(response.status_code,403);self.assertEqual(self.sms.deliveries,[])
        for body in ({'phone':NUMBER,'captcha_ticket':'x','captcha_randstr':'r','user_id':self.operator.id}, {'phone':NUMBER}):
            response=self.client.post('/login/phone/request-code',headers={'X-Phone-CSRF':self.csrf},json=body)
            self.assertEqual(response.status_code,400);self.assertNotIn(NUMBER,response.text)
        self.assertEqual(self.send(**{'X-Forwarded-For':'198.51.100.99'}).status_code,200)
        self.assertEqual(self.human.calls[-1][2],IP)
        limited=self.send();self.assertEqual(limited.status_code,429);self.assertIn('Retry-After',limited.headers)

    def test_other_browser_cannot_complete_challenge_and_unknown_account_is_generic(self):
        result=self.send().json();code=self.sms.deliveries[-1]['code']
        other=TestClient(main.app,base_url='https://testserver',client=(IP,2))
        self.addCleanup(other.close)
        other.get('/login');nonce=other.cookies.get(api.CSRF_COOKIE)
        response=other.post('/login/phone/verify',headers={'X-Phone-CSRF':nonce},json={'challenge_id':result['challenge_id'],'code':code})
        self.assertEqual(response.status_code,401);self.assertNotIn(COOKIE_NAME,response.cookies)
        self.assertEqual(self.login(result,code).status_code,200)

    def test_service_outage_does_not_leak_provider_error_or_grant_session(self):
        with patch.object(self.service,'request_code',side_effect=OperationalError('sensitive statement',{},Exception('private details'))):
            response=self.send();self.assertEqual(response.status_code,503);self.assertNotIn('private details',response.text)
        self.assertNotIn(COOKIE_NAME,response.cookies)

    def test_password_login_stays_available_with_reserved_phone_panel(self):
        response=self.client.post('/login',data={'username':self.operator.username,'password':self.password})
        self.assertEqual(response.status_code,302);self.assertIn(COOKIE_NAME,response.cookies)


class PhoneConfigurationTests(unittest.TestCase):
    def test_captcha_must_be_configured_for_phone_but_optional_password_gate_unchanged(self):
        with patch.dict(os.environ,{'ADMIN_CAPTCHA_CONFIG':''}),patch.object(login_captcha.requests,'post') as network:
            login_captcha.verify('','',None)
            with self.assertRaises(phone.PhoneUnavailable):phone.ConfiguredCaptchaVerifier().verify('t','r',IP)
            network.assert_not_called()
        with self.assertRaises(phone.PhoneUnavailable):phone.DisabledSmsDispatcher().enqueue(phone=NUMBER,code='123456',request_id='virtual')

    def test_migration_matches_models_and_blocks_nonempty_downgrade(self):
        with TemporaryDirectory() as folder:
            url='sqlite:///'+(Path(folder)/'virtual.db').as_posix();config=Config('alembic.ini');config.attributes['database_url']=url
            command.upgrade(config,'0020_home_banners');command.upgrade(config,'head');command.check(config)
            engine=create_engine(url)
            try:
                self.assertTrue({'admin_phone_bindings','admin_phone_challenges','admin_phone_limits'} <= set(inspect(engine).get_table_names()))
                command.downgrade(config,'0020_home_banners');command.upgrade(config,'head')
                with engine.begin() as connection:connection.exec_driver_sql("INSERT INTO admin_phone_limits (key_digest,count,expires_at) VALUES ('virtual',1,9999999999)")
                with self.assertRaises(RuntimeError):command.downgrade(config,'0020_home_banners')
            finally:engine.dispose()
