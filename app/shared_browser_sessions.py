"""Database-backed browser sessions; no process-local fallback on store failure."""
import secrets
import time

from .auth_transactions import serialize_writes
from sqlalchemy.exc import SQLAlchemyError

from .browser_sessions import BrowserSession, LIFETIME, fingerprint
from .models import BackofficeSession, User


class SessionStoreUnavailable(RuntimeError):
    pass


class SessionCredentialsChanged(RuntimeError):
    pass


def token_key(token):
    if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        return None
    return fingerprint(token).hex()


class DatabaseBrowserSessions:
    def __init__(self, session_factory, capacity=4096):
        if capacity < 1:
            raise ValueError("Session capacity must be positive.")
        self.sessions = session_factory
        self.capacity = capacity

    def issue(self, user_id, password_hash):
        try:
            with self.sessions() as db:
                with db.begin():
                    return self.issue_in_transaction(db, user_id, password_hash)
        except SQLAlchemyError:
            raise SessionStoreUnavailable("Browser session store unavailable.") from None

    def issue_in_transaction(self, db, user_id, password_hash, *, accept_user=None):
        """Consume a login proof and issue its session in the SAME caller-owned transaction.

        The optional predicate sees the fresh locked account; no session is minted if
        the account's role/binding eligibility changed while proof was being checked.
        """
        serialize_writes(db, 73190512)
        user = db.query(User).filter(User.id == user_id).with_for_update().populate_existing().first()
        if (user is None or not user.is_active or not user.password_hash or user.password_hash != password_hash
                or (accept_user is not None and not accept_user(user))):
            raise SessionCredentialsChanged("Login credentials changed.")
        token = secrets.token_hex(32)
        now = int(time.time())
        db.query(BackofficeSession).filter(BackofficeSession.expires_at <= now).delete(synchronize_session=False)
        excess = db.query(BackofficeSession).count() - self.capacity + 1
        if excess > 0:
            oldest = [row[0] for row in db.query(BackofficeSession.token_hash)
                      .order_by(BackofficeSession.expires_at, BackofficeSession.token_hash).limit(excess)]
            db.query(BackofficeSession).filter(BackofficeSession.token_hash.in_(oldest)).delete(synchronize_session=False)
        db.add(BackofficeSession(token_hash=token_key(token), user_id=user_id,
            credential_hash=fingerprint(password_hash).hex(), expires_at=now+LIFETIME))
        db.flush()
        return token

    def resolve(self, token):
        key = token_key(token)
        if key is None:
            return None
        try:
            with self.sessions() as db:
                row = db.get(BackofficeSession, key)
                if row is None or row.expires_at <= time.time():
                    return None
                return BrowserSession(row.user_id, bytes.fromhex(row.credential_hash), row.expires_at)
        except (SQLAlchemyError, ValueError):
            raise SessionStoreUnavailable("Browser session store unavailable.") from None

    def revoke(self, token):
        key = token_key(token)
        if key is None:
            return
        try:
            with self.sessions() as db:
                with db.begin():
                    db.query(BackofficeSession).filter_by(token_hash=key).delete(synchronize_session=False)
        except SQLAlchemyError:
            raise SessionStoreUnavailable("Browser session store unavailable.") from None

    def revoke_user(self, user_id, *, db=None):
        try:
            if db is not None:
                # Account mutation and revocation must commit or roll back together.
                db.flush()
                db.query(BackofficeSession).filter_by(user_id=user_id).delete(synchronize_session=False)
            else:
                with self.sessions() as owned:
                    with owned.begin():
                        owned.query(BackofficeSession).filter_by(user_id=user_id).delete(synchronize_session=False)
        except SQLAlchemyError:
            raise SessionStoreUnavailable("Browser session store unavailable.") from None
