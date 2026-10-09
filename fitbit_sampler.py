"""
Google Health API sampler with the existing one-minute JSON output contract.
sample_once() returns a single latest sample, fetch_day() returns a full-day per-minute record.
The output format is compatible with the existing PerMinute JSON schema used for Fitbit data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import time
from typing import Any, Iterable, Optional

import requests

import get_fitbit_token


HEALTH_API_ROOT = "https://health.googleapis.com/v4/users/me/dataTypes"
_RESTING_HEART_RATE_CACHE_TTL_SECONDS = 60 * 60
_RESTING_HEART_RATE_CACHE: dict[tuple[str, str], tuple[float, Optional[int]]] = {}


def _get_access_token(user_id: str) -> str:
    access_token, _, _ = get_fitbit_token.get_cached_access_token(user_id)
    if not access_token:
        raise RuntimeError("No Google Health access token")
    return access_token


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_number(value: Any, preferred_keys: Iterable[str]) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    for key in preferred_keys:
        if key in value:
            found = _first_number(value[key], ())
            if found is not None:
                return found
    for nested in value.values():
        found = _first_number(nested, ())
        if found is not None:
            return found
    return None


def _list_data_points(access_token: str, data_type: str, start: datetime, end: datetime) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    filter_name = data_type.replace("-", "_")
    if data_type == "heart-rate":
        time_field = f"{filter_name}.sample_time.physical_time"
    elif data_type == "sleep":
        time_field = f"{filter_name}.interval.end_time"
    elif data_type == "daily-resting-heart-rate":
        time_field = f"{filter_name}.date"
    else:
        time_field = f"{filter_name}.interval.start_time"
    if data_type == "daily-resting-heart-rate":
        start_value = start.date().isoformat()
        end_value = end.date().isoformat()
    else:
        start_value = _utc_iso(start)
        end_value = _utc_iso(end)
    params = {
        "pageSize": 10000,
        "filter": (
            f'{time_field} >= "{start_value}" '
            f'AND {time_field} < "{end_value}"'
        ),
    }
    points: list[dict] = []
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(
            f"{HEALTH_API_ROOT}/{data_type}/dataPoints",
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        points.extend(point for point in body.get("dataPoints", []) if isinstance(point, dict))
        page_token = body.get("nextPageToken")
        if not page_token:
            return points


def _rollup_data_points(access_token: str, data_type: str, start: datetime, end: datetime) -> list[dict]:
    """Return one-minute reconciled Google Health rollups for an interval data type."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = {
        "range": {"startTime": _utc_iso(start), "endTime": _utc_iso(end)},
        "windowSize": "60s",
        "pageSize": 10000,
    }
    rollups: list[dict] = []
    page_token = None
    while True:
        request_body = dict(body)
        if page_token:
            request_body["pageToken"] = page_token
        response = requests.post(
            f"{HEALTH_API_ROOT}/{data_type}/dataPoints:rollUp",
            headers=headers,
            json=request_body,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        rollups.extend(item for item in payload.get("rollupDataPoints", []) if isinstance(item, dict))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return rollups


def _reconcile_data_points(access_token: str, data_type: str, start: datetime, end: datetime) -> list[dict]:
    """Return a single data stream reconciled across Google Health sources."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    filter_name = data_type.replace("-", "_")
    time_field = f"{filter_name}.interval.start_time"
    params = {
        "pageSize": 10000,
        "filter": (
            f'{time_field} >= "{_utc_iso(start)}" '
            f'AND {time_field} < "{_utc_iso(end)}"'
        ),
    }
    points: list[dict] = []
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(
            f"{HEALTH_API_ROOT}/{data_type}/dataPoints:reconcile",
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        points.extend(point for point in body.get("dataPoints", []) if isinstance(point, dict))
        page_token = body.get("nextPageToken")
        if not page_token:
            return points


def _rollups_by_minute(rollups: list[dict], payload_key: str, value_key: str, local_tz) -> dict[str, float]:
    values = {}
    for rollup in rollups:
        payload = rollup.get(payload_key)
        timestamp = _parse_time(rollup.get("startTime"))
        value = _first_number(payload.get(value_key) if isinstance(payload, dict) else None, ())
        if timestamp is not None and value is not None:
            values[timestamp.astimezone(local_tz).strftime("%H:%M:00")] = value
    return values


def _points_by_minute(
    points: list[dict], payload_key: str, local_tz, preferred_keys: Iterable[str], aggregate: bool
) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for point in points:
        payload = point.get(payload_key)
        if not isinstance(payload, dict):
            continue
        sample_time = payload.get("sampleTime")
        interval = payload.get("interval")
        timestamp = _parse_time(
            sample_time.get("physicalTime") if isinstance(sample_time, dict)
            else interval.get("startTime") if isinstance(interval, dict)
            else None
        )
        value = _first_number(payload, preferred_keys)
        if timestamp is None or value is None:
            continue
        minute = timestamp.astimezone(local_tz).strftime("%H:%M:00")
        buckets.setdefault(minute, []).append(value)
    if aggregate:
        return {minute: sum(values) for minute, values in buckets.items()}
    return {minute: sum(values) / len(values) for minute, values in buckets.items()}


def _fetch_minutes(access_token: str, start: datetime, end: datetime) -> list[dict]:
    local_tz = start.tzinfo or datetime.now().astimezone().tzinfo
    heart_rates = _points_by_minute(
        _list_data_points(access_token, "heart-rate", start, end),
        "heartRate",
        local_tz,
        ("beatsPerMinute",),
        aggregate=False,
    )
    steps = _rollups_by_minute(
        _rollup_data_points(access_token, "steps", start, end),
        "steps",
        "countSum",
        local_tz,
    )
    if not steps:
        steps = _points_by_minute(
            _reconcile_data_points(access_token, "steps", start, end),
            "steps",
            local_tz,
            ("count",),
            aggregate=True,
        )
    if not steps:
        steps = _points_by_minute(
            _list_data_points(access_token, "steps", start, end),
            "steps",
            local_tz,
            ("count",),
            aggregate=True,
        )
    calories = _rollups_by_minute(
        _rollup_data_points(access_token, "total-calories", start, end),
        "totalCalories",
        "kcalSum",
        local_tz,
    )
    samples = []
    for minute in sorted(set(heart_rates) | set(steps) | set(calories)):
        heart_rate = round(heart_rates[minute]) if minute in heart_rates else None
        step_count = int(steps[minute]) if minute in steps else None
        samples.append(
            {
            "time": minute,
            "heart_rate": heart_rate,
            "steps": 0 if heart_rate is not None and step_count is None else step_count,
            "calories": calories.get(minute),
        }
        )
    return samples


def _complete_minute_timeline(start: datetime, end: datetime, samples: list[dict]) -> list[dict]:
    """Fill every minute from 00:01 through the requested inclusive end minute."""
    by_time = {sample.get("time"): sample for sample in samples if sample.get("time")}
    minute = start + timedelta(minutes=1)
    complete = []
    while minute <= end:
        time_key = minute.strftime("%H:%M:00")
        sample = by_time.get(time_key, {})
        complete.append(
            {
                "time": time_key,
                "heart_rate": sample.get("heart_rate"),
                "steps": sample.get("steps"),
                "calories": sample.get("calories"),
            }
        )
        minute += timedelta(minutes=1)
    return complete


def fetch_sleep_sessions(user_id: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch Google Health sleep sessions in the existing sleep-file record shape."""
    sessions = []
    for point in _list_data_points(_get_access_token(user_id), "sleep", start, end):
        raw = point.get("sleep")
        if not isinstance(raw, dict):
            continue
        interval = raw.get("interval")
        start_time = interval.get("startTime") if isinstance(interval, dict) else None
        end_time = interval.get("endTime") if isinstance(interval, dict) else None
        if not start_time:
            continue
        start_dt = _parse_time(start_time)
        end_dt = _parse_time(end_time)
        duration_min = None
        if start_dt and end_dt:
            duration_min = int((end_dt - start_dt).total_seconds() / 60)
        sessions.append(
            {
                "start": start_time,
                "end": end_time,
                "duration_min": duration_min,
                "raw": raw,
            }
        )
    return sessions


def sample_once(user_id: str):
    """Fetch the latest available one-minute Google Health sample."""
    access_token = _get_access_token(user_id)
    end = datetime.now().astimezone().replace(second=0, microsecond=0)
    samples = _fetch_minutes(access_token, end - timedelta(minutes=1), end)
    sample = samples[-1] if samples else {
        "time": end.strftime("%H:%M:00"),
        "heart_rate": None,
        "steps": None,
        "calories": None,
    }
    sample["raw"] = {"source": "google_health"}
    return sample


def _extract_resting_heart_rate(data_points: list[dict]) -> Optional[int]:
    for point in data_points:
        payload = point.get("dailyRestingHeartRate")
        if not isinstance(payload, dict):
            continue
        value = _first_number(payload.get("beatsPerMinute"), ())
        if value is not None:
            return int(round(value))
    return None


def _get_daily_resting_heart_rate(
    user_id: str,
    access_token: str,
    day: str,
    start: datetime,
) -> Optional[int]:
    cache_key = (user_id, day)
    now = time.monotonic()
    cached = _RESTING_HEART_RATE_CACHE.get(cache_key)
    if cached and now - cached[0] < _RESTING_HEART_RATE_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        daily_points = _list_data_points(
            access_token,
            "daily-resting-heart-rate",
            start,
            start + timedelta(days=1),
        )
        value = _extract_resting_heart_rate(daily_points)
    except Exception:
        logging.exception("Failed to fetch daily resting heart rate for %s on %s", user_id, day)
        value = None

    _RESTING_HEART_RATE_CACHE[cache_key] = (now, value)
    return value


def fetch_day(user_id: str, date: str, include_summary: bool = False):
    """Fetch one calendar day as existing PerMinute-compatible records."""
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime.fromisoformat(date).replace(tzinfo=local_tz)
    full_day_end = start + timedelta(days=1) - timedelta(minutes=1)
    now = datetime.now(local_tz).replace(second=0, microsecond=0)
    end = min(full_day_end, now) if start.date() == now.date() else full_day_end
    access_token = _get_access_token(user_id)
    raw_samples = _fetch_minutes(access_token, start, end + timedelta(minutes=1))
    per_minute = _complete_minute_timeline(start, end, raw_samples)
    if include_summary:
        resting_heart_rate = _get_daily_resting_heart_rate(
            user_id,
            access_token,
            date,
            start,
        )
        return per_minute, {"resting_heart_rate": resting_heart_rate}
    return per_minute


def save_day_json(user_id: str, date: str, per_minute: list, out_dir: str = "./data_output/value", resting_heart_rate=None):
    """Save full-day per-minute data using the existing JSON schema and merge policy."""
    try:
        import storage_sqlite

        day_filepath = storage_sqlite._day_file_path(user_id, datetime.fromisoformat(date + "T00:00:00"))
        os.makedirs(os.path.dirname(day_filepath), exist_ok=True)
    except Exception:
        os.makedirs(out_dir, exist_ok=True)
        day_filepath = os.path.join(out_dir, f"heart_{date}.json")

    existing = {}
    if os.path.exists(day_filepath):
        try:
            with open(day_filepath, "r", encoding="utf-8") as existing_file:
                existing = json.load(existing_file)
        except Exception:
            existing = {}

    def normalize_time(value: str) -> Optional[str]:
        if not value:
            return None
        if len(value) >= 5 and value[2] == ":":
            return value[:5] + ":00"
        try:
            return datetime.fromisoformat(value).strftime("%H:%M:00")
        except Exception:
            return value

    by_time = {}
    for item in existing.get("PerMinute", []) if isinstance(existing, dict) else []:
        if isinstance(item, dict) and (time_key := normalize_time(item.get("time"))):
            by_time[time_key] = {**item, "time": time_key}
    for item in per_minute:
        if not isinstance(item, dict) or not (time_key := normalize_time(item.get("time"))):
            continue
        current = {**item, "time": time_key}
        previous = by_time.get(time_key, {})
        by_time[time_key] = {
            **current,
            "heart_rate": current.get("heart_rate") if current.get("heart_rate") is not None else previous.get("heart_rate"),
            "steps": current.get("steps") if current.get("steps") is not None else previous.get("steps"),
            "calories": current.get("calories"),
        }

    for item in by_time.values():
        if item.get("heart_rate") is not None and item.get("steps") is None:
            item["steps"] = 0

    merged = [by_time[key] for key in sorted(by_time)]
    heart_rates = [item["heart_rate"] for item in merged if isinstance(item.get("heart_rate"), (int, float))]
    steps = [item["steps"] for item in merged if isinstance(item.get("steps"), (int, float))]
    calories = [item["calories"] for item in merged if isinstance(item.get("calories"), (int, float))]
    existing_calculated = existing.get("calculated_value", {}) if isinstance(existing, dict) else {}
    if resting_heart_rate is None:
        resting_heart_rate = existing_calculated.get("resting_heart_rate")
    output = {
        "PerMinute": merged,
        "calculated_value": {
            "heart_rate_max": max(heart_rates) if heart_rates else None,
            "heart_rate_min": min(heart_rates) if heart_rates else None,
            "steps_max": max(steps) if steps else None,
            "steps_min": min(steps) if steps else None,
            "steps_max_min_diff": int(max(steps) - min(steps)) if steps else None,
            "resting_heart_rate": resting_heart_rate,
            "calories_max": max(calories) if calories else None,
            "calories_min": min(calories) if calories else None,
        },
    }
    temp_path = day_filepath + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as output_file:
        json.dump(output, output_file, indent=2, ensure_ascii=False)
    os.replace(temp_path, day_filepath)
    return day_filepath
