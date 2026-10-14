"""Short-lived, signed Mini Program bearer sessions for bound members.

The token contains a binding row ID, never an openid or WeChat session_key.
Every private request must also check that the binding and member remain active.
"""

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import secrets
import time


SESSION_TTL_SECONDS = 15 * 60
_AUDIENCE = "mall-miniprogram-member-v1"
_MAX_TOKEN_LENGTH = 512


class SessionConfigurationError(Exception):
    pass


class InvalidSession(Exception):
    pass


@dataclass(frozen=True, repr=False)
class SessionClaims:
    binding_id: int = field(repr=False)
    app_id: str = field(repr=False)
    binding_tag: str = field(repr=False)
    expires_at: int = field(repr=False)


def _key() -> bytes:
    secret = os.environ.get("MINIPROGRAM_SESSION_SIGNING_KEY", "")
    if len(secret) < 32 or len(secret) > 256 or any(ord(c) < 33 for c in secret):
        raise SessionConfigurationError()
    return secret.encode("utf-8")


def assert_session_configured() -> None:
    _key()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _binding_tag(key: bytes, app_id: str, openid: str) -> str:
    return hmac.new(key, b"binding-v1:\x00" + app_id.encode() + b"\x00" + openid.encode(),
                    hashlib.sha256).hexdigest()


def issue_session(binding_id: int, app_id: str, openid: str,
                  *, now: int | None = None) -> tuple[str, int]:
    key = _key()
    if (type(binding_id) is not int or binding_id < 1
            or not isinstance(app_id, str) or not app_id
            or not isinstance(openid, str) or not openid):
        raise ValueError("invalid binding")
    timestamp = int(time.time()) if now is None else now
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError("invalid time")
    claims = {"aud": _AUDIENCE, "bid": binding_id, "app": app_id,
              "iat": timestamp, "exp": timestamp + SESSION_TTL_SECONDS,
              "jti": secrets.token_urlsafe(12),
              "tag": _binding_tag(key, app_id, openid)}
    payload = _encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signature = _encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
    token = payload + "." + signature
    if len(token) > _MAX_TOKEN_LENGTH:
        raise ValueError("invalid binding")
    return token, SESSION_TTL_SECONDS


def verify_session(token: str, *, now: int | None = None) -> SessionClaims:
    key = _key()
    if not isinstance(token, str) or not 1 <= len(token) <= _MAX_TOKEN_LENGTH:
        raise InvalidSession()
    try:
        payload, signature = token.split(".")
        if not payload or not signature or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in payload + signature):
            raise ValueError()
        expected = _encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        claims = json.loads(raw)
        timestamp = int(time.time()) if now is None else now
        if (
            type(timestamp) is not int or type(claims) is not dict
            or set(claims) != {"aud", "bid", "app", "iat", "exp", "jti", "tag"}
            or claims["aud"] != _AUDIENCE
            or type(claims["bid"]) is not int or claims["bid"] < 1
            or not isinstance(claims["app"], str) or not claims["app"]
            or type(claims["iat"]) is not int or type(claims["exp"]) is not int
            or claims["iat"] > timestamp + 30
            or claims["exp"] != claims["iat"] + SESSION_TTL_SECONDS
            or timestamp >= claims["exp"]
            or not isinstance(claims["jti"], str) or len(claims["jti"]) != 16
            or not isinstance(claims["tag"], str) or len(claims["tag"]) != 64
        ):
            raise ValueError()
    except (ValueError, UnicodeError, TypeError, KeyError, binascii.Error):
        raise InvalidSession() from None
    return SessionClaims(binding_id=claims["bid"], app_id=claims["app"],
                         binding_tag=claims["tag"], expires_at=claims["exp"])


def session_matches_binding(claims: SessionClaims, app_id: str, openid: str) -> bool:
    key = _key()
    return hmac.compare_digest(claims.binding_tag, _binding_tag(key, app_id, openid))
