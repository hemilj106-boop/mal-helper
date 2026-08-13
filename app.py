import os
import secrets
import sqlite3
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel, Field

MAL_CLIENT_ID = os.getenv("MAL_CLIENT_ID", "")
MAL_CLIENT_SECRET = os.getenv("MAL_CLIENT_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
HELPER_API_KEY = os.getenv("HELPER_API_KEY", "")
TOKEN_DB_PATH = os.getenv("TOKEN_DB_PATH", "tokens.db")

MAL_AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
MAL_API_BASE = "https://api.myanimelist.net/v2"

app = FastAPI(
    title="MAL Helper",
    version="1.0.0",
    description="Personal MyAnimeList helper API for a Custom GPT."
)


def db():
    conn = sqlite3.connect(TOKEN_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_state (
                state TEXT PRIMARY KEY,
                verifier TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mal_token (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        conn.commit()


@app.on_event("startup")
def startup():
    init_db()


def require_env():
    missing = []
    if not MAL_CLIENT_ID:
        missing.append("MAL_CLIENT_ID")
    if not MAL_CLIENT_SECRET:
        missing.append("MAL_CLIENT_SECRET")
    if not APP_BASE_URL:
        missing.append("APP_BASE_URL")
    if not HELPER_API_KEY:
        missing.append("HELPER_API_KEY")
    if missing:
        raise HTTPException(500, f"Missing environment variables: {', '.join(missing)}")


def check_api_key(x_api_key: Optional[str]):
    if not HELPER_API_KEY:
        raise HTTPException(500, "HELPER_API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, HELPER_API_KEY):
        raise HTTPException(401, "Invalid API key")


def make_pkce_verifier() -> str:
    # 96 random bytes -> 128 URL-safe characters, suitable for MAL PKCE.
    return secrets.token_urlsafe(96)[:128]


def save_token(payload: dict):
    access_token = payload["access_token"]
    refresh_token = payload["refresh_token"]
    expires_in = int(payload.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in - 60
    with db() as conn:
        conn.execute("""
            INSERT INTO mal_token (id, access_token, refresh_token, expires_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at
        """, (access_token, refresh_token, expires_at))
        conn.commit()


def get_saved_token():
    with db() as conn:
        return conn.execute("SELECT * FROM mal_token WHERE id = 1").fetchone()


async def get_access_token() -> str:
    require_env()
    row = get_saved_token()
    if not row:
        raise HTTPException(401, "MAL is not authorized yet. Open /auth/start first.")

    if int(row["expires_at"]) > int(time.time()):
        return row["access_token"]

    data = {
        "client_id": MAL_CLIENT_ID,
        "client_secret": MAL_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": row["refresh_token"],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(MAL_TOKEN_URL, data=data)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Could not refresh MAL token: {resp.text}")

    payload = resp.json()
    # Some OAuth servers may omit a new refresh token. Keep the old one if so.
    payload["refresh_token"] = payload.get("refresh_token") or row["refresh_token"]
    save_token(payload)
    return payload["access_token"]


async def mal_request(method: str, path: str, *, params=None, data=None):
    token = await get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            f"{MAL_API_BASE}{path}",
            headers=headers,
            params=params,
            data=data,
        )
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)
    if resp.status_code == 204 or not resp.content:
        return {"ok": True}
    return resp.json()


class ListUpdate(BaseModel):
    status: Optional[str] = Field(
        default=None,
        description="watching, completed, on_hold, dropped, or plan_to_watch"
    )
    score: Optional[int] = Field(default=None, ge=0, le=10)
    num_watched_episodes: Optional[int] = Field(default=None, ge=0)
    is_rewatching: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=2)
    num_times_rewatched: Optional[int] = Field(default=None, ge=0)
    rewatch_value: Optional[int] = Field(default=None, ge=0, le=5)
    tags: Optional[str] = None
    comments: Optional[str] = None


@app.get("/")
def root():
    return {
        "name": "MAL Helper",
        "status": "running",
        "authorized": get_saved_token() is not None,
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """
    <html><body style="font-family:sans-serif;max-width:700px;margin:40px auto">
    <h1>MAL Helper Privacy Policy</h1>
    <p>This is a private personal helper. It stores MyAnimeList OAuth tokens only
    so the owner can manage their own anime list. It does not sell or share data.</p>
    <p>Do not publish your MAL client secret, helper API key, or OAuth tokens.</p>
    </body></html>
    """


@app.get("/auth/start")
def auth_start():
    require_env()
    verifier = make_pkce_verifier()
    state = secrets.token_urlsafe(32)

    with db() as conn:
        # Clean stale states older than 20 minutes.
        conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (int(time.time()) - 1200,))
        conn.execute(
            "INSERT INTO oauth_state (state, verifier, created_at) VALUES (?, ?, ?)",
            (state, verifier, int(time.time()))
        )
        conn.commit()

    callback = f"{APP_BASE_URL}/auth/callback"
    params = {
        "response_type": "code",
        "client_id": MAL_CLIENT_ID,
        "code_challenge": verifier,
        "state": state,
        "redirect_uri": callback,
    }
    from urllib.parse import urlencode
    return RedirectResponse(f"{MAL_AUTH_URL}?{urlencode(params)}")


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(code: str = Query(...), state: str = Query(...)):
    require_env()
    with db() as conn:
        row = conn.execute(
            "SELECT verifier FROM oauth_state WHERE state = ?",
            (state,)
        ).fetchone()
        conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,))
        conn.commit()

    if not row:
        raise HTTPException(400, "Invalid or expired OAuth state")

    callback = f"{APP_BASE_URL}/auth/callback"
    data = {
        "client_id": MAL_CLIENT_ID,
        "client_secret": MAL_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": row["verifier"],
        "redirect_uri": callback,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(MAL_TOKEN_URL, data=data)

    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"MAL token exchange failed: {resp.text}")

    save_token(resp.json())
    return """
    <html><body style="font-family:sans-serif;max-width:700px;margin:40px auto">
    <h1>Connected ✅</h1>
    <p>Your MAL Helper is now authorized. You can close this tab.</p>
    </body></html>
    """


@app.get("/anime/search")
async def search_anime(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=100),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    result = await mal_request(
        "GET",
        "/anime",
        params={
            "q": q,
            "limit": limit,
            "fields": "id,title,alternative_titles,main_picture,start_date,end_date,media_type,status,num_episodes",
        },
    )
    return result


@app.get("/anime/{anime_id}")
async def get_anime(
    anime_id: int,
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    return await mal_request(
        "GET",
        f"/anime/{anime_id}",
        params={
            "fields": "id,title,alternative_titles,main_picture,start_date,end_date,synopsis,media_type,status,num_episodes,my_list_status"
        },
    )


@app.put("/anime/{anime_id}/list")
async def update_list(
    anime_id: int,
    body: ListUpdate,
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(400, "Provide at least one field to update")
    return await mal_request(
        "PUT",
        f"/anime/{anime_id}/my_list_status",
        data=payload,
    )


@app.delete("/anime/{anime_id}/list")
async def delete_from_list(
    anime_id: int,
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    return await mal_request(
        "DELETE",
        f"/anime/{anime_id}/my_list_status",
    )


@app.get("/me/anime-list")
async def my_anime_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(100, ge=1, le=1000),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    params = {
        "limit": limit,
        "fields": "list_status,num_episodes,media_type,status",
        "sort": "list_updated_at",
    }
    if status:
        params["status"] = status
    return await mal_request("GET", "/users/@me/animelist", params=params)
