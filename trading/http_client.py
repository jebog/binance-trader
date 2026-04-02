from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from config import BINANCE_API_KEY as API_KEY
from config import BINANCE_SECRET_KEY as SECRET_KEY

BASE_URL = "https://api.binance.com"


# ── HTTP helpers ─────────────────────────────────────────────────────────────
def get(path: str, params: Optional[dict[str, Any]] = None, _retries: int = 1) -> Any:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "binance-spot/1.1.0 (Scanner)",
        "X-MBX-APIKEY": API_KEY,
    })
    for attempt in range(_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < _retries:
                time.sleep(1)
                continue
            raise


def signed_get(path: str, params: dict[str, Any]) -> Any:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return get(path, params)


def signed_post(path: str, params: dict[str, Any]) -> Any:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        BASE_URL + path, data=data, method="POST",
        headers={
            "User-Agent": "binance-spot/1.1.0 (Scanner)",
            "X-MBX-APIKEY": API_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise Exception(f"HTTP {e.code} \u2014 {body}") from None


def signed_delete(path: str, params: dict[str, Any]) -> Any:
    """Authenticated DELETE request \u2014 Binance DELETE endpoints read params from the query string."""
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    full_url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        full_url, data=None, method="DELETE",
        headers={
            "User-Agent": "binance-spot/1.1.0 (Scanner)",
            "X-MBX-APIKEY": API_KEY,
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise Exception(f"HTTP {e.code} \u2014 {body}") from None
