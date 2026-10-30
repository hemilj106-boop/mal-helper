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


# Only accept sessions from the actual hanime.tv site.
SUPPORTED_LOGIN_HOSTS = {"hanime.tv", "www.hanime.tv"}


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
    # Hanime's current app API uses /rapi/v7/sessions and an
    # {email, password} payload. The older v4 / {burger, fries} flow
    # is retired and should not be used.
    url = f"{h.HANIME_RAPI_BASE}/rapi/v7/sessions"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(
                url,
                json={"email": email, "password": password},
                headers=_hashed_headers("app2"),
            )
    except Exception as exc:
        raise HTTPException(
            502,
            f"Could not reach Hanime's current login API ({type(exc).__name__}).",
        )

    try:
        payload = response.json()
    except Exception:
        payload = {}

    token = payload.get("session_token") if isinstance(payload, dict) else None
    if response.status_code >= 400 or not token:
        if response.status_code in (400, 401):
            raise HTTPException(
                401,
                "Hanime rejected the login request. Check the account credentials and try again.",
            )
        raise HTTPException(
            502,
            f"Hanime's current login API did not return a session (HTTP {response.status_code}). "
            "The hosting network may be blocked, or Hanime may have changed its API.",
        )

    user = payload.get("user") or {}
    user_id = str(user.get("id")) if user.get("id") is not None else None
    user_name = user.get("name") or user.get("username")
    h.save_session(
        session_token=token,
        user_id=user_id,
        user_name=user_name,
        auth_mode="rapi-v7-app2",
    )
    return {"user_id": user_id, "user_name": user_name, "auth_mode": "rapi-v7-app2"}


class BridgeSessionIn(BaseModel):
    base_url: str = Field(description="Hanime login origin, for example https://hanime.tv")
    cookie: str = Field(min_length=1, max_length=16384)
    user_agent: Optional[str] = Field(default=None, max_length=1024)


def _cookie_has_session(cookie_header: str) -> bool:
    names = []
    for piece in cookie_header.split(";"):
        name = piece.partition("=")[0].strip().lower()
        if name:
            names.append(name)
    return any("session" in name for name in names) or "laravel_session" in names


def _parse_cookie_header(cookie_header: str, host: str) -> tuple[httpx.Cookies, int]:
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
    return cookies, count


async def _verify_webview_session(
    base_url: str,
    cookies: httpx.Cookies,
    user_agent: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    headers = h.web_headers(base_url + "/")
    if user_agent:
        headers["User-Agent"] = user_agent

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=headers,
            cookies=cookies,
        ) as client:
            response = await client.get(base_url + "/account?tab=profile")
    except Exception as exc:
        raise HTTPException(502, f"Could not verify the Hanime session ({type(exc).__name__}).")

    final_url = str(response.url)
    body = response.text
    body_lower = body.lower()

    if response.status_code >= 400:
        raise HTTPException(401, f"Hanime did not accept the browser session (HTTP {response.status_code}).")

    if "/login" in final_url.lower():
        raise HTTPException(401, "You are not logged in inside Hanime Bridge yet. Finish signing in, then try again.")

    authenticated_hint = any(
        hint in body_lower
        for hint in ("profile details", "display name", "/logout", "account created", "channel views")
    ) or "/account" in final_url.lower()

    if not authenticated_hint:
        raise HTTPException(401, "No authenticated Hanime account was detected. Finish signing in inside the Bridge first.")

    user_id = h.extract_user_id(body)
    user_name = None
    name_match = re.search(
        r'(?:display\s*name|name)[\s\S]{0,300}?value=["\']([^"\']{1,100})["\']',
        body,
        re.I,
    )
    if name_match:
        user_name = h.clean_text(name_match.group(1)) or None

    return user_id, user_name


h.rapi_login = robust_rapi_login
app = h.app


@app.post("/bridge/session")
async def bridge_session(
    body: BridgeSessionIn,
    x_api_key: Optional[str] = Header(default=None),
):
    h.check_api_key(x_api_key)

    parsed = urlparse(body.base_url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in SUPPORTED_LOGIN_HOSTS:
        raise HTTPException(400, "Unsupported Hanime login host")

    cookie_header = body.cookie.strip()
    if not _cookie_has_session(cookie_header):
        raise HTTPException(400, "No Hanime session cookie was found. Open the Hanime login page first.")

    cookies, count = _parse_cookie_header(cookie_header, host)
    if count == 0:
        raise HTTPException(400, "Could not parse the Hanime session cookies")

    base_url = f"https://{host}"
    user_id, user_name = await _verify_webview_session(base_url, cookies, body.user_agent)

    h.save_session(
        cookies=cookies,
        user_id=user_id,
        user_name=user_name,
        auth_mode=f"webview-cookie:{host}",
    )

    return {
        "ok": True,
        "connected": True,
        "verified": True,
        "host": host,
        "user_id": user_id,
        "user_name": user_name,
        "cookies_received": count,
        "password_received": False,
    }
