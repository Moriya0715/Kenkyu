import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import data_collector
import storage_sqlite
from japan_time import JST


class WeeklyDateRangeTests(unittest.TestCase):
    def test_week_dates_start_on_configured_weekday_in_jst(self):
        now = datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)

        with patch.object(storage_sqlite, 'WEEK_START_WEEKDAY', 2):
            dates = storage_sqlite.get_dates_from_week_start(now)

        self.assertEqual(
            dates,
            [datetime(2026, 10, 28, tzinfo=JST).date(),
             datetime(2026, 10, 29, tzinfo=JST).date()],
        )

    def test_week_start_can_be_changed_to_sunday(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=JST)

        with patch.object(storage_sqlite, 'WEEK_START_WEEKDAY', 6):
            dates = storage_sqlite.get_dates_from_week_start(now)

        self.assertEqual(dates[0].weekday(), 6)
        self.assertEqual(dates[-1], now.date())

    def test_invalid_weekday_configuration_is_rejected(self):
        with patch.object(storage_sqlite, 'WEEK_START_WEEKDAY', 7):
            with self.assertRaises(ValueError):
                storage_sqlite.get_dates_from_week_start(
                    datetime(2026, 10, 29, tzinfo=JST)
                )


class CollectorStartupRangeTests(unittest.TestCase):
    def test_initial_run_fetches_week_start_through_today_for_all_data(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=JST)
        expected_dates = storage_sqlite.get_dates_from_week_start(now)
        with patch.object(data_collector, 'get_users', return_value=['user']), \
                patch.object(data_collector, 'now_jst', return_value=now), \
                patch.object(data_collector, 'collect_vitals') as collect_vitals, \
                patch.object(data_collector, 'collect_sleep') as collect_sleep, \
                patch.object(data_collector.google_calendar_freebusy, 'main') as collect_calendar:
            self.assertTrue(data_collector.run_once(initial_fetch=True))

        self.assertEqual(
            [call.args[1] for call in collect_vitals.call_args_list],
            expected_dates,
        )
        self.assertEqual(
            [call.kwargs['target_date'] for call in collect_calendar.call_args_list],
            expected_dates,
        )
        self.assertEqual(collect_sleep.call_args.args[2], expected_dates[0])

    def test_recurring_run_keeps_yesterday_and_today_range(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=JST)
        expected_dates = [now.date() - timedelta(days=1), now.date()]
        with patch.object(data_collector, 'get_users', return_value=['user']), \
                patch.object(data_collector, 'now_jst', return_value=now), \
                patch.object(data_collector, 'collect_vitals') as collect_vitals, \
                patch.object(data_collector, 'collect_sleep') as collect_sleep, \
                patch.object(data_collector.google_calendar_freebusy, 'main') as collect_calendar:
            self.assertTrue(data_collector.run_once())

        self.assertEqual(
            [call.args[1] for call in collect_vitals.call_args_list],
            expected_dates,
        )
        self.assertEqual(
            [call.kwargs['target_date'] for call in collect_calendar.call_args_list],
            expected_dates,
        )
        self.assertEqual(collect_sleep.call_args.args[2], expected_dates[0])


if __name__ == '__main__':
    unittest.main()
