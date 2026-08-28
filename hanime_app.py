import hashlib
import html
import json
import os
import re
import secrets
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import psycopg
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row

APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
HELPER_API_KEY = os.getenv("HELPER_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
HANIME_BASE_URL = os.getenv("HANIME_BASE_URL", "https://hanime.tv").strip().rstrip("/")
HANIME_SEARCH_URL = os.getenv("HANIME_SEARCH_URL", "https://search.htv-services.com/").strip()
HANIME_RAPI_BASE = os.getenv("HANIME_RAPI_BASE", "https://www.universal-cdn.com").strip().rstrip("/")

UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
)

app = FastAPI(
    title="Hanime Helper",
    version="0.2.0",
    description="Private helper for searching Hanime.tv and managing the owner's authenticated account session.",
)


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hanime_session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cookies_json TEXT,
                session_token TEXT,
                user_id TEXT,
                user_name TEXT,
                auth_mode TEXT,
                updated_at BIGINT NOT NULL
            )
            """
        )
        # Upgrade the first prototype table in-place if it already exists.
        conn.execute("ALTER TABLE hanime_session ALTER COLUMN cookies_json DROP NOT NULL")
        conn.execute("ALTER TABLE hanime_session ADD COLUMN IF NOT EXISTS session_token TEXT")
        conn.execute("ALTER TABLE hanime_session ADD COLUMN IF NOT EXISTS user_name TEXT")
        conn.execute("ALTER TABLE hanime_session ADD COLUMN IF NOT EXISTS auth_mode TEXT")
        conn.commit()


@app.on_event("startup")
def startup():
    init_db()


def check_api_key(x_api_key: Optional[str]):
    if not HELPER_API_KEY:
        raise HTTPException(500, "HELPER_API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, HELPER_API_KEY):
        raise HTTPException(401, "Invalid API key")


def web_headers(referer: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Signature-Version": "web2",
        "X-Signature": secrets.token_hex(32),
    }
    if referer:
        headers["Referer"] = referer
    return headers


def rapi_headers() -> dict:
    claim = str(int(time.time()))
    signature = hashlib.sha256(f"9944822{claim}8{claim}113".encode()).hexdigest()
    return {
        "X-Signature-Version": "web2",
        "X-Claim": claim,
        "X-Signature": signature,
        "User-Agent": "okhttp/3.12.1",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def save_session(
    *,
    cookies: Optional[httpx.Cookies] = None,
    session_token: Optional[str] = None,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    auth_mode: str,
):
    saved = []
    if cookies:
        for c in cookies.jar:
            saved.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path or "/",
                    "secure": bool(c.secure),
                    "expires": c.expires,
                }
            )
    if not saved and not session_token:
        raise HTTPException(502, "Hanime login did not return a usable session")

    with db() as conn:
        conn.execute(
            """
            INSERT INTO hanime_session
                (id, cookies_json, session_token, user_id, user_name, auth_mode, updated_at)
            VALUES (1, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                cookies_json=excluded.cookies_json,
                session_token=excluded.session_token,
                user_id=excluded.user_id,
                user_name=excluded.user_name,
                auth_mode=excluded.auth_mode,
                updated_at=excluded.updated_at
            """,
            (json.dumps(saved), session_token, user_id, user_name, auth_mode, int(time.time())),
        )
        conn.commit()


def load_session() -> Optional[dict]:
    with db() as conn:
        return conn.execute("SELECT * FROM hanime_session WHERE id = 1").fetchone()


def clear_session():
    with db() as conn:
        conn.execute("DELETE FROM hanime_session WHERE id = 1")
        conn.commit()


def client_from_saved_session(*, follow_redirects: bool = True) -> httpx.AsyncClient:
    row = load_session()
    if not row:
        raise HTTPException(401, "Hanime account is not connected yet. Open /connect first.")

    cookies = httpx.Cookies()
    raw_cookies = row.get("cookies_json") or "[]"
    for item in json.loads(raw_cookies):
        try:
            cookies.set(
                item["name"],
                item["value"],
                domain=item.get("domain") or urlparse(HANIME_BASE_URL).hostname,
                path=item.get("path") or "/",
            )
        except Exception:
            cookies.set(item["name"], item["value"])

    headers = web_headers(HANIME_BASE_URL + "/")
    if row.get("session_token"):
        headers["X-Session-Token"] = row["session_token"]

    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=follow_redirects,
        headers=headers,
        cookies=cookies,
    )


def extract_csrf(page: str) -> Optional[str]:
    soup = BeautifulSoup(page, "html.parser")
    field = soup.find("input", attrs={"name": "_token"})
    if field and field.get("value"):
        return str(field.get("value"))
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return str(meta.get("content"))
    match = re.search(r'csrf-token[^>]+content=["\']([^"\']+)', page, re.I)
    return match.group(1) if match else None


def extract_user_id(page: str) -> Optional[str]:
    for pattern in (
        r'/user/(\d+)(?:/|["\'])',
        r'"user_id"\s*:\s*"?(\d+)"?',
        r'"id"\s*:\s*"?(\d+)"?\s*,\s*"name"',
    ):
        match = re.search(pattern, page, re.I)
        if match:
            return match.group(1)
    return None


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)).strip()


def parse_video_links(page: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    results, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href"))
        match = re.search(r"/(?:videos/)?hentai/([^/?#]+)", href)
        if not match:
            continue
        slug = match.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        img = anchor.find("img")
        title = (
            anchor.get("title")
            or (img.get("alt") if img else None)
            or anchor.get_text(" ", strip=True)
            or slug.replace("-", " ").title()
        )
        image = None
        if img:
            image = img.get("data-src") or img.get("src")
            if image:
                image = urljoin(base_url, str(image))
        results.append({
            "slug": slug,
            "title": clean_text(str(title)),
            "url": urljoin(base_url, href),
            "image": image,
        })
    return results


def parse_playlist_links(page: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    results, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href"))
        match = re.search(r"/playlists/([^/?#]+)", href)
        if not match:
            continue
        playlist_id = match.group(1)
        if playlist_id in seen:
            continue
        seen.add(playlist_id)
        title = anchor.get("title") or anchor.get_text(" ", strip=True) or playlist_id
        results.append({"id": playlist_id, "title": clean_text(str(title)), "url": urljoin(base_url, href)})
    return results


async def rapi_login(email: str, password: str) -> dict:
    url = f"{HANIME_RAPI_BASE}/rapi/v4/sessions"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.post(
            url,
            json={"burger": email, "fries": password},
            headers=rapi_headers(),
        )
    try:
        payload = response.json()
    except Exception:
        payload = {}

    token = payload.get("session_token") if isinstance(payload, dict) else None
    if response.status_code >= 400 or not token:
        raise HTTPException(
            401 if response.status_code in (400, 401, 403) else 502,
            f"Hanime API login failed (HTTP {response.status_code}). Check your email/password and try again.",
        )

    user = payload.get("user") or {}
    user_id = str(user.get("id")) if user.get("id") is not None else None
    user_name = user.get("name") or user.get("username")
    save_session(
        session_token=token,
        user_id=user_id,
        user_name=user_name,
        auth_mode="rapi",
    )
    return {"user_id": user_id, "user_name": user_name, "auth_mode": "rapi"}


async def website_login(email: str, password: str) -> Optional[dict]:
    login_url = f"{HANIME_BASE_URL}/login"
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=False,
        headers=web_headers(HANIME_BASE_URL + "/"),
    ) as client:
        login_page = await client.get(login_url)
        if login_page.status_code >= 400:
            return None
        csrf = extract_csrf(login_page.text)
        if not csrf:
            return None

        headers = web_headers(login_url)
        headers.update({"Origin": HANIME_BASE_URL, "X-CSRF-TOKEN": csrf})
        response = await client.post(
            login_url,
            data={"_token": csrf, "email": email, "password": password},
            headers=headers,
        )
        if response.status_code not in (200, 302, 303):
            return None

        verify = await client.get(HANIME_BASE_URL + "/")
        body = verify.text
        success_hint = response.status_code in (302, 303) or "/logout" in body or "/user/" in body
        if not success_hint:
            return None

        user_id = extract_user_id(body)
        save_session(cookies=client.cookies, user_id=user_id, auth_mode="website")
        return {"user_id": user_id, "user_name": None, "auth_mode": "website"}


async def fetch_account_page(path: str) -> tuple[httpx.Response, str]:
    async with client_from_saved_session() as client:
        response = await client.get(f"{HANIME_BASE_URL}/{path.lstrip('/')}")
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Hanime returned HTTP {response.status_code} for /{path.lstrip('/')}")
    final_url = str(response.url)
    if "/login" in final_url:
        raise HTTPException(401, "Hanime website session is not available for this account connection.")
    return response, final_url


@app.get("/")
def root():
    row = load_session()
    return {
        "name": "Hanime Helper",
        "status": "running",
        "account_connected": row is not None,
        "auth_mode": row.get("auth_mode") if row else None,
        "storage": "neon-postgres",
    }


@app.get("/health")
def health():
    return {"ok": True, "version": "0.2.0"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """
    <html><body style="font-family:sans-serif;max-width:720px;margin:40px auto;line-height:1.5">
    <h1>Hanime Helper Privacy Policy</h1>
    <p>This is a private personal helper. Credentials entered on the connection page are used only for the sign-in request and are not stored.</p>
    <p>Only the resulting Hanime session token/cookies and basic account identifier are stored in the owner's private Neon database.</p>
    <p>Do not publish the helper API key, database URL, or stored session credentials.</p>
    </body></html>
    """


@app.get("/connect", response_class=HTMLResponse)
def connect_page():
    return """
    <html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="font-family:sans-serif;max-width:520px;margin:40px auto;padding:0 18px;line-height:1.5">
      <h1>Connect Hanime.tv</h1>
      <p>Enter your Hanime credentials here. They are used only for sign-in and are <b>not stored</b>.</p>
      <form method="post" action="/connect">
        <label>Helper API key<br><input name="helper_key" type="password" required style="width:100%;padding:10px;margin:6px 0 14px"></label><br>
        <label>Hanime email<br><input name="email" type="email" required autocomplete="username" style="width:100%;padding:10px;margin:6px 0 14px"></label><br>
        <label>Hanime password<br><input name="password" type="password" required autocomplete="current-password" style="width:100%;padding:10px;margin:6px 0 14px"></label><br>
        <button type="submit" style="padding:10px 18px">Connect</button>
      </form>
    </body></html>
    """


@app.post("/connect", response_class=HTMLResponse)
async def connect_account(
    helper_key: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    if not HELPER_API_KEY or not secrets.compare_digest(helper_key, HELPER_API_KEY):
        raise HTTPException(401, "Invalid helper API key")

    # First try the ordinary website login. Some cloud hosts are blocked from
    # the login page (HTTP 403), so fall back to Hanime's session API.
    result = await website_login(email, password)
    if not result:
        result = await rapi_login(email, password)

    account_label = result.get("user_name") or result.get("user_id") or "connected account"
    return f"""
    <html><body style="font-family:sans-serif;max-width:620px;margin:40px auto;padding:0 18px">
      <h1>Connected ✅</h1>
      <p>Your Hanime session is stored in Neon. Your password was not stored.</p>
      <p>Account: {html.escape(str(account_label))}</p>
      <p>Connection mode: {html.escape(str(result.get('auth_mode')))}</p>
      <p>You can close this tab.</p>
    </body></html>
    """


@app.post("/disconnect")
def disconnect_account(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    clear_session()
    return {"ok": True, "connected": False}


@app.get("/search")
async def search_hanime(
    q: str = Query(..., min_length=1),
    page: int = Query(0, ge=0, le=100),
    limit: int = Query(20, ge=1, le=50),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    payload = {
        "blacklist": [], "brands": [], "order_by": "title_sortable", "ordering": "asc",
        "page": page, "search_text": q, "tags": [], "tags_mode": "AND",
    }
    async with httpx.AsyncClient(timeout=30, headers=web_headers()) as client:
        response = await client.post(HANIME_SEARCH_URL, json=payload)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Hanime search failed: HTTP {response.status_code}")
    data = response.json()
    hits = data.get("hits", [])
    if isinstance(hits, str):
        try:
            hits = json.loads(hits)
        except json.JSONDecodeError:
            hits = []
    results = []
    for item in (hits or [])[:limit]:
        slug = item.get("slug")
        results.append({
            "id": item.get("id"), "title": item.get("name") or item.get("title"), "slug": slug,
            "cover_url": item.get("cover_url"), "poster_url": item.get("poster_url"),
            "brand": item.get("brand"), "views": item.get("views"),
            "url": f"{HANIME_BASE_URL}/videos/hentai/{slug}" if slug else None,
        })
    return {"query": q, "page": page, "pages": data.get("nbPages"), "results": results}


@app.get("/video/{slug}")
async def get_hanime_video(slug: str, x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    async with httpx.AsyncClient(timeout=30, headers=web_headers()) as client:
        response = await client.get(f"{HANIME_BASE_URL}/api/v8/video", params={"id": slug})
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Hanime video lookup failed: HTTP {response.status_code}")
    data = response.json()
    video = data.get("hentai_video") or {}
    franchise = data.get("hentai_franchise") or {}
    episodes = []
    for item in data.get("hentai_franchise_hentai_videos") or []:
        ep_slug = item.get("slug")
        episodes.append({
            "id": item.get("id"), "title": item.get("name"), "slug": ep_slug,
            "cover_url": item.get("cover_url"), "views": item.get("views"),
            "url": f"{HANIME_BASE_URL}/videos/hentai/{ep_slug}" if ep_slug else None,
        })
    tags = [t.get("text") or t.get("name") for t in (data.get("hentai_tags") or video.get("hentai_tags") or [])]
    return {
        "id": video.get("id"), "title": video.get("name"), "slug": video.get("slug") or slug,
        "description": clean_text(video.get("description")), "poster_url": video.get("poster_url"),
        "cover_url": video.get("cover_url"), "brand": video.get("brand"), "views": video.get("views"),
        "likes": video.get("likes"), "dislikes": video.get("dislikes"), "released_at": video.get("released_at"),
        "tags": [t for t in tags if t],
        "franchise": {"id": franchise.get("id"), "title": franchise.get("title"), "slug": franchise.get("slug")},
        "episodes": episodes,
        "url": f"{HANIME_BASE_URL}/videos/hentai/{video.get('slug') or slug}",
    }


@app.get("/me/status")
async def account_status(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    row = load_session()
    if not row:
        return {"connected": False, "connect_url": f"{APP_BASE_URL}/connect" if APP_BASE_URL else "/connect"}
    return {
        "connected": True,
        "user_id": row.get("user_id"),
        "user_name": row.get("user_name"),
        "auth_mode": row.get("auth_mode"),
        "updated_at": row.get("updated_at"),
    }


@app.get("/me/probe")
async def probe_account_pages(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    row = load_session()
    if not row:
        raise HTTPException(401, "Hanime account is not connected yet. Open /connect first.")
    paths = ["account?tab=profile", "favorites", "history", "playlists", "subscriptions"]
    output = []
    async with client_from_saved_session() as client:
        for path in paths:
            try:
                response = await client.get(f"{HANIME_BASE_URL}/{path}")
                soup = BeautifulSoup(response.text, "html.parser")
                title = clean_text(soup.title.string if soup.title and soup.title.string else "")
                output.append({
                    "path": "/" + path,
                    "status": response.status_code,
                    "final_url": str(response.url),
                    "title": title,
                    "video_links": len(parse_video_links(response.text, HANIME_BASE_URL)),
                    "playlist_links": len(parse_playlist_links(response.text, HANIME_BASE_URL)),
                })
            except Exception as exc:
                output.append({"path": "/" + path, "error": str(exc)})
    return {"auth_mode": row.get("auth_mode"), "pages": output}


@app.get("/me/favorites")
async def get_favorites(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    response, final_url = await fetch_account_page("favorites")
    return {"source": final_url, "results": parse_video_links(response.text, HANIME_BASE_URL)}


@app.get("/me/history")
async def get_history(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    response, final_url = await fetch_account_page("history")
    return {"source": final_url, "results": parse_video_links(response.text, HANIME_BASE_URL)}


@app.get("/me/playlists")
async def get_playlists(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    response, final_url = await fetch_account_page("playlists")
    return {"source": final_url, "results": parse_playlist_links(response.text, HANIME_BASE_URL)}
