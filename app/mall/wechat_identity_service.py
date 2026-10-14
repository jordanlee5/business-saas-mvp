"""Server-only exchange of a WeChat Mini Program login code.

This module never exposes the WeChat session key to the Mini Program. The
caller is responsible for issuing its own short-lived application session.
"""

from dataclasses import dataclass, field
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


_ENDPOINT = "https://api.weixin.qq.com/sns/jscode2session"
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_+/-]{1,512}$")
_MAX_RESPONSE_BYTES = 8192
_TIMEOUT_SECONDS = 5


class WechatConfigurationError(Exception):
    """The server has no usable WeChat credentials."""


class WechatCodeError(Exception):
    """The login code was absent or rejected by WeChat."""


class WechatExchangeUnavailable(Exception):
    """The provider could not return a trustworthy response."""


@dataclass(frozen=True, repr=False)
class WechatIdentity:
    app_id: str = field(repr=False)
    openid: str = field(repr=False)
    unionid: str | None = field(default=None, repr=False)


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _fetch_response(url: str) -> bytes:
    # Never follow a redirect containing the app secret and one-time code.
    opener = build_opener(_RejectRedirect())
    with opener.open(Request(url, method="GET"), timeout=_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise WechatExchangeUnavailable()
        return response.read(_MAX_RESPONSE_BYTES + 1)


def exchange_wechat_code(code: str, *, fetch=None) -> WechatIdentity:
    """Exchange an untrusted wx.login code without logging provider secrets.

    ``fetch`` is an injectable transport for offline tests. Production always
    calls the fixed HTTPS WeChat endpoint with a bounded timeout and response.
    """
    if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
        raise WechatCodeError()

    app_id = os.environ.get("WECHAT_MINIPROGRAM_APP_ID", "").strip()
    app_secret = os.environ.get("WECHAT_MINIPROGRAM_APP_SECRET", "").strip()
    if (
        not re.fullmatch(r"wx[0-9a-fA-F]{16}", app_id)
        or not app_secret
        or len(app_secret) > 256
        or any(ord(character) < 33 for character in app_secret)
    ):
        raise WechatConfigurationError()

    url = _ENDPOINT + "?" + urlencode({
        "appid": app_id,
        "secret": app_secret,
        "js_code": code,
        "grant_type": "authorization_code",
    })
    try:
        payload = (fetch or _fetch_response)(url)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise WechatExchangeUnavailable() from None
    # A custom transport must meet the same size and type restrictions.
    if not isinstance(payload, bytes) or len(payload) > _MAX_RESPONSE_BYTES:
        raise WechatExchangeUnavailable()
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WechatExchangeUnavailable() from None
    if not isinstance(result, dict):
        raise WechatExchangeUnavailable()
    if "errcode" in result:
        if type(result["errcode"]) is not int:
            raise WechatExchangeUnavailable()
        if result["errcode"] != 0:
            raise WechatCodeError()

    openid = result.get("openid")
    session_key = result.get("session_key")
    unionid = result.get("unionid")
    if (
        not isinstance(openid, str) or not 1 <= len(openid) <= 128
        or not isinstance(session_key, str) or not 1 <= len(session_key) <= 256
        or any(ord(character) < 33 for character in openid + session_key)
        or (unionid is not None and (
            not isinstance(unionid, str) or not 1 <= len(unionid) <= 128
            or any(ord(character) < 33 for character in unionid)
        ))
    ):
        raise WechatExchangeUnavailable()
    # session_key deliberately goes out of scope here; never persist/return it.
    return WechatIdentity(app_id=app_id, openid=openid, unionid=unionid)
