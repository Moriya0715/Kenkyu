import argparse
import logging
import os
import requests
import tokens_db
from env_loader import load_env_file

load_env_file()

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _HAS_SLACK_SDK = True
except Exception:
    _HAS_SLACK_SDK = False

SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')


def get_channel_for_user(user_id: str):
  # tokens_db.get_channel_for_user returns channel or None
  return tokens_db.get_channel_for_user(user_id)


def send_message(channel: str, text: str) -> bool:
    """Send message immediately to Slack (no dry-run).

    Minimal and direct: always performs the API call using the hardcoded token.
    """
    logging.info('Sending Slack message to channel=%s', channel)

    if not SLACK_BOT_TOKEN:
        print('SLACK_BOT_TOKEN が設定されていません')
        return False

    if _HAS_SLACK_SDK:
        client = WebClient(token=SLACK_BOT_TOKEN)
        try:
            res = client.chat_postMessage(channel=channel, text=text)
            print('メッセージ送信成功 ts=', res.get('ts'))
            return True
        except SlackApiError as e:
            print('Slack API エラー:', e.response.get('error'))
            return False

    # fallback: simple REST call
    headers = {'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
    try:
        r = requests.post('https://slack.com/api/chat.postMessage', headers=headers, json={'channel': channel, 'text': text}, timeout=10)
        j = r.json()
        if not j.get('ok'):
            print('Slack API returned error:', j.get('error'))
            return False
        print('メッセージ送信成功 ts=', j.get('ts'))
        return True
    except Exception as e:
        print('送信失敗:', e)
        return False


def main():
        p = argparse.ArgumentParser()
        p.add_argument('--user', '-u', default='seiichirou019@gmail.com', help='user_id to lookup in tokens.db')
        p.add_argument('--text', '-t', default=None, help='override message text')
        args = p.parse_args()

        channel = get_channel_for_user(args.user)
        if not channel:
            print('Channel not found in tokens.db for user=', args.user)
            return

        text = args.text or 'テスト通知: Slack への送信確認'
        ok = send_message(channel, text)
        if not ok:
            print('送信が失敗しました')


if __name__ == '__main__':
  logging.basicConfig(level=logging.INFO)
  main()
