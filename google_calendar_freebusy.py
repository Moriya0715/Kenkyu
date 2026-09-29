"""
google_calendar_freebusy.py

目的:
    - Google カレンダーから指定期間の busy 情報を取得し、予約の入っていない時間帯（最短 30 分）を CSV に出力します。

前提:
  - OAuth クライアント方式 (credentials.json) を使用します。
    Google Cloud Console で作成した OAuth クレデンシャル（Desktop）を `credentials.json` として
    スクリプト実行ディレクトリに置いてください。初回実行時にブラウザで認可が求められます。
  - 必要パッケージ:
      pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib

使い方:
  python google_calendar_freebusy.py

出力:
  CSV を `data_output/date/freebusy_YYYY-MM-DD.csv` に保存します。カラム: start_iso,end_iso,duration_min

設計上の決定:
- 対象カレンダー: 
- 期間: 当日 00:00:00 から本日 23:59:59 まで
    - 営業時間制限は廃止（全日を対象）
  - 最短空き枠: 30 分
"""

from __future__ import print_function
import logging
from datetime import datetime, time, timedelta
import os
import csv
import sys
import json
from db import connect as db_connect, DB_URL
import argparse

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
except Exception as e:
    print("Missing Google API libraries. Install with:\n  pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    raise

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


DB_PATH = os.environ.get('TOKEN_DB') or os.environ.get('FITBIT_TOKEN_DB') or None


def load_credentials(account_id: str = 'seiichirou019@gmail.com'):
    """Load credentials preferably from tokens.db -> google_calendar_tokens. If not found,
    fall back to token.json / credentials.json flow.
    If credentials are refreshed, update the DB row.
    """
    creds = None

    # Try DB first (only when DATABASE_URL points to Postgres)
    try:
        is_pg = bool(DB_URL and DB_URL.startswith(('postgres://', 'postgresql://')))
    except Exception:
        is_pg = False
    if is_pg:
        try:
            conn = db_connect()
            cur = conn.cursor()
            cur.execute('SELECT access_token, refresh_token, scope, token_type, expiry, raw_json FROM google_calendar_tokens WHERE account_id=?', (account_id,))
            row = cur.fetchone()
            if row:
                access_token, refresh_token, scope, token_type, expiry, raw_json = row
                # raw_json may contain original structure from google-auth
                info = None
                if raw_json:
                    try:
                        info = json.loads(raw_json)
                    except Exception:
                        info = None

                # Build info dict expected by Credentials.from_authorized_user_info
                info2 = {}
                if info and isinstance(info, dict):
                    # try common keys
                    # google oauthlib sometimes stores under 'token' or 'access_token'
                    if 'token' in info:
                        info2['token'] = info.get('token')
                    if 'access_token' in info:
                        info2['token'] = info.get('access_token')
                    if 'refresh_token' in info:
                        info2['refresh_token'] = info.get('refresh_token')
                    if 'client_id' in info:
                        info2['client_id'] = info.get('client_id')
                    if 'client_secret' in info:
                        info2['client_secret'] = info.get('client_secret')
                    if 'token_uri' in info:
                        info2['token_uri'] = info.get('token_uri')
                    if 'scopes' in info:
                        s = info.get('scopes')
                        info2['scopes'] = ' '.join(s) if isinstance(s, list) else s
                    if 'expiry' in info:
                        info2['expiry'] = info.get('expiry')

                # fallback to simpler columns
                if not info2.get('token') and access_token:
                    info2['token'] = access_token
                if not info2.get('refresh_token') and refresh_token:
                    info2['refresh_token'] = refresh_token
                if not info2.get('scopes') and scope:
                    info2['scopes'] = scope

                # Only construct creds if we have at least one token-like field
                if info2:
                    try:
                        creds = Credentials.from_authorized_user_info(info2, SCOPES)
                    except Exception:
                        creds = None

        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Fallback to file-based flow
    if not creds:
        if os.path.exists('token.json'):
            try:
                creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            except Exception:
                creds = None

    # If we have a refresh token, always attempt to refresh to get a fresh access token
    if creds and getattr(creds, 'refresh_token', None):
        try:
            creds.refresh(Request())
            logging.info('Refreshed credentials for account %s', account_id)
        except Exception as e:
            # keep creds as-is for now; we'll fall back to interactive flow if it's invalid
            logging.warning('Credential refresh failed for account %s: %s', account_id, e)

    # If no valid credentials available, let the user log in.
    if not creds or not creds.valid:
        # try to refresh if possible (some flows might not have refreshed above)
        if creds and getattr(creds, 'refresh_token', None):
            try:
                creds.refresh(Request())
                logging.info('Refreshed credentials for account %s (second attempt)', account_id)
            except Exception as e:
                logging.warning('Second refresh attempt failed for account %s: %s', account_id, e)
                creds = None
        if not creds:
            if not os.path.exists('credentials.json'):
                logging.error('credentials.json not found. Create OAuth client credentials and save as credentials.json in this directory.')
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open('token.json', 'w', encoding='utf-8') as token:
                token.write(creds.to_json())

    # If creds came from DB and was refreshed, update DB (only when Postgres configured)
    try:
        if creds and creds.valid and is_pg:
            # update raw_json and useful columns under a per-user lock to avoid races
            try:
                import storage_sqlite
                with storage_sqlite.user_lock(account_id):
                    conn = db_connect()
                    cur = conn.cursor()
                    raw = creds.to_json()
                    access_token = getattr(creds, 'token', None)
                    refresh_token = getattr(creds, 'refresh_token', None)
                    expiry = getattr(creds, 'expiry', None)
                    scope = ' '.join(creds.scopes) if getattr(creds, 'scopes', None) else None
                    # Prefer updating an existing row that already has the same refresh_token
                    updated = False
                    if refresh_token:
                        try:
                            cur.execute('SELECT account_id FROM google_calendar_tokens WHERE refresh_token=?', (refresh_token,))
                            r = cur.fetchone()
                            if r:
                                existing_account = r[0]
                                cur.execute('''
                                    UPDATE google_calendar_tokens SET
                                      access_token=?,
                                      expiry=?,
                                      scope=?,
                                      raw_json=?
                                    WHERE account_id=?
                                ''', (access_token, str(expiry), scope, raw, existing_account))
                                conn.commit()
                                updated = True
                        except Exception:
                            updated = False

                    if not updated:
                        # No existing row matched by refresh_token; upsert by account_id
                        cur.execute('''
                            INSERT INTO google_calendar_tokens(account_id, access_token, refresh_token, scope, token_type, expiry, raw_json)
                            VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(account_id) DO UPDATE SET
                              access_token=excluded.access_token,
                              refresh_token=excluded.refresh_token,
                              scope=excluded.scope,
                              token_type=excluded.token_type,
                              expiry=excluded.expiry,
                              raw_json=excluded.raw_json
                        ''', (account_id, access_token, refresh_token, scope, None, str(expiry), raw))
                        conn.commit()
            except Exception as e:
                logging.exception('Failed to update DB for account %s: %s', account_id, e)
    except Exception as e:
        print(f'Unexpected DB update error: {e}')
    finally:
        try:
            if is_pg:
                conn.close()
        except Exception:
            pass

    return creds


def query_freebusy(service, calendar_id, time_min_iso, time_max_iso):
    body = {
        "timeMin": time_min_iso,
        "timeMax": time_max_iso,
        "items": [{"id": calendar_id}]
    }
    fb = service.freebusy().query(body=body).execute()
    return fb.get('calendars', {}).get(calendar_id, {}).get('busy', [])


def subtract_busy_from_range(free_range, busy_ranges):
    """free_range: (start_dt, end_dt), busy_ranges: list of dicts with start/end ISO strings
       returns list of (start_dt, end_dt) free intervals after subtracting busy ranges
    """
    free_list = [free_range]
    busy_intervals = []
    for b in busy_ranges:
        try:
            s = datetime.fromisoformat(b['start'])
            e = datetime.fromisoformat(b['end'])
            busy_intervals.append((s, e))
        except Exception:
            continue
    busy_intervals.sort()

    result = []
    for fs, fe in free_list:
        cur_start = fs
        for bs, be in busy_intervals:
            # no overlap
            if be <= cur_start or bs >= fe:
                continue
            # overlap
            if bs <= cur_start < be:
                cur_start = max(cur_start, be)
            elif cur_start < bs < fe:
                # free from cur_start to bs
                result.append((cur_start, bs))
                cur_start = max(cur_start, be)
        if cur_start < fe:
            result.append((cur_start, fe))
    return result


def format_iso(dt):
    # Ensure output includes local timezone offset (RFC3339)
    if dt.tzinfo is None:
        # attach local timezone
        local_tz = datetime.now().astimezone().tzinfo
        dt = dt.replace(tzinfo=local_tz)
    return dt.isoformat()


def main(account_id: str = None):
    # Settings per user's choices
    calendar_id = account_id or 'seiichirou019@gmail.com'
    min_slot_minutes = 30

    # Ensure we load credentials for the same account/calendar we will query
    creds = load_credentials(calendar_id)
    service = build('calendar', 'v3', credentials=creds)

    # use timezone-aware 'now' so comparisons with API datetimes (which include offsets) work
    now = datetime.now().astimezone()
    today = now.date()
    # period: today 00:00:00 .. today 23:59:59
    time_min = datetime.combine(today, time(0, 0, 0)).replace(tzinfo=now.tzinfo)
    # make time_max timezone-aware using same tzinfo as now
    time_max = datetime.combine(today, time(23, 59, 59)).replace(tzinfo=now.tzinfo)

    # Search for whole day (no business-hour restriction)
    search_start = time_min
    search_end = time_max
    time_min_iso = format_iso(search_start)
    time_max_iso = format_iso(search_end)

    busy = query_freebusy(service, calendar_id, time_min_iso, time_max_iso)

    # busy is list of {'start': iso, 'end': iso}
    free_intervals = subtract_busy_from_range((search_start, search_end), busy)

    # filter by min_slot_minutes
    final_slots = []
    for s, e in free_intervals:
        dur = (e - s).total_seconds() / 60.0
        if dur >= min_slot_minutes:
            final_slots.append((s, e, int(dur)))

    # Output CSV
    # write per-user freebusy under data_output/<sanitized_user>/date/
    try:
        import storage_sqlite
        safe = storage_sqlite._sanitize_user_id(calendar_id)
        out_dir = os.path.join('data_output', safe, 'date')
    except Exception:
        out_dir = os.path.join('data_output', 'date')
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f'freebusy_{today.strftime("%Y-%m-%d")}.csv')
    with open(out_file, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        # Keep original columns for compatibility, then add human-friendly columns
        writer.writerow([
            'start_iso', 'end_iso', 'duration_min',
            'start_local', 'end_local', 'start_time', 'end_time', 'weekday', 'duration_hm', 'range'
        ])
        for s, e, dur in final_slots:
            start_iso = format_iso(s)
            end_iso = format_iso(e)
            # local readable
            try:
                start_local = s.astimezone().strftime('%Y-%m-%d %H:%M %z') if getattr(s, 'tzinfo', None) else s.strftime('%Y-%m-%d %H:%M')
            except Exception:
                start_local = s.strftime('%Y-%m-%d %H:%M')
            try:
                end_local = e.astimezone().strftime('%Y-%m-%d %H:%M %z') if getattr(e, 'tzinfo', None) else e.strftime('%Y-%m-%d %H:%M')
            except Exception:
                end_local = e.strftime('%Y-%m-%d %H:%M')
            start_time = s.strftime('%H:%M')
            end_time = e.strftime('%H:%M')
            weekday = s.strftime('%a')
            hours = int(dur // 60)
            minutes = int(dur % 60)
            duration_hm = f"{hours}h{minutes}m" if hours else f"{minutes}m"
            human_range = f"{start_local} - {end_local} ({weekday})"

            writer.writerow([start_iso, end_iso, dur, start_local, end_local, start_time, end_time, weekday, duration_hm, human_range])

    logging.info('Saved free slots to %s', out_file)


if __name__ == '__main__':
    main()
