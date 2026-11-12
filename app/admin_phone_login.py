"""Reserved phone login domain. No production SMS provider or configuration switch.

The application factory returns no service. Only isolated tests inject an explicit
sender, mandatory human verifier, private HMAC key and isolated session factory.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from typing import Protocol

from . import login_captcha
from .admin_permissions import VALID_ADMIN_LEVELS, SUPER_ADMIN
from .auth_transactions import serialize_writes
from .models import AdminPhoneBinding as Binding, AdminPhoneChallenge as Challenge, AdminPhoneLimit as Limit
from .models import User, AdminActionLog
from .shared_browser_sessions import DatabaseBrowserSessions, SessionCredentialsChanged


class PhoneUnavailable(Exception):
    pass


class PhoneRejected(Exception):
    pass


class HumanRejected(Exception):
    pass


class PhoneRateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, min(3600, int(retry_after)))


class SmsDispatcher(Protocol):
    def enqueue(self, *, phone: str, code: str, request_id: str) -> bool:
        """Accept exactly one delivery job, deduplicated by request_id; never log secrets."""
        ...


class HumanVerifier(Protocol):
    def verify(self, ticket: str, randstr: str, client_ip: str) -> bool:
        """Return literal True only after server-side verification, never fail open."""
        ...


class DisabledSmsDispatcher:
    def enqueue(self, **kwargs):
        raise PhoneUnavailable()


class ConfiguredCaptchaVerifier:
    def verify(self, ticket, randstr, client_ip):
        try:
            login_captcha.verify(ticket, randstr, client_ip, required=True)
        except login_captcha.CaptchaRejected:
            raise HumanRejected() from None
        except login_captcha.CaptchaUnavailable:
            raise PhoneUnavailable() from None
        return True


def public_context():
    # There is deliberately no environment-variable "mock" or live activation path.
    return {'phone_login_enabled': False, 'phone_captcha_app_id': ''}


def normalize_phone(value):
    value = value.strip() if isinstance(value, str) else ''
    if not re.fullmatch(r'1[3-9][0-9]{9}', value):
        raise PhoneRejected()
    return value


def eligible(user):
    return bool(user and user.is_active and user.password_hash and user.role == 'admin'
                and user.admin_level in VALID_ADMIN_LEVELS)


@dataclass(frozen=True)
class LoginGrant:
    token: str = field(repr=False)
    user_id: int
    must_change_password: bool


class PhoneLoginService:
    def __init__(self, session_factory, *, key: bytes, sms: SmsDispatcher, human: HumanVerifier,
                 clock=time.time, challenge_capacity=10000, rate_capacity=4096):
        if not isinstance(key, bytes) or len(key) < 32 or min(challenge_capacity, rate_capacity) < 1:
            raise ValueError('Invalid private authentication configuration.')
        self.sessions, self.key, self.sms, self.human, self.clock = session_factory, key, sms, human, clock
        self.challenge_capacity, self.rate_capacity = challenge_capacity, rate_capacity
        self.browser_sessions = DatabaseBrowserSessions(session_factory)

    def digest(self, purpose, value):
        return hmac.new(self.key, (purpose + ':' + value).encode('utf-8'), hashlib.sha256).hexdigest()

    def stamp(self, user):
        return self.digest('account', f'{user.id}:{user.password_hash}:{user.role}:{user.admin_level}:{user.must_change_password}')

    @contextmanager
    def transaction(self):
        with self.sessions() as db:
            with db.begin():
                serialize_writes(db, 73190513)
                yield db

    def cleanup(self, db, now):
        db.query(Limit).filter(Limit.expires_at <= now).delete(synchronize_session=False)
        # Retain replay receipts for a day; never evict active limits or challenges to make room.
        db.query(Challenge).filter(Challenge.created_at <= now-86400).delete(synchronize_session=False)

    def charge(self, db, purpose, identity, maximum, period, now):
        key = self.digest('limit-'+purpose, identity)
        row = db.get(Limit, key)
        if row:
            if row.count >= maximum:
                raise PhoneRateLimited(row.expires_at-now)
            row.count += 1
        else:
            if db.query(Limit).count() >= self.rate_capacity:
                raise PhoneUnavailable()
            db.add(Limit(key_digest=key, count=1, expires_at=now+period))
        db.flush()

    def bind(self, actor_id, user_id, phone, expected_revision):
        """Internal provisioning only: super-admin verifies ownership out of band first.

        None unbinds. No public binding endpoint, no business-table phone inference.
        Changing the binding invalidates outstanding codes AND existing browser sessions.
        """
        phone_key = self.digest('phone', normalize_phone(phone)) if phone is not None else None
        with self.transaction() as db:
            users = {u.id:u for u in db.query(User).filter(User.id.in_([actor_id,user_id])).order_by(User.id).with_for_update()}
            actor, user = users.get(actor_id), users.get(user_id)
            if not eligible(actor) or actor.admin_level != SUPER_ADMIN or actor.must_change_password:
                raise PermissionError('无手机号绑定管理权限')
            if user is None or (phone_key is not None and not eligible(user)):
                raise ValueError('只能绑定已启用的管理员账号')
            row = db.get(Binding, user_id)
            if type(expected_revision) is not int or expected_revision != (row.revision if row else 0):
                raise ValueError('绑定状态已更新，请重新核对')
            if phone_key and db.query(Binding).filter(Binding.phone_digest==phone_key, Binding.user_id!=user_id).first():
                raise ValueError('该号码不能用于此绑定')
            revision = expected_revision+1
            if row:
                row.phone_digest, row.revision = phone_key, revision
                row.updated_by_id, row.updated_at = actor_id, int(self.clock())
            else:
                db.add(Binding(user_id=user_id, phone_digest=phone_key, revision=revision,
                               updated_by_id=actor_id, updated_at=int(self.clock())))
            db.query(Challenge).filter(Challenge.user_id==user_id, Challenge.state.in_(['pending','ready'])).update({'state':'failed'})
            self.browser_sessions.revoke_user(user_id, db=db)
            db.add(AdminActionLog(admin_id=actor_id, action_type='admin_phone_binding', target_type='user', target_id=user_id,
                description=f'手机号登录绑定已{"更新" if phone_key else "解除"}；版本 {revision}'))
        return revision

    def request_code(self, phone, browser, client_ip, ticket, randstr):
        phone = normalize_phone(phone)
        try:
            client_ip = str(ipaddress.ip_address(client_ip))
        except ValueError:
            raise PhoneUnavailable() from None
        if not isinstance(browser,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',browser):
            raise PhoneRejected()
        if (not isinstance(ticket,str) or not 1 <= len(ticket) <= 4096 or
                not isinstance(randstr,str) or not 1 <= len(randstr) <= 256 or ticket.startswith('trerror')):
            raise HumanRejected()
        now = int(self.clock()); phone_key = self.digest('phone',phone)
        # Charge before any external verification/enqueue. Fixed windows are shared across workers.
        with self.transaction() as db:
            self.cleanup(db,now)
            for purpose,identity,maximum,period in (
                ('send-global','all',200,3600), ('send-ip-minute',client_ip,5,60),
                ('send-ip-hour',client_ip,20,3600), ('send-phone-minute',phone_key,1,60),
                ('send-phone-hour',phone_key,5,3600)):
                self.charge(db,purpose,identity,maximum,period,now)
        try:
            verified = self.human.verify(ticket,randstr,client_ip)
        except (HumanRejected, PhoneUnavailable):
            raise
        except Exception:
            raise PhoneUnavailable() from None
        if verified is not True:
            raise HumanRejected()
        challenge_id = secrets.token_hex(32)
        code = f'{secrets.randbelow(1000000):06d}'
        captcha_key = self.digest('captcha',ticket)
        with self.transaction() as db:
            self.cleanup(db,now)
            if db.query(Challenge).filter_by(captcha_digest=captcha_key).first():
                raise HumanRejected()
            if db.query(Challenge).count() >= self.challenge_capacity:
                raise PhoneUnavailable()
            binding = db.query(Binding).filter_by(phone_digest=phone_key).first()
            user = db.get(User,binding.user_id) if binding else None
            deliver = eligible(user)
            # Resending invalidates previous pending/ready codes; delayed send acknowledgements cannot revive them.
            db.query(Challenge).filter(Challenge.phone_digest==phone_key,Challenge.state.in_(['pending','ready'])).update({'state':'failed'})
            db.add(Challenge(challenge_id=challenge_id, phone_digest=phone_key, browser_digest=self.digest('browser',browser),
                captcha_digest=captcha_key, code_digest=self.digest('otp',challenge_id+':'+code),
                user_id=user.id if deliver else None, binding_revision=binding.revision if deliver else None,
                credential_digest=self.stamp(user) if deliver else None, attempts=0, state='pending',created_at=now,expires_at=now+300))
        accepted = not deliver  # Unknown accounts receive an indistinguishable decoy challenge, never an SMS.
        if deliver:
            try:
                accepted = self.sms.enqueue(phone=phone,code=code,request_id=challenge_id) is True
            except Exception:
                accepted = False
        with self.transaction() as db:
            db.query(Challenge).filter_by(challenge_id=challenge_id,state='pending').update({'state':'ready' if accepted else 'failed'})
        return {'challenge_id':challenge_id,'expires_in':300,'retry_after':60,
                'message':'如该号码已绑定可用管理员账号，将收到验证码。'}

    def verify_code(self, challenge_id, code, browser, client_ip, *, previous_session=None):
        try:
            client_ip = str(ipaddress.ip_address(client_ip))
        except ValueError:
            raise PhoneUnavailable() from None
        now = int(self.clock()); grant = None
        with self.transaction() as db:
            self.cleanup(db,now)
            self.charge(db,'verify-ip',client_ip,30,600,now)
            row = db.get(Challenge,challenge_id) if isinstance(challenge_id,str) and re.fullmatch(r'[0-9a-f]{64}',challenge_id) else None
            if row:
                self.charge(db,'verify-phone',row.phone_digest,10,600,now)
            if (row and row.state=='ready' and row.expires_at>now and row.attempts<5 and
                    isinstance(browser,str) and hmac.compare_digest(row.browser_digest,self.digest('browser',browser))):
                row.attempts += 1
                valid = isinstance(code,str) and re.fullmatch(r'[0-9]{6}',code) and hmac.compare_digest(row.code_digest,self.digest('otp',challenge_id+':'+code))
                binding = db.get(Binding,row.user_id) if row.user_id else None
                user = db.get(User,row.user_id) if row.user_id else None
                if (valid and binding and binding.phone_digest==row.phone_digest and binding.revision==row.binding_revision
                        and eligible(user) and hmac.compare_digest(row.credential_digest or '',self.stamp(user))):
                    try:
                        token = self.browser_sessions.issue_in_transaction(db,user.id,user.password_hash,
                            accept_user=lambda current: eligible(current) and hmac.compare_digest(row.credential_digest or '',self.stamp(current)))
                    except SessionCredentialsChanged:
                        row.state = 'failed'
                    else:
                        from .shared_browser_sessions import token_key
                        from .models import BackofficeSession
                        old = token_key(previous_session)
                        if old: db.query(BackofficeSession).filter_by(token_hash=old).delete(synchronize_session=False)
                        row.state = 'used'
                        grant = LoginGrant(token,user.id,bool(user.must_change_password))
                        db.add(AdminActionLog(admin_id=user.id,action_type='admin_phone_login',target_type='user',target_id=user.id,
                                              description='手机号验证码核验成功'))
                elif valid or row.attempts>=5:
                    row.state = 'failed'
        # Rejections happen AFTER commit so attempts/limits are never rolled back by an auth failure.
        if grant is None:
            raise PhoneRejected()
        return grant
