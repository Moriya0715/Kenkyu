from slack_bolt.adapter.socket_mode import SocketModeHandler
from datetime import datetime
import storage_sqlite
import os


""""
print(datetime.now())
print(datetime.now().astimezone())
print(datetime.now().astimezone().tzinfo)
print(datetime.now().astimezone().isoformat())

WEEK_START_WEEKDAY = 6  # Sunday
print(datetime.now().astimezone().weekday())
print((datetime.now().astimezone().weekday() - WEEK_START_WEEKDAY) % 7)
"""

# payload = storage_sqlite.build_exercise_ai_input(
#     user_id="seiichirou019@gmail.com",
#     exercise_type="cardio",
#     exercise_name = None,
#     event = None,
# )
# print(payload)

# print(os.path.dirname(os.path.abspath(__file__)))
# print(os.path.abspath(__file__))
# print(os.path.dirname(__file__))

import use_openai
use_openai._load_project_env()
print(os.environ.get('OPENAI_NOTIFICATION_MODEL'))