import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from analysis.heart_rate_analysis import analyze_user, _zone_for_hrr


class HeartRateAnalysisTests(unittest.TestCase):
    def test_zone_boundaries(self) -> None:
        self.assertEqual(_zone_for_hrr(29.99), "under_30")
        self.assertEqual(_zone_for_hrr(30), "30_to_39")
        self.assertEqual(_zone_for_hrr(40), "40_to_59")
        self.assertEqual(_zone_for_hrr(60), "60_to_84")
        self.assertEqual(_zone_for_hrr(85), "85_or_more")

    def test_analyze_user_calculates_zone_counts_and_wear_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day_file = (
                root
                / "person_example_com"
                / "value"
                / "heart_2026-01-01.json"
            )
            day_file.parent.mkdir(parents=True)
            day_file.write_text(
                json.dumps(
                    {
                        "PerMinute": [
                            {"time": "00:01:00", "heart_rate": 84},
                            {"time": "00:02:00", "heart_rate": 96},
                            {"time": "00:03:00", "heart_rate": 108},
                            {"time": "00:04:00", "heart_rate": 132},
                            {"time": "00:05:00", "heart_rate": 162},
                            {"time": "00:06:00", "heart_rate": None},
                        ],
                        "calculated_value": {"resting_heart_rate": 60},
                    }
                ),
                encoding="utf-8",
            )

            result = analyze_user(
                "person@example.com",
                40,
                date(2026, 1, 1),
                date(2026, 1, 1),
                root,
            )

        self.assertEqual(result["wearing_minutes"], 5)
        self.assertEqual(result["wearing_rate_percent"], round(5 / 1440 * 100, 2))
        self.assertEqual(result["hrr_sample_minutes"], 5)
        self.assertEqual(result["under_30_count"], 1)
        self.assertEqual(result["30_to_39_count"], 1)
        self.assertEqual(result["40_to_59_count"], 1)
        self.assertEqual(result["60_to_84_count"], 1)
        self.assertEqual(result["85_or_more_count"], 1)
        self.assertEqual(
            sum(result[f"{name}_percent"] for name in (
                "under_30",
                "30_to_39",
                "40_to_59",
                "60_to_84",
                "85_or_more",
            )),
            100,
        )


if __name__ == "__main__":
    unittest.main()
