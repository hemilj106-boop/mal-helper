import hashlib
import re
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

import hanime_app as h


SUPPORTED_LOGIN_HOSTS = {"hanime1.me", "hanimeone.me"}


def _hashed_headers(version: str = "app2") -> dict:
    claim = str(int(time.time()))
    signature = hashlib.sha256(f"9944822{claim}8{claim}113".encode()).hexdigest()
    return {
        "X-Signature-Version": version,
        "X-Claim": claim,
        "X-Signature": signature,
        "User-Agent": "okhttp/3.12.1",
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json",
    }


def _web2_headers() -> dict:
    now = str(int(time.time()))
    return {
        "X-Signature-Version": "web2",
        "X-Time": now,
        "X-Signature": secrets.token_hex(32),
        "User-Agent": h.UA,
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json",
    }


async def robust_rapi_login(email: str, password: str) -> dict:
    attempts = [
        (f"{h.HANIME_BASE_URL}/rapi/v4/sessions", _web2_headers(), "hanime-web2"),
        (f"{h.HANIME_RAPI_BASE}/rapi/v4/sessions", _hashed_headers("app2"), "cdn-app2"),
        (f"{h.HANIME_RAPI_BASE}/rapi/v4/sessions", _hashed_headers("web2"), "cdn-web2"),
    ]

    statuses = []
    unauthorized = False
    for url, headers, mode in attempts:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.post(
                    url,
                    json={"burger": email, "fries": password},
                    headers=headers,
                )
            statuses.append(f"{mode}:{response.status_code}")
            try:
                payload = response.json()
            except Exception:
                payload = {}

            token = payload.get("session_token") if isinstance(payload, dict) else None
            if token:
                user = payload.get("user") or {}
                user_id = str(user.get("id")) if user.get("id") is not None else None
                user_name = user.get("name") or user.get("username")
                h.save_session(
                    session_token=token,
                    user_id=user_id,
                    user_name=user_name,
                    auth_mode=mode,
                )
                return {"user_id": user_id, "user_name": user_name, "auth_mode": mode}

            if response.status_code in (400, 401):
                unauthorized = True
            if isinstance(payload, dict) and "Unauthorized" in str(payload.get("errors", "")):
                unauthorized = True
        except Exception as exc:
            statuses.append(f"{mode}:error-{type(exc).__name__}")

    if unauthorized:
        raise HTTPException(
            401,
            "Hanime's login API was reachable, but it did not accept the account credentials. "
            "Use the Android WebView bridge instead of entering your Hanime password on Render.",
        )

    raise HTTPException(
        502,
        "Hanime blocked or rejected all cloud login methods. "
        f"Attempt results: {', '.join(statuses)}",
    )


class BridgeSessionIn(BaseModel):
    base_url: str = Field(description="Hanime login origin, for example https://hanime1.me")
    cookie: str = Field(min_length=1, max_length=16384)
    user_agent: Optional[str] = Field(default=None, max_length=1024)


def _cookie_looks_authenticated(cookie_header: str) -> bool:
    names = []
    for piece in cookie_header.split(";"):
        name = piece.partition("=")[0].strip().lower()
        if name:
            names.append(name)
    return any("session" in name for name in names) or "laravel_session" in names


# The route functions in hanime_app resolve this global at request time, so
# replacing it here upgrades /connect without duplicating any existing routes.
h.rapi_login = robust_rapi_login
app = h.app


@app.post("/bridge/session")
def bridge_session(
    body: BridgeSessionIn,
    x_api_key: Optional[str] = Header(default=None),
):
    h.check_api_key(x_api_key)

    parsed = urlparse(body.base_url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in SUPPORTED_LOGIN_HOSTS:
        raise HTTPException(400, "Unsupported Hanime login host")

    cookie_header = body.cookie.strip()
    if not _cookie_looks_authenticated(cookie_header):
        raise HTTPException(400, "No Hanime login session cookie was found. Finish logging in first.")

    cookies = httpx.Cookies()
    count = 0
    for piece in cookie_header.split(";"):
        name, sep, value = piece.strip().partition("=")
        if not sep or not name:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_\-\.]+", name):
            continue
        cookies.set(name, value, domain=host, path="/")
        count += 1

    if count == 0:
        raise HTTPException(400, "Could not parse the Hanime session cookies")

    h.save_session(
        cookies=cookies,
        user_id=None,
        user_name=None,
        auth_mode=f"webview-cookie:{host}",
    )

    return {
        "ok": True,
        "connected": True,
        "host": host,
        "cookies_received": count,
        "password_received": False,
    }
