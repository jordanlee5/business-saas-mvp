"""Optional Tencent Captcha gate. Secrets stay in an external deployment file."""
import hashlib
import hmac
import ipaddress
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


class CaptchaUnavailable(Exception):
    pass


class CaptchaRejected(Exception):
    pass


def settings():
    path = os.environ.get("ADMIN_CAPTCHA_CONFIG", "").strip()
    if not path:
        return None
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        required = ("app_id", "app_secret_key", "secret_id", "secret_key")
        if not isinstance(config, dict) or any(
            not isinstance(config.get(key), str) or not config[key].strip()
            for key in required
        ):
            raise ValueError()
        if not config["app_id"].isascii() or not config["app_id"].isdigit():
            raise ValueError()
        if not 0 < int(config["app_id"]) < 2 ** 63:
            raise ValueError()
        return config
    except (OSError, ValueError, TypeError):
        raise CaptchaUnavailable() from None


def public_context():
    try:
        config = settings()
        return {"captcha_required": config is not None,
                "captcha_app_id": config["app_id"] if config else "",
                "captcha_unavailable": False}
    except CaptchaUnavailable:
        return {"captcha_required": True, "captcha_app_id": "",
                "captcha_unavailable": True}


def signed_headers(payload, config, timestamp):
    """Tencent Cloud API 3.0 TC3 signing; never log headers or request body."""
    host = "captcha.tencentcloudapi.com"
    content_type = "application/json; charset=utf-8"
    signed = "content-type;host;x-tc-action"
    canonical_headers = (f"content-type:{content_type}\nhost:{host}\n"
                         "x-tc-action:describecaptcharesult\n")
    sha = lambda value: hashlib.sha256(value).hexdigest()
    canonical = f"POST\n/\n\n{canonical_headers}\n{signed}\n{sha(payload)}"
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    scope = f"{date}/captcha/tc3_request"
    to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n{sha(canonical.encode())}"
    digest = lambda key, text: hmac.new(key, text.encode(), hashlib.sha256).digest()
    key = digest(("TC3" + config["secret_key"]).encode(), date)
    key = digest(digest(key, "captcha"), "tc3_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": content_type, "Host": host,
            "X-TC-Action": "DescribeCaptchaResult", "X-TC-Version": "2019-07-22",
            "X-TC-Timestamp": str(timestamp),
            "Authorization": f"TC3-HMAC-SHA256 Credential={config['secret_id']}/{scope}, "
                             f"SignedHeaders={signed}, Signature={signature}"}


def verify(ticket, randstr, client_ip):
    config = settings()
    if config is None:
        return
    if (not ticket or not randstr or len(ticket) > 4096 or len(randstr) > 256
            or ticket.startswith("trerror")):
        raise CaptchaRejected()
    try:
        ipaddress.ip_address(client_ip)
    except (ValueError, TypeError):
        raise CaptchaUnavailable() from None
    payload = json.dumps({"CaptchaType": 9, "CaptchaAppId": int(config["app_id"]),
                          "AppSecretKey": config["app_secret_key"], "Ticket": ticket,
                          "Randstr": randstr, "UserIp": client_ip},
                         separators=(",", ":")).encode()
    try:
        # Never retry a one-use ticket; ask the browser for a fresh challenge.
        response = requests.post("https://captcha.tencentcloudapi.com/", data=payload,
                                 headers=signed_headers(payload, config, int(time.time())),
                                 timeout=(2, 5), allow_redirects=False)
        response.raise_for_status()
        result = response.json()["Response"]
        if not isinstance(result, dict) or result.get("Error") or "CaptchaCode" not in result:
            raise ValueError()
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise CaptchaUnavailable() from None
    # API transport success is NOT proof of verification. Only code 1 is accepted.
    if type(result["CaptchaCode"]) is not int or result["CaptchaCode"] != 1:
        raise CaptchaRejected()
    if result.get("EvilLevel") not in (None, 0):
        raise CaptchaRejected()
