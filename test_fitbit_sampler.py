import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import fitbit_sampler


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


if __name__ == "__main__":
    unittest.main()