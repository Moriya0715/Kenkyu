"""fitbit_sleep.py

Helper to query Fitbit Sleep API and determine whether a user is currently sleeping.

Functions:
 - is_user_sleeping(user_id, now, ttl_seconds=300) -> bool

This module caches recent API responses per-user for `ttl_seconds` to avoid
excessive API calls. On error, it returns False (do not suppress notifications).
"""
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List
import logging

import os
import json
import storage_sqlite
import fitbit_sampler

_CACHE = {}  # user_id -> (fetched_at_ts, sessions_list)
_DEFAULT_TTL = 300


def _parse_iso(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        # Python 3.7+: fromisoformat handles timezone offsets
        return datetime.fromisoformat(dt_str)
    except Exception:
        try:
            # fallback: try rfc parsing without timezone
            return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S.%f')
        except Exception:
            try:
                return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S')
            except Exception:
                return None


def _fetch_sleep_for_date(user_id: str, date_str: str) -> list[dict]:
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime.fromisoformat(date_str).replace(tzinfo=local_tz)
    return fitbit_sampler.fetch_sleep_sessions(user_id, start, start + timedelta(days=1))


def _get_cached_sessions(user_id: str, now_ts: float, ttl_seconds: int) -> List[dict]:
    entry = _CACHE.get(user_id)
    if entry and (now_ts - entry[0]) <= ttl_seconds:
        return entry[1]
    return []


def _set_cache(user_id: str, sessions: List[dict]):
    _CACHE[user_id] = (time.time(), sessions)


def is_user_sleeping(user_id: str, now: datetime, ttl_seconds: int = _DEFAULT_TTL) -> bool:
    """Return True if `now` is within any sleep session for the user.

    On API or token error, returns False (do not suppress notifications).
    """
    now_ts = time.time()
    try:
        # Use cache if available
        sessions = _get_cached_sessions(user_id, now_ts, ttl_seconds)
        if not sessions:
            # fetch today's and yesterday's sessions to cover overnight
            dates = [now.date(), (now.date() - timedelta(days=1))]
            all_sessions = []
            for d in dates:
                try:
                    s = _fetch_sleep_for_date(user_id, d.isoformat())
                    all_sessions.extend(s)
                except Exception:
                    logging.exception('fitbit_sleep: failed to fetch sleep for %s on %s', user_id, d)
                    # continue to try other dates
                    continue
            sessions = all_sessions
            _set_cache(user_id, sessions)

        # now check sessions
        for s in sessions:
            start_s = s.get('startTime') or s.get('start') or s.get('startTimeLocal')
            end_s = s.get('endTime') or s.get('end') or s.get('endTimeLocal')
            start_dt = _parse_iso(start_s)
            end_dt = _parse_iso(end_s) if end_s else None
            if start_dt is None:
                continue
            # ensure timezone-aware: assume local tz if naive
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=now.tzinfo or timezone.utc)
            if end_dt and end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=now.tzinfo or timezone.utc)
            if start_dt <= now and (end_dt is None or now < end_dt):
                # Persist sessions around the found one for history
                try:
                    _save_sessions_to_disk(user_id, sessions, datetime.now().astimezone())
                except Exception:
                    logging.exception('Failed to save sleep sessions for %s', user_id)
                return True
        return False
    except Exception:
        logging.exception('fitbit_sleep: unexpected error for user=%s', user_id)
        return False


def _ensure_sleep_dir(user_id: str) -> str:
    safe = storage_sqlite._sanitize_user_id(user_id)
    user_base = os.path.join('data_output', safe)
    sleep_dir = os.path.join(user_base, 'sleep')
    os.makedirs(sleep_dir, exist_ok=True)
    return sleep_dir


def _sleep_file_path(user_id: str, day: date) -> str:
    safe = storage_sqlite._sanitize_user_id(user_id)
    return os.path.join('data_output', safe, 'sleep', f'sleep_{day.isoformat()}.json')


