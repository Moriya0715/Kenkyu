from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from env_loader import load_env_file

load_env_file()
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')

if not SLACK_BOT_TOKEN:
    raise RuntimeError('SLACK_BOT_TOKEN が設定されていません')

client = WebClient(token=SLACK_BOT_TOKEN)

try:
    response = client.chat_postMessage(
        channel="C0B8HR83VK3",  # チャンネルID
        text="レートリミット確認テスト"
    )

    print("送信成功")
    print(response["ts"])

except SlackApiError as e:
    print(f"Status Code: {e.response.status_code}")
    print(f"Error: {e.response['error']}")

    if e.response.status_code == 429:
        retry_after = e.response.headers.get("Retry-After")
        print(f"レートリミット発生")
        print(f"{retry_after}秒後に再試行してください")

    print("Headers:")
    print(e.response.headers)