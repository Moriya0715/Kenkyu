import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from analysis import heart_rate_analysis
from analysis.heart_rate_analysis import analyze_user, _zone_for_hrr


class HeartRateAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(heart_rate_analysis, "PERIODS", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_input_data_directory_is_project_data_output(self) -> None:
        self.assertEqual(
            heart_rate_analysis.DATA_OUTPUT_DIR,
            heart_rate_analysis.PROJECT_ROOT / "data_output",
        )

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

    def test_exclusion_calendar_removes_busy_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_dir = root / "person_example_com"
            (user_dir / "value").mkdir(parents=True)
            (user_dir / "date").mkdir()
            (user_dir / "value" / "heart_2026-01-01.json").write_text(
                json.dumps(
                    {
                        "PerMinute": [
                            {"time": "00:01:00", "heart_rate": 84},
                            {"time": "00:02:00", "heart_rate": 96},
                            {"time": "00:03:00", "heart_rate": 108},
                            {"time": "00:04:00", "heart_rate": 132},
                            {"time": "00:05:00", "heart_rate": 162},
                        ],
                        "calculated_value": {"resting_heart_rate": 60},
                    }
                ),
                encoding="utf-8",
            )
            (user_dir / "date" / "freebusy_2026-01-01.csv").write_text(
                "start_iso,end_iso,duration_min\n"
                "2026-01-01T00:00:00+09:00,2026-01-01T00:03:00+09:00,3\n"
                "2026-01-01T00:05:00+09:00,2026-01-01T23:59:59+09:00,1434\n",
                encoding="utf-8",
            )

            result = analyze_user(
                "person@example.com", 40, date(2026, 1, 1), date(2026, 1, 1),
                root, exclude_calendar=True,
            )
            without = analyze_user(
                "person@example.com", 40, date(2026, 1, 1), date(2026, 1, 1), root,
            )

        self.assertEqual(result["calendar_excluded_minutes"], 2)
        self.assertEqual(result["wearing_minutes"], 3)
        self.assertEqual(result["total_minutes"], 1438)
        self.assertEqual(result["missing_calendar_files"], "")
        self.assertEqual(without["wearing_minutes"], 5)
        self.assertNotIn("calendar_excluded_minutes", without)

    def test_exclusion_calendar_flag_is_passed_to_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "analyze_user",
                                 return_value={"user_id": "x"}) as analyze:
                heart_rate_analysis.main(["--exclusion_calendar", "--output-dir", directory])

        self.assertTrue(analyze.call_args.kwargs["exclude_calendar"])

    def test_json_flag_writes_json_output_file(self) -> None:
        result = {"user_id": "person@example.com", "wearing_minutes": 120}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "START_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "END_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "OUTPUT_DIR", Path(directory)), \
                    patch.object(heart_rate_analysis, "analyze_user", return_value=result):
                heart_rate_analysis.main(["--json"])

            output_path = Path(directory) / "heart_rate_analysis_2026-01-01_2026-01-01.json"
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), [result])

    def test_csv_flag_writes_csv_output_file(self) -> None:
        result = {"user_id": "person@example.com", "wearing_minutes": 120}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "START_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "END_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "OUTPUT_DIR", Path(directory)), \
                    patch.object(heart_rate_analysis, "analyze_user", return_value=result):
                heart_rate_analysis.main(["--csv"])

            output_path = Path(directory) / "heart_rate_analysis_2026-01-01_2026-01-01.csv"
            with output_path.open(encoding="utf-8-sig", newline="") as file:
                self.assertEqual(list(csv.DictReader(file)), [
                    {"user_id": "person@example.com", "wearing_minutes": "120"}
                ])

    def test_csv_remains_the_default_output_format(self) -> None:
        result = {"user_id": "person@example.com", "wearing_minutes": 120}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "START_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "END_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "OUTPUT_DIR", Path(directory)), \
                    patch.object(heart_rate_analysis, "analyze_user", return_value=result):
                heart_rate_analysis.main([])

            self.assertTrue(
                (Path(directory) / "heart_rate_analysis_2026-01-01_2026-01-01.csv").is_file()
            )

    def test_output_directory_can_be_set_from_command_line(self) -> None:
        result = {"user_id": "person@example.com", "wearing_minutes": 120}
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "nested" / "results"
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "START_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "END_DATE", "2026-01-01"), \
                    patch.object(heart_rate_analysis, "analyze_user", return_value=result):
                heart_rate_analysis.main(["--csv", "--output-dir", str(output_dir)])

            output_path = output_dir / "heart_rate_analysis_2026-01-01_2026-01-01.csv"
            self.assertTrue(output_path.is_file())
            with output_path.open(encoding="utf-8-sig", newline="") as file:
                self.assertEqual(next(csv.DictReader(file))["user_id"], "person@example.com")

    def test_output_format_flags_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            heart_rate_analysis.main(["--csv", "--json"])

        self.assertEqual(raised.exception.code, 2)

    def test_multiple_periods_are_analyzed_into_one_file(self) -> None:
        def fake(user_id, age, start, end):
            return {"user_id": user_id, "start_date": start.isoformat(), "end_date": end.isoformat()}

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "analyze_user", side_effect=fake):
                heart_rate_analysis.main([
                    "--json", "--output-dir", directory,
                    "--period", "2026-01-05:2026-01-06",
                    "--period", "2026-01-01:2026-01-02",
                ])

            output_path = Path(directory) / "heart_rate_analysis_2026-01-01_2026-01-06_2periods.json"
            rows = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [(r["start_date"], r["end_date"]) for r in rows],
                [("2026-01-01", "2026-01-02"), ("2026-01-05", "2026-01-06")],
            )

    def test_multiple_users_are_grouped_with_periods_in_chronological_order(self) -> None:
        def fake(user_id, age, start, end):
            return {
                "user_id": user_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                heart_rate_analysis,
                "USER_AGES",
                {"first@example.com": 40, "second@example.com": 41},
            ), patch.object(heart_rate_analysis, "analyze_user", side_effect=fake):
                heart_rate_analysis.main([
                    "--json",
                    "--output-dir",
                    directory,
                    "--period",
                    "2026-01-05:2026-01-06",
                    "--period",
                    "2026-01-01:2026-01-02",
                ])

            output_path = (
                Path(directory)
                / "heart_rate_analysis_2026-01-01_2026-01-06_2periods.json"
            )
            rows = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [(row["user_id"], row["start_date"]) for row in rows],
            [
                ("first@example.com", "2026-01-01"),
                ("first@example.com", "2026-01-05"),
                ("second@example.com", "2026-01-01"),
                ("second@example.com", "2026-01-05"),
            ],
        )

    def test_configured_periods_are_used_without_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}), \
                    patch.object(heart_rate_analysis, "PERIODS",
                                 [("2026-01-01", "2026-01-01"), ("2026-02-01", "2026-02-01")]), \
                    patch.object(heart_rate_analysis, "OUTPUT_DIR", Path(directory)), \
                    patch.object(heart_rate_analysis, "analyze_user",
                                 return_value={"user_id": "x"}) as analyze:
                heart_rate_analysis.main([])

            self.assertEqual(analyze.call_count, 2)

    def test_invalid_or_overlapping_periods_are_rejected(self) -> None:
        with patch.object(heart_rate_analysis, "USER_AGES", {"person@example.com": 40}):
            for periods in (
                ["2026-01-01:2026-01-05", "2026-01-01:2026-01-05"],
                ["2026-01-01"],
                ["2026-01-05:2026-01-01"],
            ):
                argv = [arg for p in periods for arg in ("--period", p)]
                with self.assertRaises(ValueError):
                    heart_rate_analysis.main(argv)


if __name__ == "__main__":
    unittest.main()
