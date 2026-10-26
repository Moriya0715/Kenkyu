"""
data_collector.py

Slack 通知を行わず、データ取得と保存のみを行うプログラム。

取得対象:
  - 心拍数 / 歩数 / 消費カロリー -> data_output/<user>/value/heart_YYYY-MM-DD.json
  - 睡眠データ                   -> data_output/<user>/sleep/sleep_YYYY-MM-DD.json
  - Google Calendar 予定(free)    -> data_output/<user>/date/freebusy_YYYY-MM-DD.csv

実行日の前日 0:00 から実行時点までを対象に、DB 内の全ユーザー分を1回取得して終了する。
常駐してポーリングする（既定 3600 秒間隔）。
  python data_collector.py                 # 環境変数 COLLECT_INTERVAL_SECONDS または 3600 秒
  python data_collector.py --interval 600  # 600 秒間隔
  python data_collector.py --once          # 1回だけ実行して終了（Cron 併用・動作確認用）
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import List

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import env_loader

env_loader.load_env_file()

from db import connect as db_connect

import fitbit_sampler
import fitbit_sleep
import google_calendar_freebusy

VALUE_JSON_DIR = os.path.join('data_output', 'value')


def get_users() -> List[str]:
    """fitbit_tokens.user_id を優先し、なければ google_calendar_tokens.account_id を返す。"""
    users: List[str] = []
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        for query in (
            'SELECT DISTINCT user_id FROM fitbit_tokens',
            'SELECT DISTINCT account_id FROM google_calendar_tokens',
        ):
            try:
                cur.execute(query)
                users = [r[0] for r in cur.fetchall() if r and r[0]]
            except Exception:
                users = []
            if users:
                break
    except Exception:
        logging.exception('Failed to read users from DB')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return users


def collect_vitals(user_id: str, day: date):
    date_str = day.isoformat()
    per_minute, summary = fitbit_sampler.fetch_day(user_id, date_str, include_summary=True)
    path = fitbit_sampler.save_day_json(
        user_id,
        date_str,
        per_minute,
        out_dir=VALUE_JSON_DIR,
        resting_heart_rate=(summary or {}).get('resting_heart_rate'),
    )
    logging.info('Saved vitals: %s', path)


def collect_sleep(user_id: str, now: datetime, start_date: date):
    tz = now.tzinfo or timezone.utc
    end_date = now.date()
    sessions = fitbit_sleep._fetch_sleep_for_window(user_id, start_date, end_date, tz)
    by_date = fitbit_sleep._group_sessions_by_end_date(sessions, start_date, end_date, tz)
    saved = 0
    for date_str, daily in by_date.items():
        path = fitbit_sleep._sleep_file_path(user_id, date.fromisoformat(date_str))
        # 空の結果で既存の睡眠データを上書きしない
        if not daily and fitbit_sleep._saved_file_has_sessions(path):
            continue
        fitbit_sleep._save_sessions_to_disk(user_id, daily, now, date_tag=date_str)
        saved += len(daily)
    logging.info('Saved sleep: user=%s range=%s..%s sessions=%s', user_id, start_date, end_date, saved)


def collect_user(user_id: str, now: datetime):
    today = now.date()
    days = [today - timedelta(days=1), today]
    steps = [(f'vitals {d}', lambda d=d: collect_vitals(user_id, d)) for d in days]
    steps.append(('sleep', lambda: collect_sleep(user_id, now, days[0])))
    steps += [(f'calendar {d}', lambda d=d: google_calendar_freebusy.main(account_id=user_id, target_date=d)) for d in days]
    for name, func in steps:
        try:
            logging.info('Collecting %s for %s', name, user_id)
            func()
        except Exception:
            logging.exception('Collecting %s failed for user=%s', name, user_id)


def run_once() -> bool:
    users = get_users()
    if not users:
        logging.error('No users found in DB.')
        return False
    now = datetime.now().astimezone()
    for user in users:
        collect_user(user, now)
    return True


def main():
    parser = argparse.ArgumentParser(description='Fitbit / Google Calendar データ収集（定期ポーリング）')
    parser.add_argument(
        '--interval',
        type=int,
        default=int(os.getenv('COLLECT_INTERVAL_SECONDS', '3600')),
        help='ポーリング間隔（秒）。既定: 環境変数 COLLECT_INTERVAL_SECONDS または 3600',
    )
    parser.add_argument('--once', action='store_true', help='1回だけ実行して終了する')
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error('--interval は1以上の秒数を指定してください')

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

    if args.once:
        return 0 if run_once() else 1

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    logging.info('Polling started: interval=%ss', args.interval)
    while not stop.is_set():
        started = time.monotonic()
        try:
            run_once()
        except Exception:
            logging.exception('Polling cycle failed')
        wait = max(0.0, args.interval - (time.monotonic() - started))
        logging.info('Next run in %.0fs', wait)
        stop.wait(wait)
    logging.info('Polling stopped.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
