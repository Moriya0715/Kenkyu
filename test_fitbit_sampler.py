import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import fitbit_sampler
from japan_time import JST
import storage_sqlite


class RestingHeartRateTests(unittest.TestCase):
    def test_extracts_daily_beats_per_minute(self):
        points = [
            {"dailyRestingHeartRate": {"beatsPerMinute": "58"}},
        ]

        self.assertEqual(fitbit_sampler._extract_resting_heart_rate(points), 58)

    def test_daily_resting_rate_uses_civil_date_filter(self):
        start = datetime(2026, 10, 7, tzinfo=timezone(timedelta(hours=9)))
        end = start + timedelta(days=1)
        response = Mock()
        response.json.return_value = {"dataPoints": []}

        with patch.object(fitbit_sampler.requests, "get", return_value=response) as get:
            fitbit_sampler._list_data_points(
                "access-token", "daily-resting-heart-rate", start, end
            )

        self.assertEqual(
            get.call_args.kwargs["params"]["filter"],
            'daily_resting_heart_rate.date >= "2026-10-07" '
            'AND daily_resting_heart_rate.date < "2026-10-08"',
        )

    def test_daily_summary_api_failure_does_not_fail_minute_fetch(self):
        with patch.object(fitbit_sampler, "_get_access_token", return_value="access-token"), \
                patch.object(fitbit_sampler, "_fetch_minutes", return_value=[]) as fetch_minutes, \
                patch.object(
                    fitbit_sampler,
                    "_list_data_points",
                    side_effect=RuntimeError("daily summary unavailable"),
                ), self.assertLogs(level="ERROR"):
            per_minute, summary = fitbit_sampler.fetch_day(
                "user@example.com", "2026-10-07", include_summary=True
            )

        self.assertTrue(per_minute)
        self.assertIsNone(summary["resting_heart_rate"])
        fetch_minutes.assert_called_once()

    def test_daily_resting_rate_is_cached_per_user_and_date(self):
        fitbit_sampler._RESTING_HEART_RATE_CACHE.clear()
        start = datetime(2026, 10, 7, tzinfo=timezone.utc)
        point = {"dailyRestingHeartRate": {"beatsPerMinute": "57"}}

        with patch.object(fitbit_sampler, "_list_data_points", return_value=[point]) as fetch:
            first = fitbit_sampler._get_daily_resting_heart_rate(
                "cache-test-user", "access-token", "2026-10-07", start
            )
            second = fitbit_sampler._get_daily_resting_heart_rate(
                "cache-test-user", "access-token", "2026-10-07", start
            )

        self.assertEqual((first, second), (57, 57))
        fetch.assert_called_once()

    def test_fetch_day_uses_japan_midnight_boundaries(self):
        fixed_now = datetime(2026, 10, 29, 18, 30, tzinfo=JST)
        with patch.object(fitbit_sampler, "_get_access_token", return_value="access-token"), \
                patch.object(fitbit_sampler, "_fetch_minutes", return_value=[]) as fetch_minutes, \
                patch.object(fitbit_sampler, "now_jst", return_value=fixed_now):
            per_minute = fitbit_sampler.fetch_day("user@example.com", "2026-10-28")

        start, end = fetch_minutes.call_args.args[1:]
        self.assertEqual(start.isoformat(), "2026-10-28T00:00:00+09:00")
        self.assertEqual(end.isoformat(), "2026-10-29T00:00:00+09:00")
        self.assertEqual(fitbit_sampler._utc_iso(start), "2026-10-27T15:00:00Z")
        self.assertEqual(fitbit_sampler._utc_iso(end), "2026-10-28T15:00:00Z")
        self.assertEqual(len(per_minute), 1439)
        self.assertEqual(per_minute[0]["time"], "00:01:00")
        self.assertEqual(per_minute[-1]["time"], "23:59:00")

    def test_naive_api_boundaries_are_interpreted_as_japan_time(self):
        self.assertEqual(
            fitbit_sampler._utc_iso(datetime(2026, 10, 28)),
            "2026-10-27T15:00:00Z",
        )

    def test_save_day_json_preserves_existing_rate_when_new_value_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "heart_2026-10-07.json")
            with open(output_path, "w", encoding="utf-8") as output_file:
                json.dump({"PerMinute": [], "calculated_value": {"resting_heart_rate": 55}}, output_file)

            with patch("storage_sqlite._day_file_path", return_value=output_path):
                fitbit_sampler.save_day_json(
                    "save-test-user", "2026-10-07", [], resting_heart_rate=None
                )
                with open(output_path, "r", encoding="utf-8") as output_file:
                    saved = json.load(output_file)
                self.assertEqual(saved["calculated_value"]["resting_heart_rate"], 55)

                fitbit_sampler.save_day_json(
                    "save-test-user", "2026-10-07", [], resting_heart_rate=57
                )
                with open(output_path, "r", encoding="utf-8") as output_file:
                    saved = json.load(output_file)

        self.assertEqual(saved["calculated_value"]["resting_heart_rate"], 57)

    def test_save_day_json_does_not_merge_unzoned_legacy_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "heart_2026-10-07.json")
            with open(output_path, "w", encoding="utf-8") as output_file:
                json.dump(
                    {
                        "PerMinute": [{"time": "09:01:00", "heart_rate": 60}],
                        "calculated_value": {},
                    },
                    output_file,
                )

            with patch("storage_sqlite._day_file_path", return_value=output_path):
                fitbit_sampler.save_day_json(
                    "save-test-user",
                    "2026-10-07",
                    [{"time": "00:01:00", "heart_rate": 55}],
                )
            with open(output_path, "r", encoding="utf-8") as output_file:
                saved = json.load(output_file)
            with open(output_path + ".legacy", "r", encoding="utf-8") as legacy_file:
                legacy = json.load(legacy_file)

        self.assertEqual(saved["time_zone"], "Asia/Tokyo")
        self.assertEqual(legacy["PerMinute"][0]["time"], "09:01:00")
        self.assertEqual(
            saved["PerMinute"],
            [{"time": "00:01:00", "heart_rate": 55, "steps": 0, "calories": None}],
        )

    def test_recent_samples_ignore_legacy_files_without_timezone_metadata(self):
        fixed_now = datetime(2026, 10, 28, 12, 0, tzinfo=JST)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "heart_2026-10-28.json")
            with open(path, "w", encoding="utf-8") as output_file:
                json.dump(
                    {"PerMinute": [{"time": "11:30:00", "heart_rate": 60, "steps": 0}]},
                    output_file,
                )
            with patch.object(storage_sqlite, "_day_file_path", return_value=path), \
                    patch.object(storage_sqlite, "now_jst", return_value=fixed_now):
                samples = storage_sqlite.get_recent_samples("legacy-user", minutes=60)

        self.assertEqual(samples, [])


if __name__ == "__main__":
    unittest.main()