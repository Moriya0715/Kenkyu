from slack_bolt.adapter.socket_mode import SocketModeHandler
from datetime import datetime
import storage_sqlite

""""
print(datetime.now())
print(datetime.now().astimezone())
print(datetime.now().astimezone().tzinfo)
print(datetime.now().astimezone().isoformat())

WEEK_START_WEEKDAY = 6  # Sunday
print(datetime.now().astimezone().weekday())
print((datetime.now().astimezone().weekday() - WEEK_START_WEEKDAY) % 7)
"""

payload = storage_sqlite.build_exercise_ai_input(
    user_id="seiichirou019@gmail.com",
    exercise_type="cardio",
    exercise_name = None,
    event = None,
)
print(payload)