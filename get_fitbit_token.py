#get_fitbit_token.py

"""
アクセストークンを自動で更新，取得するプログラム
使用方法は使い方を参照
関連プログラム
    register_token_data.py

複数ユーザ対応。Google OAuth2 の refresh_token を使って
Google Health API 用の access_token を取得して返す最小モジュール。

アクセストークンを自動で更新，取得するプログラム
関連プログラム: register_token_data.py

複数ユーザ対応。Fitbit OAuth2 の refresh_token を使って
新しい access_token(AC) / refresh_token(RT) を取得して返す最小モジュール。

使い方（他プログラムから）:
    register_token_data.py で user_id, 初回のリフレッシュトークン, client_id, client_secret を登録する。
    登録後は get_fitbit_token.get_new_tokens を呼ぶことで新しいトークンを入手できる。
"""

import os
import time
import datetime
from db import connect as db_connect
import requests
from typing import Tuple, Optional
import storage_sqlite

DB_PATH = os.environ.get("TOKEN_DB") or os.environ.get("FITBIT_TOKEN_DB") or None
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# --- DB helpers --------------------------------------------------------------

def _db():
    # use db.connect() which will return sqlite3 connection or psycopg2 connection
    con = db_connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS fitbit_tokens(
            user_id TEXT PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            token_type TEXT,
            scope TEXT,
            expires_at INTEGER,
            obtained_at INTEGER,
            client_id TEXT,
            client_secret TEXT,
            oauth_type TEXT
        )
    """)
    con.execute("ALTER TABLE fitbit_tokens ADD COLUMN IF NOT EXISTS oauth_type TEXT")
    return con

def register_user(user_id: str, refresh_token: str, client_id: str, client_secret: Optional[str] = None) -> None:
    """Google OAuthで得たrefresh tokenとクライアント情報を登録する。"""
    now = int(time.time())
    con = _db()
    con.execute("""
            INSERT INTO fitbit_tokens(user_id, refresh_token, client_id, client_secret, obtained_at, oauth_type)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                refresh_token=excluded.refresh_token,
                client_id=excluded.client_id,
                client_secret=excluded.client_secret,
                obtained_at=excluded.obtained_at,
                oauth_type=excluded.oauth_type
    """, (user_id, refresh_token, client_id, client_secret, now, "google_health"))

def get_registered_refresh_token(user_id: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    """指定ユーザの更新用OAuth情報を取り出す。"""
    con = _db()
    row = con.execute(
        "SELECT refresh_token, client_id, client_secret, scope "
        "FROM fitbit_tokens WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        raise RuntimeError(f"user_id={user_id} の refresh_token または client_id が未登録です")
    return row

def _save_tokens(user_id: str, access_token: str, refresh_token: str,
                 token_type: Optional[str], scope: Optional[str],
                 expires_in: int, client_id: str, client_secret: Optional[str]) -> None:
    now = int(time.time())
    expires_at = now + int(expires_in or 3600)
    con = _db()
    con.execute("""
      INSERT INTO fitbit_tokens(user_id, access_token, refresh_token, token_type, scope,
                   expires_at, obtained_at, client_id, client_secret, oauth_type)
      VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET
        access_token=excluded.access_token,
        refresh_token=excluded.refresh_token,
        token_type=excluded.token_type,
        scope=excluded.scope,
        expires_at=excluded.expires_at,
        obtained_at=excluded.obtained_at,
        client_id=excluded.client_id,
        client_secret=excluded.client_secret,
        oauth_type='google_health'
    """, (user_id, access_token, refresh_token, token_type, scope,
        expires_at, now, client_id, client_secret, "google_health"))

# --- OAuth helpers -----------------------------------------------------------

def refresh_with_rt(user_id: str, rt: str, client_id: str, client_secret: Optional[str]) -> dict:
    """Google OAuth refresh tokenでGoogle Health用access tokenを更新する。"""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
    }

    data["client_id"] = client_id
    if client_secret:
        data["client_secret"] = client_secret

    r = requests.post(GOOGLE_TOKEN_URL, headers=headers, data=data, timeout=30)
    # If non-2xx, include response body in logs for easier diagnosis
    if not r.ok:
        try:
            body = r.text
        except Exception:
            body = '<unreadable response body>'
        # attach status and body to raised exception for callers to log
        msg = f'OAuth token refresh failed: {r.status_code} {r.reason} - {body}'
        raise RuntimeError(msg)
    try:
        return r.json()
    except Exception:
        # unexpected non-JSON success response
        return {"raw_text": r.text}

# --- Public API --------------------------------------------------------------

def get_new_tokens(user_id: str) -> Tuple[str, str, int]:
    """
    指定ユーザの登録済み情報でトークン更新を行い、
    新しい (access_token, refresh_token, expires_at) を返す。
    """
    current_rt, client_id, client_secret, current_scope = get_registered_refresh_token(user_id)

    # Perform the network refresh outside the per-user lock. Holding the
    # lock while doing an HTTP request can cause long-lived DB locks when
    # multiple processes/threads run concurrently. We still acquire the
    # per-user lock briefly to persist the new tokens to avoid write races.
    body = refresh_with_rt(user_id, current_rt, client_id, client_secret)

    ac = body["access_token"]
    rt = body.get("refresh_token") or current_rt
    token_type = body.get("token_type", "Bearer")
    scope = body.get("scope") or current_scope
    expires_in = int(body.get("expires_in", 3600))

    # Acquire per-user lock only while saving tokens into the DB
    with storage_sqlite.user_lock(user_id):
        _save_tokens(user_id, ac, rt, token_type, scope, expires_in, client_id, client_secret)
    expires_at = int(time.time()) + expires_in
    return ac, rt, expires_at

def get_cached_access_token(user_id: str, safety_margin_sec: int = 120) -> Tuple[str, int, datetime.datetime]:
    """期限内ならDBのACを返し、期限が近ければ更新"""
    con = _db()
    row = con.execute("SELECT access_token, expires_at FROM fitbit_tokens WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row[0]:
        ac, _, exp = get_new_tokens(user_id)
        exp_dt = datetime.datetime.fromtimestamp(exp)
        return ac, exp, exp_dt
    ac, exp = row
    if time.time() >= (exp or 0) - safety_margin_sec:
        ac, _, exp = get_new_tokens(user_id)
    exp_dt = datetime.datetime.fromtimestamp(exp)
    return ac, exp, exp_dt                           #exp:人にはわからない有効期限,exp_dtにすることで，分かりやすく日付表記にした
