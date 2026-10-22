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

    def test_median_inputs_prefer_db(self):
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 10, 7, 12, 0, tzinfo=tz)
        db_starts = [now - timedelta(days=i, hours=14) for i in range(14)]
        db_ends = [now - timedelta(days=i, hours=6) for i in range(14)]

        with patch('storage_sqlite.get_sleep_times_for_window_db', return_value=(db_starts, db_ends)) as mock_db, \
                patch.object(fitbit_sleep, '_sleep_file_path') as mock_json:
            starts, ends = fitbit_sleep._get_sleep_times_for_window('user', now, 14)
            mock_db.assert_called_once()
            mock_json.assert_not_called()
            self.assertEqual(starts, db_starts)
            self.assertEqual(ends, db_ends)

    def test_save_sessions_to_disk_calls_upsert_db(self):
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 10, 7, 12, 0, tzinfo=tz)
        session = {
            'start': '2026-10-06T23:00:00+09:00',
            'end': '2026-10-07T06:00:00+09:00',
            'raw': {'isMainSleep': True},
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(fitbit_sleep, '_ensure_sleep_dir', return_value=directory), \
                    patch('storage_sqlite.upsert_sleep_sessions') as mock_upsert:
                fitbit_sleep._save_sessions_to_disk('user', [session], now, date_tag='2026-10-07')
                mock_upsert.assert_called_once()
                args, _ = mock_upsert.call_args
                self.assertEqual(args[0], 'user')
                self.assertEqual(args[2], '2026-10-07')
                self.assertEqual(len(args[1]), 1)
                self.assertEqual(args[1][0]['start'], '2026-10-06T23:00:00+09:00')


    def test_get_median_sleep_suppression_status(self):
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 10, 7, 23, 30, tzinfo=tz)
        db_starts = [datetime(2026, 10, 7 - i, 23, 0, tzinfo=tz) for i in range(7)]
        db_ends = [datetime(2026, 10, 7 - i + 1, 7, 0, tzinfo=tz) for i in range(7)]

        with patch('storage_sqlite.get_sleep_times_for_window_db', return_value=(db_starts, db_ends)):
            is_suppressed, info = fitbit_sleep.get_median_sleep_suppression_status('user', now, 7)
            self.assertTrue(is_suppressed)
            self.assertIsNotNone(info)
            self.assertEqual(info['median_start'], '23:00')
            self.assertEqual(info['median_end'], '07:00')
            self.assertEqual(info['starts_count'], 7)

            # Test daytime (not suppressed)
            day_now = datetime(2026, 10, 7, 14, 0, tzinfo=tz)
            is_suppressed_day, info_day = fitbit_sleep.get_median_sleep_suppression_status('user', day_now, 7)
            self.assertFalse(is_suppressed_day)
            self.assertIsNotNone(info_day)


    def test_resolve_is_main_sleep_priority_and_threshold(self):
        import storage_sqlite
        resolve = storage_sqlite.resolve_is_main_sleep
        self.assertTrue(resolve({'raw': {'isMainSleep': True}}, 30))
        self.assertFalse(resolve({'raw': {'isMainSleep': False}}, 600))
        self.assertFalse(resolve({'raw': {}}, 120))
        self.assertTrue(resolve({'raw': {}}, 121))
        self.assertFalse(resolve({'start': '2026-10-07T14:00:00+09:00', 'end': '2026-10-07T15:00:00+09:00'}))
        self.assertTrue(resolve({'start': '2026-10-06T23:00:00+09:00', 'end': '2026-10-07T06:00:00+09:00'}))

    def test_json_fallback_excludes_naps(self):
        tz = timezone(timedelta(hours=9))
        now = datetime(2026, 10, 7, 12, 0, tzinfo=tz)
        payload = {'sessions': [
            {'start': '2026-10-07T14:00:00+09:00', 'end': '2026-10-07T15:00:00+09:00', 'raw': {}},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'nap.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            with patch('storage_sqlite.get_sleep_times_for_window_db', return_value=([], [])), \
                    patch.object(fitbit_sleep, '_sleep_file_path', return_value=path):
                starts, ends = fitbit_sleep._get_sleep_times_for_window('user', now, 1)
        self.assertEqual(starts, [])
        self.assertEqual(ends, [])


if __name__ == '__main__':
    unittest.main()