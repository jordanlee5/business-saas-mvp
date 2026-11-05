"""Browser cookie contract and a process-local store retained for isolated tests."""
import hashlib
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

COOKIE_NAME = "backoffice_session"
LIFETIME = 8 * 60 * 60


def fingerprint(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass(frozen=True)
class BrowserSession:
    user_id: int
    credential: bytes
    expires_at: float


class BrowserSessions:
    def __init__(self, capacity: int = 4096):
        self._capacity = capacity
        self._sessions: OrderedDict[bytes, BrowserSession] = OrderedDict()
        self._lock = Lock()

    def issue(self, user_id: int, password_hash: str) -> str:
        token = secrets.token_hex(32)
        with self._lock:
            if len(self._sessions) >= self._capacity:
                self._sessions.popitem(last=False)
            self._sessions[fingerprint(token)] = BrowserSession(
                user_id, fingerprint(password_hash), time.monotonic() + LIFETIME
            )
        return token

    def resolve(self, token: str | None) -> BrowserSession | None:
        if not token or len(token) != 64:
            return None
        key = fingerprint(token)
        with self._lock:
            session = self._sessions.get(key)
            if session and session.expires_at <= time.monotonic():
                del self._sessions[key]
                return None
            return session

    def revoke(self, token: str | None) -> None:
        if token and len(token) == 64:
            with self._lock:
                self._sessions.pop(fingerprint(token), None)

    def revoke_user(self, user_id: int, *, db=None) -> None:
        with self._lock:
            for key in [key for key, item in self._sessions.items() if item.user_id == user_id]:
                del self._sessions[key]
