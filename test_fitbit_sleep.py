import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fitbit_sleep


class SleepHistoryRefreshTests(unittest.TestCase):
    def test_refresh_runs_once_after_noon_for_14_dates(self):
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 10, 7, 11, 59, tzinfo=tz)
        session = {
            'start': '2026-10-06T23:00:00+09:00',
            'end': '2026-10-07T06:00:00+09:00',
            'raw': {},
        }

        with tempfile.TemporaryDirectory() as directory:
            marker_path = os.path.join(directory, '.refresh.json')
            with patch.object(fitbit_sleep, '_refresh_marker_path', return_value=marker_path), \
                    patch.object(fitbit_sleep, '_fetch_sleep_for_window', return_value=[session]) as fetch, \
                    patch.object(fitbit_sleep, '_sleep_file_path', side_effect=lambda _user, day: os.path.join(directory, f'{day}.json')), \
                    patch.object(fitbit_sleep, '_saved_file_has_sessions', return_value=False), \
                    patch.object(fitbit_sleep, '_save_sessions_to_disk') as save:
                self.assertFalse(fitbit_sleep.refresh_recent_sleep_history_if_due('user', now))
                fetch.assert_not_called()

                now = now.replace(hour=12, minute=0)
                self.assertTrue(fitbit_sleep.refresh_recent_sleep_history_if_due('user', now))
                fetch.assert_called_once()
                self.assertEqual(save.call_count, 14)
                saved_dates = {call.kwargs['date_tag'] for call in save.call_args_list}
                self.assertEqual(
                    saved_dates,
                    {(now.date() - timedelta(days=offset)).isoformat() for offset in range(14)},
                )
                self.assertEqual(
                    sum(bool(call.args[1]) for call in save.call_args_list),
                    1,
                )
                self.assertFalse(
                    fitbit_sleep.refresh_recent_sleep_history_if_due('user', now.replace(hour=13))
                )
                fetch.assert_called_once()

    def test_median_inputs_exclude_dates_older_than_14_calendar_days(self):
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 10, 7, 12, 0, tzinfo=tz)

        with tempfile.TemporaryDirectory() as directory:
            def sleep_path(_user_id, day):
                return os.path.join(directory, f'sleep_{day.isoformat()}.json')

            for offset in range(15):
                day = now.date() - timedelta(days=offset)
                start_hour = 12 if offset == 14 else 22
                end_hour = 14 if offset == 14 else 6
                end_day = day + timedelta(days=1) if end_hour < start_hour else day
                payload = {
                    'sessions': [{
                        'start': f'{day.isoformat()}T{start_hour:02d}:00:00+09:00',
                        'end': f'{end_day.isoformat()}T{end_hour:02d}:00:00+09:00',
                        'raw': {},
                    }],
                }
                with open(sleep_path('user', day), 'w', encoding='utf-8') as f:
                    json.dump(payload, f)

            with patch.object(fitbit_sleep, '_sleep_file_path', side_effect=sleep_path):
                starts, ends = fitbit_sleep._get_sleep_times_for_window('user', now, 14)

        self.assertEqual(len(starts), 14)
        self.assertEqual(len(ends), 14)
        self.assertTrue(all(start.hour == 22 for start in starts))
        self.assertTrue(all(end.hour == 6 for end in ends))


if __name__ == '__main__':
    unittest.main()