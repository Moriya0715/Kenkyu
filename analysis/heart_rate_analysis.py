"""Summarize heart-rate reserve zones and Fitbit wear time for configured users."""

import csv
import json
import math
import re
from collections import Counter
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any


USER_AGES: dict[str, int] = {
    # "user@example.com": 30,
}
START_DATE = ""
END_DATE = ""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_OUTPUT_DIR = PROJECT_ROOT / "data_output"
OUTPUT_DIR = Path(__file__).resolve().parent
ZONE_NAMES = ("under_30", "30_to_39", "40_to_59", "60_to_84", "85_or_more")
ZONE_COLUMNS = {
    "under_30": "under_30_percent",
    "30_to_39": "30_to_39_percent",
    "40_to_59": "40_to_59_percent",
    "60_to_84": "60_to_84_percent",
    "85_or_more": "85_or_more_percent",
}


def _configured_date(value: str, name: str) -> date:
    if not value:
        raise ValueError(f"Set {name} in {Path(__file__).name} using YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD: {value!r}") from exc


def _user_folder_name(user_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", user_id) or "unknown"


def _zone_for_hrr(hrr_percent: float) -> str:
    if hrr_percent < 30:
        return "under_30"
    if hrr_percent < 40:
        return "30_to_39"
    if hrr_percent < 60:
        return "40_to_59"
    if hrr_percent < 85:
        return "60_to_84"
    return "85_or_more"


def _read_day(path: Path) -> tuple[dict[str, float | None], float | None]:
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    samples = data.get("PerMinute")
    calculated = data.get("calculated_value")
    if not isinstance(samples, list):
        raise ValueError(f"Expected PerMinute to be a list in {path}.")
    if not isinstance(calculated, dict):
        raise ValueError(f"Expected calculated_value to be an object in {path}.")

    heart_rates: dict[str, float | None] = {}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict) or not isinstance(sample.get("time"), str):
            raise ValueError(f"Invalid PerMinute record {index} in {path}.")
        try:
            sample_time = time.fromisoformat(sample["time"])
        except ValueError as exc:
            raise ValueError(f"Invalid time in PerMinute record {index} in {path}.") from exc
        minute_key = sample_time.strftime("%H:%M")
        if minute_key in heart_rates:
            raise ValueError(f"Duplicate heart-rate minute {minute_key} in {path}.")

        heart_rate = sample.get("heart_rate")
        if heart_rate is not None and (
            isinstance(heart_rate, bool)
            or not isinstance(heart_rate, (int, float))
            or not math.isfinite(heart_rate)
        ):
            raise ValueError(f"Invalid heart_rate in PerMinute record {index} in {path}.")
        heart_rates[minute_key] = float(heart_rate) if heart_rate is not None else None

    resting_heart_rate = calculated.get("resting_heart_rate")
    if resting_heart_rate is not None and (
        isinstance(resting_heart_rate, bool)
        or not isinstance(resting_heart_rate, (int, float))
        or not math.isfinite(resting_heart_rate)
    ):
        raise ValueError(f"Invalid resting_heart_rate in {path}.")
    return heart_rates, (
        float(resting_heart_rate) if resting_heart_rate is not None else None
    )


def analyze_user(
    user_id: str,
    age: int,
    start_date: date,
    end_date: date,
    data_output_dir: Path = DATA_OUTPUT_DIR,
) -> dict[str, Any]:
    max_heart_rate = 220 - age
    total_minutes = (end_date - start_date).days * 1440 + 1440
    wearing_minutes = 0
    hrr_minutes = 0
    zone_counts: Counter[str] = Counter()
    missing_heart_files: list[str] = []
    missing_resting_hr_days: list[str] = []
    invalid_resting_hr_days: list[str] = []

    current_date = start_date
    while current_date <= end_date:
        date_text = current_date.isoformat()
        path = (
            data_output_dir
            / _user_folder_name(user_id)
            / "value"
            / f"heart_{date_text}.json"
        )
        if not path.is_file():
            missing_heart_files.append(date_text)
            current_date += timedelta(days=1)
            continue

        heart_rates, resting_heart_rate = _read_day(path)
        wearing_minutes += sum(value is not None for value in heart_rates.values())

        if resting_heart_rate is None:
            if any(value is not None for value in heart_rates.values()):
                missing_resting_hr_days.append(date_text)
        elif max_heart_rate <= resting_heart_rate:
            invalid_resting_hr_days.append(date_text)
        else:
            heart_rate_reserve = max_heart_rate - resting_heart_rate
            for heart_rate in heart_rates.values():
                if heart_rate is None:
                    continue
                hrr_percent = (
                    (heart_rate - resting_heart_rate) / heart_rate_reserve * 100
                )
                zone_counts[_zone_for_hrr(hrr_percent)] += 1
                hrr_minutes += 1

        current_date += timedelta(days=1)

    result: dict[str, Any] = {
        "user_id": user_id,
        "age": age,
        "max_heart_rate": max_heart_rate,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "total_minutes": total_minutes,
        "wearing_minutes": wearing_minutes,
        "wearing_rate_percent": round(wearing_minutes / total_minutes * 100, 2),
        "hrr_sample_minutes": hrr_minutes,
        "missing_heart_files": ";".join(missing_heart_files),
        "missing_resting_hr_days": ";".join(missing_resting_hr_days),
        "invalid_resting_hr_days": ";".join(invalid_resting_hr_days),
    }
    for zone_name in ZONE_NAMES:
        result[f"{zone_name}_count"] = zone_counts[zone_name]
        result[ZONE_COLUMNS[zone_name]] = (
            round(zone_counts[zone_name] / hrr_minutes * 100, 2)
            if hrr_minutes
            else None
        )
    return result


def _validate_configuration(start_date: date, end_date: date) -> None:
    if not USER_AGES:
        raise ValueError("Add target user IDs and ages to USER_AGES before running.")
    if end_date < start_date:
        raise ValueError("END_DATE must be on or after START_DATE.")

    sanitized_users: set[str] = set()
    for user_id, age in USER_AGES.items():
        if not user_id:
            raise ValueError("USER_AGES must not contain an empty user ID.")
        if isinstance(age, bool) or not isinstance(age, int) or age <= 0:
            raise ValueError(f"Age for {user_id!r} must be a positive integer.")
        safe_name = _user_folder_name(user_id)
        if safe_name in sanitized_users:
            raise ValueError("USER_AGES contains IDs that map to the same data folder.")
        sanitized_users.add(safe_name)


def _write_results(results: list[dict[str, Any]], output_path: Path) -> None:
    fieldnames = list(results[0])
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def main() -> None:
    start_date = _configured_date(START_DATE, "START_DATE")
    end_date = _configured_date(END_DATE, "END_DATE")
    _validate_configuration(start_date, end_date)

    results = [
        analyze_user(user_id, age, start_date, end_date)
        for user_id, age in USER_AGES.items()
    ]
    output_path = OUTPUT_DIR / (
        f"heart_rate_analysis_{start_date.isoformat()}_{end_date.isoformat()}.csv"
    )
    _write_results(results, output_path)
    print(f"Analysis results saved to {output_path}")


if __name__ == "__main__":
    main()
