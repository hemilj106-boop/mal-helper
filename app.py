import hashlib
import secrets
import time

import httpx
from fastapi import HTTPException

import hanime_app as h


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
            "Make sure you are using the email address and password that work on Hanime.tv (not the profile/display name).",
        )

    raise HTTPException(
        502,
        "Hanime blocked or rejected all cloud login methods. "
        f"Attempt results: {', '.join(statuses)}",
    )


# The route functions in hanime_app resolve this global at request time, so
# replacing it here upgrades /connect without duplicating any routes.
h.rapi_login = robust_rapi_login
app = h.app
