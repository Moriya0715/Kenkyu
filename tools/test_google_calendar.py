import os
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import psycopg2
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


ENV_PATH = r"C:\Users\seiic\kenkyu_new\.env"  # 自分の.env絶対パスに変更
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def load_env():
    env_file = Path(ENV_PATH)
    if not env_file.exists():
        raise FileNotFoundError(f".env が見つかりません: {ENV_PATH}")

    with env_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_conn():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL が .env にありません")
    return psycopg2.connect(database_url)


def fetch_tokens(conn):
    sql = """
        SELECT
            account_id,
            access_token,
            refresh_token,
            scope,
            token_type,
            expiry,
            raw_json
        FROM google_calendar_tokens
        WHERE refresh_token IS NOT NULL
        ORDER BY account_id;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def parse_raw_json(raw_json):
    if not raw_json:
        return {}

    if isinstance(raw_json, dict):
        return raw_json

    if isinstance(raw_json, str):
        return json.loads(raw_json)

    return {}


def create_credentials(row):
    account_id, access_token, refresh_token, scope, token_type, expiry, raw_json = row

    raw = parse_raw_json(raw_json)

    client_id = raw.get("client_id")
    client_secret = raw.get("client_secret")
    token_uri = raw.get("token_uri", "https://oauth2.googleapis.com/token")

    if not client_id or not client_secret:
        raise RuntimeError(
            f"{account_id}: raw_json に client_id / client_secret がありません"
        )

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )

    if expiry:
        expiry_dt = expiry

        if isinstance(expiry_dt, str):
            expiry_dt = datetime.fromisoformat(expiry_dt.replace("Z", "+00:00"))

        if expiry_dt.tzinfo is not None:
            expiry_dt = expiry_dt.astimezone(timezone.utc).replace(tzinfo=None)

        creds.expiry = expiry_dt

    if not creds.valid:
        print(f"{account_id}: access_token 更新中...")
        creds.refresh(Request())

    return account_id, creds


def print_token_debug(row):
    account_id, access_token, refresh_token, scope, token_type, expiry, raw_json = row

    print("\n" + "=" * 80)
    print(f"DB account_id: {account_id}")
    print(f"access_token先頭:  {access_token[:25] if access_token else None}")
    print(f"refresh_token先頭: {refresh_token[:25] if refresh_token else None}")
    print(f"scope: {scope}")
    print(f"token_type: {token_type}")
    print(f"expiry: {expiry}")

    raw = parse_raw_json(raw_json)
    print(f"raw_json client_id: {raw.get('client_id')}")
    print(f"raw_json refresh_token先頭: {raw.get('refresh_token', '')[:25] if raw.get('refresh_token') else None}")


def print_calendar_list(account_id, creds):
    service = build("calendar", "v3", credentials=creds)

    result = service.calendarList().list().execute()
    calendars = result.get("items", [])

    print("\n--- Calendar List ---")
    print(f"DB account_id: {account_id}")
    print(f"取得カレンダー数: {len(calendars)}")

    for cal in calendars:
        print("-" * 40)
        print(f"id: {cal.get('id')}")
        print(f"summary: {cal.get('summary')}")
        print(f"primary: {cal.get('primary', False)}")
        print(f"accessRole: {cal.get('accessRole')}")


def print_events_for_calendar(service, calendar_id, label, days=14):
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    result = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=20,
    ).execute()

    events = result.get("items", [])

    print("\n--- Events ---")
    print(f"calendar: {label}")
    print(f"calendarId: {calendar_id}")
    print(f"取得期間: {time_min} ～ {time_max}")
    print(f"取得件数: {len(events)}")

    if not events:
        print("予定なし")
        return

    for ev in events:
        summary = ev.get("summary", "(タイトルなし)")
        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
        transparency = ev.get("transparency", "opaque/予定あり扱い")

        print("-" * 40)
        print(f"予定名: {summary}")
        print(f"開始: {start}")
        print(f"終了: {end}")
        print(f"transparency: {transparency}")


def print_events(account_id, creds):
    service = build("calendar", "v3", credentials=creds)

    # primaryを取得
    print_events_for_calendar(
        service=service,
        calendar_id="primary",
        label=f"{account_id} / primary",
        days=14,
    )

    # 念のため、見えている全カレンダーも取得
    calendar_list = service.calendarList().list().execute().get("items", [])

    for cal in calendar_list:
        calendar_id = cal.get("id")
        summary = cal.get("summary", "(no summary)")

        if not calendar_id or calendar_id == "primary":
            continue

        try:
            print_events_for_calendar(
                service=service,
                calendar_id=calendar_id,
                label=summary,
                days=14,
            )
        except Exception as e:
            print(f"{summary}: 予定取得失敗: {e}")


def main():
    load_env()

    with get_conn() as conn:
        rows = fetch_tokens(conn)

    print(f"DBから取得したGoogle Calendarトークン数: {len(rows)}")

    if not rows:
        print("google_calendar_tokens に refresh_token がある行がありません")
        return

    for row in rows:
        account_id = row[0]

        try:
            print_token_debug(row)
            account_id, creds = create_credentials(row)
            print_calendar_list(account_id, creds)
            print_events(account_id, creds)

        except Exception as e:
            print("\n" + "=" * 80)
            print(f"{account_id}: 取得失敗")
            print(f"理由: {e}")


if __name__ == "__main__":
    main()