"""slack_notifier.py

Slack 通知専用モジュール。

機能:
 - `load_token_from_slack_test()` : 既存の `slack_test.py` からトークン/チャンネルを抽出（読み取りのみ）
 - `send_slack_message(token, channel, text, do_send=False)` : 実際に送信する関数。デフォルトは dry-run。
 - CLI: `python slack_notifier.py --user USER_ID --text "message" [--send]` でテスト送信。

注意: トークンは機密情報です。ログにそのまま出力しないでください（ここではテスト目的で短いマスクを表示します）。
"""

import os
import re
import logging
import logging.handlers
import time
import copy
from typing import Optional, Tuple

from env_loader import load_env_file

load_env_file()
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
# Channel is looked up from DB by main; keep optional module constant for manual CLI use if needed.
SLACK_CHANNEL_ID = os.environ.get('SLACK_CHANNEL_ID')  # optional

logging.basicConfig(level=logging.INFO)

CHANNEL_MENTION = '<!channel>'

# If DEBUG_SLACK is set, write detailed slack/http/websocket logs to a rotating file
if os.environ.get('DEBUG_SLACK') in ('1', 'true', 'True'):
    try:
        os.makedirs('logs', exist_ok=True)
        dbg_path = os.path.join('logs', 'slack_debug.log')
        dbg_handler = logging.handlers.RotatingFileHandler(dbg_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
        dbg_handler.setLevel(logging.DEBUG)
        dbg_handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s'))
        for ln in ('slack_bolt', 'slack_sdk', 'urllib3', 'websockets', 'websocket', 'httpx', 'httpcore'):
            lg = logging.getLogger(ln)
            lg.setLevel(logging.DEBUG)
            lg.addHandler(dbg_handler)
        logging.info('DEBUG_SLACK enabled: detailed slack/http logs -> %s', dbg_path)
    except Exception:
        logging.exception('Failed to initialize DEBUG_SLACK logging')


def _mask_token(tok: str) -> str:
    if not tok:
        return ''
    if len(tok) <= 8:
        return '*' * len(tok)
    return tok[:4] + '...' + tok[-4:]


def get_slack_config(path: str = None) -> Optional[Tuple[str, str]]:
    """Return (token, channel) from slack_notifier configuration.

        Sources (in priority order):
            1. Environment variable `SLACK_BOT_TOKEN` (overrides module constant)
            2. Module-level constant `SLACK_BOT_TOKEN` defined above

        This function does NOT read `slack_test.py`. Channel lookup is expected to be done by the caller
        (for example, `main.py` reads channel from `tokens_db`).
    """
    token = SLACK_BOT_TOKEN
    # channel intentionally not provided here; caller should fetch the channel from tokens_db
    if token:
        logging.info('Loaded slack token from slack_notifier: token=%s', _mask_token(token))
        return token, None
    logging.warning('Slack bot token not configured in slack_notifier (env or constants).')
    return None


def with_channel_mention(text: str, blocks: list = None) -> Tuple[str, list]:
    """Add a Slack @channel mention to fallback and visible Block Kit text."""
    message_text = text if isinstance(text, str) else str(text or '')
    if not message_text.startswith(CHANNEL_MENTION):
        message_text = f'{CHANNEL_MENTION}\n{message_text}'.rstrip()

    if not isinstance(blocks, list):
        return message_text, blocks

    mentioned_blocks = copy.deepcopy(blocks)
    for block in mentioned_blocks:
        block_text = block.get('text') if isinstance(block, dict) else None
        if (
            isinstance(block_text, dict)
            and block_text.get('type') == 'mrkdwn'
            and isinstance(block_text.get('text'), str)
        ):
            if not block_text['text'].startswith(CHANNEL_MENTION):
                block_text['text'] = f"{CHANNEL_MENTION}\n{block_text['text']}"
            break
    return message_text, mentioned_blocks


def send_slack_message(token: str, channel: str, text: str, blocks: list = None, do_send: bool = False, return_ts: bool = False):
    """Send message to Slack. If do_send is False, only logs the action (dry-run).
    Returns (success_bool, ts_or_None) when sending; in dry-run returns (True, None).
    If `return_ts` is False callers may treat the first element as boolean compatibility.
    """
    # Avoid attempting to send when channel is not provided
    if not channel:
        # Mask token and truncate text to avoid leaking long content into logs
        try:
            t_preview = (text[:120] + '...') if isinstance(text, str) and len(text) > 120 else text
        except Exception:
            t_preview = None
        logging.warning('No Slack channel provided; skipping send. token=%s text_preview=%s', _mask_token(token), t_preview)
        return False

    logging.info('Preparing Slack message to channel=%s token=%s', channel, _mask_token(token))
    if not do_send:
        logging.info('Dry-run mode: message not sent. text=%s', text)
        return (True, None) if return_ts else True
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except Exception as e:
        logging.exception('slack_sdk import failed: %s', e)
        return (False, None) if return_ts else False

    text, blocks = with_channel_mention(text, blocks)
    client = WebClient(token=token)
    # Prepare message payload
    m = re.search(r"(https?://\S+)", text)
    local_blocks = blocks
    post_text = text
    if m and not local_blocks:
        url = m.group(1)
        if any(x in url for x in ('.png', '.jpg', '.jpeg', 'drive.google.com', 'drive.usercontent.google.com', 'googleusercontent.com')):
            post_text = text.replace(url, '').strip()
            local_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": post_text or " "}},
                {"type": "image", "image_url": url, "alt_text": "image"},
            ]

    max_retries = 4
    for attempt in range(1, max_retries + 1):
        try:
            if local_blocks:
                res = client.chat_postMessage(channel=channel, text=post_text or text, blocks=local_blocks)
            else:
                res = client.chat_postMessage(channel=channel, text=text)
            ts = None
            try:
                ts = res.get('ts')
            except Exception:
                try:
                    ts = res['ts']
                except Exception:
                    ts = getattr(res, 'data', {}).get('ts') if getattr(res, 'data', None) else None
            logging.info('Slack message sent: ts=%s', ts)
            return (True, ts) if return_ts else True
        except SlackApiError as sae:
            # Handle rate limit explicitly
            try:
                status = int(getattr(sae.response, 'status_code', 0) or 0)
            except Exception:
                status = 0
            # Determine error code for retry decisions
            err_code = ''
            try:
                err_code = (sae.response or {}).get('error', '') or ''
            except Exception:
                pass
            if status == 429 or err_code == 'message_limit_exceeded':
                # respect Retry-After header when present; treat message_limit_exceeded as rate limit
                try:
                    retry_after = int(sae.response.headers.get('Retry-After', '2'))
                except Exception:
                    retry_after = 2
                sleep_for = max(2, retry_after)
                logging.warning('Slack rate limited (%s / %s), sleeping %s seconds (attempt %s/%s)', status, err_code, sleep_for, attempt, max_retries)
                time.sleep(sleep_for)
                continue
            # For other SlackApiError, don't retry many times; log and fail
            logging.exception('SlackApiError while sending message: %s', sae)
            return (False, None) if return_ts else False
        except Exception as e:
            # Network or other transient issues: retry with backoff up to max_retries
            logging.exception('Transient error sending Slack message (attempt %s/%s): %s', attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            return (False, None) if return_ts else False


if __name__ == '__main__':
    # Simple CLI for testing
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument('--user', '-u', help='user id to pick channel (not used by default)')
    p.add_argument('--token', help='Slack bot token (optional)')
    p.add_argument('--channel', help='Channel id (optional)')
    p.add_argument('--text', '-t', required=True, help='Message text')
    p.add_argument('--send', action='store_true', help='Actually send (default is dry-run)')
    args = p.parse_args()

    cfg = None
    if args.token and args.channel:
        cfg = (args.token, args.channel)
    else:
        cfg = get_slack_config()

    if not cfg:
        logging.error('No slack token/channel available. Provide --token and --channel or set SLACK_BOT_TOKEN/SLACK_CHANNEL_ID')
        raise SystemExit(2)

    ok = send_slack_message(cfg[0], cfg[1], args.text, do_send=args.send)
    if ok:
        logging.info('Done')
    else:
        logging.error('Send failed')