def _refresh_marker_path(user_id: str, day: date) -> str:
    sleep_dir = _ensure_sleep_dir(user_id)
    return os.path.join(sleep_dir, f'.sleep_refresh_{day.isoformat()}.json')


def _was_refresh_attempted_today(user_id: str, now: datetime) -> bool:
    try:
        return os.path.exists(_refresh_marker_path(user_id, now.date()))
    except Exception:
        return False


def _mark_refresh_attempt_today(user_id: str, now: datetime):
    marker = _refresh_marker_path(user_id, now.date())
    payload = {
        'date': now.date().isoformat(),
        'attempted_at': now.isoformat(),
        'source': 'scheduled_sleep_refresh',
    }
    try:
        with open(marker, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception('Failed to write sleep refresh marker for %s', user_id)


def _save_sessions_to_disk(
    user_id: str,
    sessions: List[dict],
    fetched_at: datetime,
    date_tag: Optional[str] = None,
):
    """Save sessions to `data_output/<safe_user>/sleep/sleep_YYYY-MM-DD.json`.

    Overwrites the per-day file selected by `date_tag`, or `fetched_at` by default.
    """
    if not sessions:
        # still create an empty structure for that date
        sessions_out = []
    else:
        sessions_out = []
        for s in sessions:
            start = s.get('start') or s.get('startTime') or s.get('startTimeLocal')
            end = s.get('end') or s.get('endTime') or s.get('endTimeLocal')
            sd = _parse_iso(start) if start else None
            ed = _parse_iso(end) if end else None
            duration = None
            if sd and ed:
                try:
                    duration = int((ed - sd).total_seconds() / 60)
                except Exception:
                    duration = None
            raw = s.get('raw') if isinstance(s.get('raw'), dict) else s
            sessions_out.append({'start': start, 'end': end, 'duration_min': duration, 'raw': raw})

    date_tag = date_tag or fetched_at.strftime('%Y-%m-%d')
    out = {
        'date': date_tag,
        'fetched_at': fetched_at.isoformat(),
        'source': 'google_health',
        'sessions': sessions_out
    }
    sleep_dir = _ensure_sleep_dir(user_id)
    fname = os.path.join(sleep_dir, f'sleep_{date_tag}.json')

    def _atomic_write_json(dst_path: str):
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix='tmp-sleep-', dir=os.path.dirname(dst_path), text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                json.dump(out, tf, ensure_ascii=False, indent=2)
            os.replace(tmp, dst_path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    # atomic write
    try:
        _atomic_write_json(fname)
    except Exception:
        logging.exception('Failed writing sleep snapshots for %s', user_id)


def _fetch_sleep_for_window(user_id: str, start_date, end_date, tz) -> list[dict]:
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    exclusive_end = end_date + timedelta(days=1)
    end = datetime(exclusive_end.year, exclusive_end.month, exclusive_end.day, tzinfo=tz)
    return fitbit_sampler.fetch_sleep_sessions(user_id, start, end)


def _group_sessions_by_end_date(sessions: List[dict], start_date, end_date, tz) -> dict:
    grouped = {}
    day = start_date
    while day <= end_date:
        grouped[day.isoformat()] = []
        day += timedelta(days=1)

    for session in sessions:
        end_value = session.get('end') or session.get('endTime') or session.get('endTimeLocal')
        end_dt = _parse_iso(end_value) if end_value else None
        if end_dt is None:
            continue
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=tz)
        else:
            end_dt = end_dt.astimezone(tz)
        day_key = end_dt.date().isoformat()
        if day_key in grouped:
            grouped[day_key].append(session)
    return grouped


def _saved_file_has_sessions(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return bool((json.load(f) or {}).get('sessions'))
    except Exception:
        logging.exception('Failed reading existing sleep file %s', path)
        return True


def refresh_recent_sleep_history_if_due(
    user_id: str,
    now: datetime,
    max_days: int = 14,
    refresh_hour: int = 12,
) -> bool:
    """Refresh the rolling sleep history once daily at or after the scheduled hour."""
    if now.hour < refresh_hour or _was_refresh_attempted_today(user_id, now):
        return False

    _mark_refresh_attempt_today(user_id, now)
    end_date = now.date()
    start_date = end_date - timedelta(days=max_days - 1)
    tz = now.tzinfo or timezone.utc

    try:
        sessions = _fetch_sleep_for_window(user_id, start_date, end_date, tz)
        sessions_by_date = _group_sessions_by_end_date(sessions, start_date, end_date, tz)
    except Exception:
        logging.exception('Scheduled sleep history fetch failed for %s', user_id)
        return True

    saved_count = 0
    for date_str, daily_sessions in sessions_by_date.items():
        day = date.fromisoformat(date_str)
        path = _sleep_file_path(user_id, day)
        if not daily_sessions and _saved_file_has_sessions(path):
            continue
        _save_sessions_to_disk(user_id, daily_sessions, now, date_tag=date_str)
        saved_count += len(daily_sessions)

    logging.info(
        'Scheduled sleep history refresh completed user=%s range=%s..%s sessions=%s',
        user_id,
        start_date,
        end_date,
        saved_count,
    )
    return True


def save_sessions(user_id: str, sessions: List[dict], fetched_at: Optional[datetime] = None):
    """Public helper to persist sessions. If fetched_at omitted, uses now()."""
    if fetched_at is None:
        fetched_at = datetime.now().astimezone()
    return _save_sessions_to_disk(user_id, sessions, fetched_at)


def get_previous_sleep_start(user_id: str, days_back: int = 1) -> Optional[datetime]:
    """Return the start datetime of the main sleep session `days_back` days before today.

    Reads the per-day saved JSON under `data_output/<safe_user>/sleep/sleep_YYYY-MM-DD.json`.
    Returns None if no file or no session found.
    """
    try:
        safe = storage_sqlite._sanitize_user_id(user_id)
        target_date = (datetime.now().date() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        sleep_dir = os.path.join('data_output', safe, 'sleep')
        fname = os.path.join(sleep_dir, f'sleep_{target_date}.json')
        if not os.path.exists(fname):
            return None
        with open(fname, 'r', encoding='utf-8') as f:
            obj = json.load(f)
        sessions = obj.get('sessions') or []
        if not sessions:
            return None
        # prefer session with raw.isMainSleep true
        chosen = None
        for s in sessions:
            raw = s.get('raw') or {}
            if raw.get('isMainSleep'):
                chosen = s
                break
        if chosen is None:
            chosen = sessions[0]
        start = chosen.get('start') or (chosen.get('raw') or {}).get('startTime')
        if not start:
            return None
        dt = _parse_iso(start)
        return dt
    except Exception:
        logging.exception('get_previous_sleep_start failed for %s', user_id)
        return None


def get_recent_sleep_starts(user_id: str, max_items: int = 3) -> List[datetime]:
    """Return a list of available sleep start datetimes from saved files, newest first.

    Scans `data_output/<safe_user>/sleep/` for `sleep_YYYY-MM-DD.json` files and extracts
    the preferred session start (main sleep if present). Returns at most `max_items` datetimes.
    """
    starts = []
    try:
        safe = storage_sqlite._sanitize_user_id(user_id)
        sleep_dir = os.path.join('data_output', safe, 'sleep')
        if not os.path.isdir(sleep_dir):
            return []
        files = []
        for fn in os.listdir(sleep_dir):
            if fn.startswith('sleep_') and fn.endswith('.json'):
                files.append(fn)
        # sort by filename (date) descending
        files.sort(reverse=True)
        for fn in files:
            if len(starts) >= max_items:
                break
            path = os.path.join(sleep_dir, fn)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                sessions = obj.get('sessions') or []
                if not sessions:
                    continue
                chosen = None
                for s in sessions:
                    raw = s.get('raw') or {}
                    if raw.get('isMainSleep'):
                        chosen = s
                        break
                if chosen is None:
                    chosen = sessions[0]
                start = chosen.get('start') or (chosen.get('raw') or {}).get('startTime')
                if not start:
                    continue
                dt = _parse_iso(start)
                if dt is not None:
                    starts.append(dt)
            except Exception:
                logging.exception('Failed reading sleep file %s', path)
                continue
    except Exception:
        logging.exception('get_recent_sleep_starts top-level failure for %s', user_id)
    return starts


def get_average_of_starts(starts: List[datetime]) -> Optional[datetime]:
    """Return average datetime of the provided datetimes (or None).

    Converts to POSIX timestamps, averages, and returns timezone-aware datetime.
    """
    if not starts:
        return None
    try:
        # Convert all to timestamps (seconds since epoch). Ensure timezone-aware.
        ts = []
        for d in starts:
            if d.tzinfo is None:
                # assume local tz
                d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
            ts.append(d.timestamp())
        avg = sum(ts) / len(ts)
        return datetime.fromtimestamp(avg, tz=datetime.now().astimezone().tzinfo)
    except Exception:
        logging.exception('get_average_of_starts failed')
        return None


def get_effective_sleep_start(user_id: str, max_recent: int = 3, fallback_hour: int = 21):
    """Return (datetime, source) to use for trigger computation.

    Returns a tuple (dt, source) where source is one of 'prev_day', 'average', 'fallback'.
    """
    try:
        # 1) previous day
        prev = get_previous_sleep_start(user_id, days_back=1)
        if prev is not None:
            return prev, 'prev_day'
        # 2) average of recent starts
        recent = get_recent_sleep_starts(user_id, max_items=max_recent)
        if recent:
            avg = get_average_of_starts(recent[:max_recent])
            if avg is not None:
                return avg, 'average'
        # 3) try to detect deep-night null (device non-wear / missing samples) start as heuristic
        try:
            null_dt = _find_night_null_start(user_id)
            if null_dt is not None:
                return null_dt, 'night_null'
        except Exception:
            logging.exception('night null detection failed for %s', user_id)
        # 3) fallback to today's fallback_hour
        now = datetime.now().astimezone()
        today = now.date()
        fallback_dt = datetime(year=today.year, month=today.month, day=today.day, hour=fallback_hour, minute=0, second=0, tzinfo=now.tzinfo)
        return fallback_dt, 'fallback'
    except Exception:
        logging.exception('get_effective_sleep_start failed for %s', user_id)
        return None, 'error'


def _find_night_null_start(user_id: str, night_start_hour: int = 20, night_end_hour: int = 6, min_block_min: int = 30) -> Optional[datetime]:
    """Scan per-minute files for contiguous null heart_rate blocks occurring in the night window.

    Returns the start datetime of a qualifying null block (or None).
    """
    try:
        safe = storage_sqlite._sanitize_user_id(user_id)
        now = datetime.now().astimezone()
        dates = [now.date() - timedelta(days=1), now.date() - timedelta(days=2)]
        tz = now.tzinfo
        candidates = []
        for d in dates:
            path = storage_sqlite._day_file_path(user_id, datetime(d.year, d.month, d.day))
            if not os.path.exists(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                permin = obj.get('PerMinute') or []
                # iterate and find null blocks
                in_null = False
                block_start = None
                block_end = None
                for e in permin:
                    t = e.get('time')
                    hr = e.get('heart_rate') if 'heart_rate' in e else None
                    if not t:
                        continue
                    try:
                        hh, mm, ss = [int(x) for x in t.split(':')]
                        ts = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=tz)
                    except Exception:
                        # try ISO
                        try:
                            ts = _parse_iso(t)
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=tz)
                        except Exception:
                            continue
                    is_null = (hr is None)
                    if is_null and not in_null:
                        in_null = True
                        block_start = ts
                        block_end = ts
                    elif is_null and in_null:
                        block_end = ts
                    elif not is_null and in_null:
                        # close block
                        duration_min = (block_end - block_start).total_seconds() / 60.0 if block_start and block_end else 0
                        if duration_min >= min_block_min:
                            # check if block_start falls into night window (allow crossing midnight)
                            if (block_start.hour >= night_start_hour) or (block_start.hour <= night_end_hour):
                                candidates.append(block_start)
                        in_null = False
                        block_start = None
                        block_end = None
                # if file ended while in null block, consider it
                if in_null and block_start and block_end:
                    duration_min = (block_end - block_start).total_seconds() / 60.0
                    if duration_min >= min_block_min:
                        if (block_start.hour >= night_start_hour) or (block_start.hour <= night_end_hour):
                            candidates.append(block_start)
            except Exception:
                logging.exception('Error scanning per-minute file %s', path)
                continue
        # prefer the candidate closest to midnight / latest (i.e., the one most likely preceding wake)
        if not candidates:
            return None
        # choose the most recent candidate
        candidates.sort(reverse=True)
        return candidates[0]
    except Exception:
        logging.exception('_find_night_null_start unexpected failure for %s', user_id)
        return None


def get_previous_sleep_end(user_id: str, days_back: int = 1) -> Optional[datetime]:
    """Return the end datetime of the main sleep session `days_back` days before today.

    Reads the per-day saved JSON under `data_output/<safe_user>/sleep/sleep_YYYY-MM-DD.json`.
    Returns None if no file or no session found.
    """
    try:
        safe = storage_sqlite._sanitize_user_id(user_id)
        target_date = (datetime.now().date() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        sleep_dir = os.path.join('data_output', safe, 'sleep')
        fname = os.path.join(sleep_dir, f'sleep_{target_date}.json')
        if not os.path.exists(fname):
            return None
        with open(fname, 'r', encoding='utf-8') as f:
            obj = json.load(f)
        sessions = obj.get('sessions') or []
        if not sessions:
            return None
        # prefer session with raw.isMainSleep true
        chosen = None
        for s in sessions:
            raw = s.get('raw') or {}
            if raw.get('isMainSleep'):
                chosen = s
                break
        if chosen is None:
            chosen = sessions[0]
        end = chosen.get('end') or (chosen.get('raw') or {}).get('endTime')
        if not end:
            return None
        dt = _parse_iso(end)
        return dt
    except Exception:
        logging.exception('get_previous_sleep_end failed for %s', user_id)
        return None


def get_recent_sleep_ends(user_id: str, max_items: int = 3) -> List[datetime]:
    """Return a list of available sleep end datetimes from saved files, newest first."""
    ends = []
    try:
        safe = storage_sqlite._sanitize_user_id(user_id)
        sleep_dir = os.path.join('data_output', safe, 'sleep')
        if not os.path.isdir(sleep_dir):
            return []
        files = [fn for fn in os.listdir(sleep_dir) if fn.startswith('sleep_') and fn.endswith('.json')]
        files.sort(reverse=True)
        for fn in files:
            if len(ends) >= max_items:
                break
            path = os.path.join(sleep_dir, fn)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                sessions = obj.get('sessions') or []
                if not sessions:
                    continue
                chosen = None
                for s in sessions:
                    raw = s.get('raw') or {}
                    if raw.get('isMainSleep'):
                        chosen = s
                        break
                if chosen is None:
                    chosen = sessions[0]
                end = chosen.get('end') or (chosen.get('raw') or {}).get('endTime')
                if not end:
                    continue
                dt = _parse_iso(end)
                if dt is not None:
                    ends.append(dt)
            except Exception:
                logging.exception('Failed reading sleep file %s', path)
                continue
    except Exception:
        logging.exception('get_recent_sleep_ends top-level failure for %s', user_id)
    return ends


def get_average_of_ends(ends: List[datetime]) -> Optional[datetime]:
    if not ends:
        return None
    try:
        ts = []
        for d in ends:
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
            ts.append(d.timestamp())
        avg = sum(ts) / len(ts)
        return datetime.fromtimestamp(avg, tz=datetime.now().astimezone().tzinfo)
    except Exception:
        logging.exception('get_average_of_ends failed')
        return None


def _time_to_night_axis_minutes(dt: datetime) -> int:
    """Map time-of-day into a night-centric axis where early morning is next day.

    00:00-11:59 are shifted by +24h so midnight-crossing windows become contiguous.
    """
    minutes = dt.hour * 60 + dt.minute
    if minutes < 12 * 60:
        minutes += 24 * 60
    return minutes


def _median(values: List[int]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _get_sleep_times_for_window(user_id: str, now: datetime, max_days: int):
    starts = []
    ends = []
    for offset in range(max_days):
        day = now.date() - timedelta(days=offset)
        path = _sleep_file_path(user_id, day)
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                sessions = (json.load(f) or {}).get('sessions') or []
            if not sessions:
                continue
            chosen = next(
                (session for session in sessions if (session.get('raw') or {}).get('isMainSleep')),
                sessions[0],
            )
            start_value = chosen.get('start') or (chosen.get('raw') or {}).get('startTime')
            end_value = chosen.get('end') or (chosen.get('raw') or {}).get('endTime')
            start_dt = _parse_iso(start_value) if start_value else None
            end_dt = _parse_iso(end_value) if end_value else None
            if start_dt is not None and end_dt is not None:
                starts.append(start_dt)
                ends.append(end_dt)
        except Exception:
            logging.exception('Failed reading sleep file %s', path)
    return starts, ends


def is_in_median_sleep_window(user_id: str, now: datetime, max_items: int = 14) -> bool:
    """Return True when `now` is inside the user's median sleep time window.

    - Uses saved sleep sessions from today and the preceding `max_items - 1` dates.
    - If no usable data exists, returns False.
    """
    try:
        starts, ends = _get_sleep_times_for_window(user_id, now, max_items)
        if not starts or not ends:
            return False

        start_minutes = [_time_to_night_axis_minutes(d) for d in starts]
        end_minutes = [_time_to_night_axis_minutes(d) for d in ends]
        median_start = _median(start_minutes)
        median_end = _median(end_minutes)
        if median_start is None or median_end is None:
            return False

        if median_end <= median_start:
            median_end += 24 * 60

        now_minutes = _time_to_night_axis_minutes(now)
        if now_minutes < median_start:
            now_minutes += 24 * 60

        return median_start <= now_minutes <= median_end
    except Exception:
        logging.exception('is_in_median_sleep_window failed for %s', user_id)
        return False


def get_effective_wake_time(user_id: str, max_recent: int = 3, fallback_hour: int = 7):
    """Return (datetime, source) for wake time (sleep end) selection.

    Source: 'prev_day' / 'average' / 'fallback' / 'error'
    """
    try:
        prev_end = get_previous_sleep_end(user_id, days_back=1)
        if prev_end is not None:
            return prev_end, 'prev_day'
        recent = get_recent_sleep_ends(user_id, max_items=max_recent)
        if recent:
            avg = get_average_of_ends(recent[:max_recent])
            if avg is not None:
                return avg, 'average'
        now = datetime.now().astimezone()
        today = now.date()
        fallback_dt = datetime(year=today.year, month=today.month, day=today.day, hour=fallback_hour, minute=0, second=0, tzinfo=now.tzinfo)
        return fallback_dt, 'fallback'
    except Exception:
        logging.exception('get_effective_wake_time failed for %s', user_id)
        return None, 'error'


def estimate_wake_time(user_id: str, non_wear_threshold_min: int = 240):
    """Estimate wake_time following the user's specified flow.

    Returns (wake_dt, source) where source is one of:
      - 'api' : obtained from Fitbit Sleep API endTime
      - 'non_wear' : long non-wear block ended; first valid sample after block
      - 'first_sample' : first valid sample of the day within 06:00-11:00
      - 'none' : no wake time
      - 'error': on unexpected failure
    """
    try:
        # 1) Google Health sleep API: try yesterday then today
        try:
            for d in [(datetime.now().date() - timedelta(days=1)), datetime.now().date()]:
                try:
                    sleeps = _fetch_sleep_for_date(user_id, d.isoformat())
                except Exception:
                    continue
                for session in sleeps:
                    end_s = session.get('end') or session.get('endTime') or session.get('endTimeLocal')
                    if end_s:
                        dt = _parse_iso(end_s)
                        if dt:
                            return dt, 'api'
        except Exception:
            logging.exception('estimate_wake_time: Google Health sleep API fetch failed for %s', user_id)

        # 2) Detect long non-wear block: look at per-minute heart files for yesterday and today
        try:
            safe = storage_sqlite._sanitize_user_id(user_id)
            dates = [datetime.now().date(), (datetime.now().date() - timedelta(days=1))]
            timestamps = []
            for d in reversed(dates):
                path = storage_sqlite._day_file_path(user_id, datetime(d.year, d.month, d.day))
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        obj = json.load(f)
                    permin = obj.get('PerMinute') or []
                    for e in permin:
                        t = e.get('time')
                        if not t:
                            continue
                        try:
                            hh, mm, ss = [int(x) for x in t.split(':')]
                            ts = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=datetime.now().astimezone().tzinfo)
                            timestamps.append(ts)
                        except Exception:
                            continue
                except Exception:
                    continue
            # sort and dedupe
            timestamps = sorted(set(timestamps))
            # find gaps >= threshold between 22:00 (prev day) and 11:00 (today)
            gap_candidate = None
            if timestamps:
                for i in range(len(timestamps) - 1):
                    a = timestamps[i]
                    b = timestamps[i + 1]
                    gap_min = (b - a).total_seconds() / 60.0
                    if gap_min >= non_wear_threshold_min:
                        # consider gap that spans night-to-morning: if a is between 21:00 and 06:00 or b between 04:30 and 11:00
                        if (21 <= a.hour <= 23) or (0 <= a.hour <= 6) or (4 <= b.hour <= 11):
                            gap_candidate = b
                            break
            if gap_candidate is not None:
                # accept if between 04:30 and 11:00
                if (gap_candidate.hour > 4 or (gap_candidate.hour == 4 and gap_candidate.minute >= 30)) and gap_candidate.hour <= 11:
                    return gap_candidate, 'non_wear'
        except Exception:
            logging.exception('estimate_wake_time: non-wear detection failed for %s', user_id)

        # 3) First valid sample of today between 06:00 and 11:00
        try:
            d = datetime.now().date()
            path = storage_sqlite._day_file_path(user_id, datetime(d.year, d.month, d.day))
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                permin = obj.get('PerMinute') or []
                first_dt = None
                for e in permin:
                    t = e.get('time')
                    if not t:
                        continue
                    try:
                        hh, mm, ss = [int(x) for x in t.split(':')]
                        ts = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=datetime.now().astimezone().tzinfo)
                        if first_dt is None:
                            first_dt = ts
                    except Exception:
                        continue
                if first_dt is not None and 6 <= first_dt.hour <= 11:
                    return first_dt, 'first_sample'
        except Exception:
            logging.exception('estimate_wake_time: first-sample check failed for %s', user_id)

        return None, 'none'
    except Exception:
        logging.exception('estimate_wake_time unexpected failure for %s', user_id)
        return None, 'error'
