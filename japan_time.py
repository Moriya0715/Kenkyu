from datetime import datetime, timedelta, timezone
from time import struct_time


JST = timezone(timedelta(hours=9), name="JST")


def now_jst() -> datetime:
    return datetime.now(JST)


def as_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


def jst_log_time(timestamp: float) -> struct_time:
    return datetime.fromtimestamp(timestamp, JST).timetuple()
