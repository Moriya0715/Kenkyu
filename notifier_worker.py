"""notifier_worker.py

Poll notifications table and send Slack messages.
Options:
  --loop       : run forever poll
  --once       : run single iteration
  --send       : actually send; default is dry-run (log only)
"""
import argparse
import time
import logging
from datetime import datetime

import storage_sqlite
import slack_notifier

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')


# Slack API hard caps (conservative)
_MAX_TEXT = 3000
_MAX_BLOCK_TEXT = 2800


def _trim(val, max_len: int = _MAX_TEXT) -> str:
    if val is None:
        return ''
    s = val if isinstance(val, str) else str(val)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + '…'


def _trim_blocks(blocks):
    """Return a copy of blocks with all text values trimmed to _MAX_BLOCK_TEXT."""
    import copy
    if not isinstance(blocks, list):
        return blocks
    safe = copy.deepcopy(blocks)
    for b in safe:
        if not isinstance(b, dict):
            continue
        txt = b.get('text')
        if isinstance(txt, dict) and isinstance(txt.get('text'), str):
            txt['text'] = _trim(txt['text'], _MAX_BLOCK_TEXT)
        for f in (b.get('fields') or []):
            if isinstance(f, dict) and isinstance(f.get('text'), str):
                f['text'] = _trim(f['text'], _MAX_BLOCK_TEXT)
        acc = b.get('accessory')
        if isinstance(acc, dict):
            for k in ('placeholder', 'text'):
                part = acc.get(k)
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    part['text'] = _trim(part['text'], _MAX_BLOCK_TEXT)
    return safe


def process_batch(limit: int = 10, do_send: bool = False):
    rows = storage_sqlite.pop_pending_notifications(limit=limit)
    if not rows:
        return 0
    count = 0
    for r in rows:
        nid, user_id, channel, text, payload = r
        text = _trim(text)
        last_attempt = datetime.now().astimezone().isoformat()
        # Defensive: if channel is not set, mark as failed and skip API call
        if not channel:
            logging.warning('Notification %s has no channel; marking failed', nid)
            _mark_failed(nid, last_attempt)
            count += 1
            continue

        if do_send:
            # attempt send
            token_cfg = slack_notifier.get_slack_config()
            token = token_cfg[0] if token_cfg else None
            if not token:
                logging.warning('Slack token not configured; cannot send notification %s', nid)
                # mark failed
                _mark_failed(nid, last_attempt)
                continue
            # Parse payload defensively and only accept BlockKit list-of-dicts
            blocks = None
            if payload:
                parsed = __parse_payload(payload)
                try:
                    if isinstance(parsed, list) and all(isinstance(b, dict) and 'type' in b for b in parsed):
                        blocks = parsed
                except Exception:
                    blocks = None

            if blocks is not None:
                blocks = _trim_blocks(blocks)

            # attempt send; on failure due to invalid blocks, retry once without blocks
            res = slack_notifier.send_slack_message(token, channel, text, blocks=blocks, do_send=True, return_ts=True)
            ok = False
            ts = None
            if not (isinstance(res, tuple) and res[0]):
                # First attempt failed; log and mark as failed.
                logging.warning('Send failed for %s (blocks=%s); marking failed.', nid, blocks is not None)
            # res may be (bool, ts) or a bare bool; normalize
            if isinstance(res, tuple):
                ok, ts = res
            else:
                ok = bool(res)
            if ok:
                try:
                    # persist message_ts if available
                    import storage_sqlite as _ss
                    _ss.mark_notification_sent(nid, message_ts=ts)
                except Exception:
                    # fallback to simpler update if storage call fails
                    _mark_sent(nid, last_attempt)
            else:
                _mark_failed(nid, last_attempt)
        else:
            # Sanitize text for console logging to avoid Windows cp932 encode errors
            safe_text = ''
            try:
                if text is not None:
                    safe_text = text.encode('utf-8', 'backslashreplace').decode('ascii')
            except Exception:
                try:
                    safe_text = repr(text)
                except Exception:
                    safe_text = ''
            logging.info('Dry-run: would send to %s: %s', channel, safe_text)
            _mark_sent(nid, last_attempt, dry_run=True)
        count += 1
    return count


def _mark_sent(nid: int, at_iso: str, dry_run: bool = False):
    conn = __get_conn()
    try:
        cur = conn.cursor()
        status = 'sent' if not dry_run else 'dry'
        cur.execute('UPDATE notifications SET status=?, attempts=attempts+1, last_attempt=? WHERE id=?', (status, at_iso, nid))
        conn.commit()
    finally:
        conn.close()


def _mark_failed(nid: int, at_iso: str):
    conn = __get_conn()
    try:
        cur = conn.cursor()
        cur.execute('UPDATE notifications SET status=?, attempts=attempts+1, last_attempt=? WHERE id=?', ('failed', at_iso, nid))
        conn.commit()
    finally:
        conn.close()


def __get_conn():
    # Prefer the storage_sqlite connection helper so the notifier uses the
    # same backend (Postgres when configured) as the rest of the app.
    try:
        from storage_sqlite import _get_conn, DB_PATH
        return _get_conn(DB_PATH)
    except Exception as e:
        # Do not fallback to sqlite; require Postgres backend to be configured.
        logging.exception('Failed to obtain DB connection for notifier: %s', e)
        raise RuntimeError('Postgres connection required for notifier') from e


def __parse_payload(p: str):
    try:
        import json
        return json.loads(p)
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--loop', action='store_true')
    p.add_argument('--once', action='store_true')
    p.add_argument('--send', action='store_true', help='actually send to Slack; otherwise dry-run')
    args = p.parse_args()

    storage_sqlite.ensure_schema()

    if args.once:
        n = process_batch(do_send=args.send)
        logging.info('Processed %d notifications', n)
        return

    if args.loop:
        logging.info('Notifier loop started (dry-run=%s)', not args.send)
        try:
            while True:
                n = process_batch(do_send=args.send)
                if n == 0:
                    time.sleep(5)
        except KeyboardInterrupt:
            logging.info('Notifier loop stopped')


if __name__ == '__main__':
    main()
