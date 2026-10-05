"""Dedicated Socket Mode server that centralizes interactive Slack handlers.

Usage: set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in env and run this module.
This script reuses `send_slack_checkbox.create_app` to register handlers, and
runs a background poller to send queued notifications from the DB.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import atexit
import copy
import signal
import subprocess

from slack_bolt.adapter.socket_mode import SocketModeHandler

repo_root = os.path.dirname(os.path.abspath(__file__))
LOCK_NAME = 'socket_server'
LOGS_DIR = os.path.join(repo_root, 'logs')

# Ensure .env is loaded before importing modules that may read DB settings
try:
    dotenv_path = os.path.join(repo_root, '.env')
    if os.path.exists(dotenv_path):
        with open(dotenv_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and os.environ.get(k) is None:
                    os.environ[k] = v
except Exception:
    pass

try:
    import send_slack_checkbox
except Exception as e:
    print('Failed to import send_slack_checkbox:', e)
    raise

import slack_notifier
import lock_db

# Load .env from repo root early so imported modules see DATABASE_URL etc.
def _load_dotenv(path: str = '.env'):
    try:
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and os.environ.get(k) is None:
                    os.environ[k] = v
    except Exception:
        pass

try:
    repo_root = os.path.dirname(os.path.abspath(__file__))
    dotenv_path = os.path.join(repo_root, '.env')
    _load_dotenv(dotenv_path)
except Exception:
    pass

try:
    import storage_sqlite
    storage_sqlite.ensure_schema()
except Exception:
    storage_sqlite = None




def setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    logpath = os.path.join(LOGS_DIR, 'socket_server.log')
    level = logging.DEBUG if os.environ.get('DEBUG_SLACK') else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.FileHandler(logpath, encoding='utf-8'), logging.StreamHandler()],
    )


def _ensure_release_on_exit(release_fn):
    def _cleanup():
        try:
            release_fn()
        except Exception:
            pass

    atexit.register(_cleanup)
    # also attempt cleanup on common termination signals
    try:
        def _sig_handler(signum, frame):
            try:
                _cleanup()
            finally:
                # re-raise default to allow termination
                sys.exit(0)

        for sig in ('SIGINT', 'SIGTERM'):
            if hasattr(signal, sig):
                signal.signal(getattr(signal, sig), _sig_handler)
        # Windows: handle CTRL_BREAK_EVENT if available
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, _sig_handler)
    except Exception:
        pass


def _process_pending_once(bot_token: str, limit: int = 10):
    if storage_sqlite is None:
        return 0
    try:
        rows = storage_sqlite.pop_pending_notifications(limit=limit)
    except Exception as e:
        logging.exception('Failed to pop pending notifications: %s', e)
        return 0
    if not rows:
        return 0
    try:
        from slack_sdk import WebClient
    except Exception:
        logging.error('slack_sdk not available')
        return 0
    client = WebClient(token=bot_token)
    sent = 0

    # Slack payload safety caps (conservative)
    MAX_TEXT_LEN = 2800
    MAX_BLOCK_TEXT_LEN = 2800

    def _trim_text(val: str, max_len: int = MAX_TEXT_LEN) -> str:
        if val is None:
            s = ''
        elif isinstance(val, str):
            s = val
        else:
            s = str(val)
        if len(s) <= max_len:
            return s
        return s[: max_len - 1] + '…'

    def _sanitize_blocks_for_slack(blocks_obj):
        """Best-effort trim for Block Kit text lengths to avoid Slack size errors."""
        if not isinstance(blocks_obj, list):
            return blocks_obj
        safe_blocks = copy.deepcopy(blocks_obj)
        for b in safe_blocks:
            if not isinstance(b, dict):
                continue
            # section text
            txt = b.get('text')
            if isinstance(txt, dict) and isinstance(txt.get('text'), str):
                txt['text'] = _trim_text(txt.get('text'), MAX_BLOCK_TEXT_LEN)
            # fields[] text
            fields = b.get('fields')
            if isinstance(fields, list):
                for f in fields:
                    if isinstance(f, dict) and isinstance(f.get('text'), str):
                        f['text'] = _trim_text(f.get('text'), MAX_BLOCK_TEXT_LEN)
            # accessories/options plain_text
            accessory = b.get('accessory')
            if isinstance(accessory, dict):
                for k in ('placeholder', 'text'):
                    part = accessory.get(k)
                    if isinstance(part, dict) and isinstance(part.get('text'), str):
                        part['text'] = _trim_text(part.get('text'), MAX_BLOCK_TEXT_LEN)
                opts = accessory.get('options')
                if isinstance(opts, list):
                    for opt in opts:
                        if isinstance(opt, dict):
                            t = opt.get('text')
                            if isinstance(t, dict) and isinstance(t.get('text'), str):
                                t['text'] = _trim_text(t.get('text'), MAX_BLOCK_TEXT_LEN)
        return safe_blocks

    for r in rows:
        row_id, uid, ch, text, payload = r
        text = _trim_text(text, MAX_TEXT_LEN)
        blocks = None
        # payload may be one of:
        # - JSON list of Block Kit blocks -> use as blocks
        # - JSON dict representing AI payload (with 'message' or 'blocks') -> extract message or blocks
        if payload:
            try:
                import json
                parsed = json.loads(payload)
                if isinstance(parsed, list):
                    blocks = parsed
                elif isinstance(parsed, dict):
                    # prefer explicit 'blocks' key if present
                    if isinstance(parsed.get('blocks'), list):
                        blocks = parsed.get('blocks')
                    # otherwise, if this dict contains a message string, use it as text
                    elif isinstance(parsed.get('message'), str):
                        text = _trim_text(parsed.get('message'), MAX_TEXT_LEN)
                    else:
                        # leave as-is (fallback to stored text)
                        pass
                else:
                    blocks = None
            except Exception:
                blocks = None
        if blocks:
            blocks = _sanitize_blocks_for_slack(blocks)
        text, blocks = slack_notifier.with_channel_mention(text, blocks)
        try:
            if blocks:
                res = client.chat_postMessage(channel=ch, text=(text or ' '), blocks=blocks)
            else:
                res = client.chat_postMessage(channel=ch, text=text)
            ts = None
            try:
                ts = res.get('ts')
            except Exception:
                ts = getattr(res, 'data', {}).get('ts') if getattr(res, 'data', None) else None
            storage_sqlite.mark_notification_sent(row_id, message_ts=ts)
            sent += 1
        except Exception as e:
            err_text = str(e)
            # Retry once with a very short fallback when Slack rejects oversized messages.
            if 'message_limit_exceeded' in err_text or 'msg_too_long' in err_text:
                try:
                    # Use a fixed tiny fallback text (do not reuse original text)
                    # so the retry payload size is guaranteed to be small.
                    short_text = '通知があります。詳細はアプリで確認してください。'
                    short_text, _ = slack_notifier.with_channel_mention(short_text)
                    res = client.chat_postMessage(channel=ch, text=short_text)
                    ts = None
                    try:
                        ts = res.get('ts')
                    except Exception:
                        ts = getattr(res, 'data', {}).get('ts') if getattr(res, 'data', None) else None
                    storage_sqlite.mark_notification_sent(row_id, message_ts=ts)
                    sent += 1
                    logging.warning('Retried row=%s with shortened text due to message_limit_exceeded', row_id)
                    continue
                except Exception:
                    logging.exception('Retry with shortened message failed for row=%s', row_id)

            logging.exception('Failed to send pending notification row=%s', row_id)
            try:
                storage_sqlite.mark_notification_failed(row_id, reason='send_failed')
            except Exception:
                pass
    return sent


def main():
    setup_logging()
    lock_owner = lock_db.make_owner()
    try:
        acquired = lock_db.acquire(LOCK_NAME, lock_owner)
    except Exception:
        logging.exception('failed to acquire DB lock')
        sys.exit(1)
    if not acquired:
        logging.error('socket_server already running (DB lock %s is held)', LOCK_NAME)
        sys.exit(1)
    heartbeat_stop = lock_db.start_heartbeat(LOCK_NAME, lock_owner)

    def _release():
        heartbeat_stop.set()
        lock_db.release(LOCK_NAME, lock_owner)

    bot_token = os.environ.get('SLACK_BOT_TOKEN')
    app_token = os.environ.get('SLACK_APP_TOKEN')

    if not bot_token or not app_token:
        logging.error('SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in environment')
        _release()
        sys.exit(2)

    # create a placeholder session for create_app; handlers will update session fields
    session = send_slack_checkbox.SurveySession(
        user_id='socket_server',
        channel=os.environ.get('SLACK_CHANNEL', ''),
        exercise_type='stretch',
        motion_detected=True,
    )

    app = send_slack_checkbox.create_app(bot_token, app_token, session)

    handler = SocketModeHandler(app, app_token)
    poll_interval = 5
    #DBから通知キューを確認して取り出す秒数をpoll_intervalで指定する。デフォルトは5秒。
    stop_event = threading.Event()

    def poller():
        logging.info('Poller thread started, interval=%s', poll_interval)
        try:
            while not stop_event.is_set():
                try:
                    cnt = _process_pending_once(bot_token, limit=10)
                    if cnt:
                        logging.info('Sent %d queued messages', cnt)
                except Exception:
                    logging.exception('Poller loop error')
                stop_event.wait(poll_interval)
        except Exception:
            logging.info('Poller exiting')

    t = threading.Thread(target=poller, daemon=True)
    t.start()

    # Ensure lock is cleaned up on exit
    try:
        _ensure_release_on_exit(_release)
    except Exception:
        pass

    try:
        logging.info('Starting Socket Mode handler')
        handler.start()
    except KeyboardInterrupt:
        logging.info('Interrupted, shutting down')
    finally:
        # signal poller to stop
        try:
            stop_event.set()
        except Exception:
            pass
        # attempt to stop SocketModeHandler gracefully
        try:
            stop_fn = getattr(handler, 'stop', None)
            if callable(stop_fn):
                stop_fn()
            else:
                # try to disconnect client if available
                client = getattr(handler, 'client', None)
                if client and hasattr(client, 'disconnect'):
                    try:
                        client.disconnect()
                    except Exception:
                        pass
        except Exception:
            logging.exception('Error stopping handler')
        # release lock (atexit also registered, but call explicitly)
        try:
            _release()
        except Exception:
            pass


if __name__ == '__main__':
    main()
