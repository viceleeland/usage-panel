import copy
import datetime as dt
import unittest

from usage_view import analytics_partial_today, codex_daily_rows, retain_daily_usage


class TodayPartialTests(unittest.TestCase):
    now = dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    def test_yesterday_and_future_issues_do_not_hide_today_cache(self):
        for when in (self.now-dt.timedelta(days=1), self.now+dt.timedelta(days=1)):
            with self.subTest(when=when):
                self.assertFalse(analytics_partial_today(
                    {'partial': True, 'issues': [{'timestamp': when.timestamp()}]}, self.now))

    def test_today_issue_including_midnight_stays_partial(self):
        for when in (self.now, self.now.replace(hour=0)):
            with self.subTest(when=when):
                self.assertTrue(analytics_partial_today(
                    {'issues': [{'timestamp': when.timestamp()}]}, self.now))

    def test_unknown_time_issues_stay_partial(self):
        for stamp in (None, float('nan'), float('inf'), True, 'unknown'):
            with self.subTest(stamp=stamp):
                self.assertTrue(analytics_partial_today(
                    {'issues': [{'timestamp': stamp}]}, self.now))

    def test_legacy_partial_and_complete_metadata(self):
        self.assertTrue(analytics_partial_today({'partial': True}, self.now))
        self.assertFalse(analytics_partial_today({'partial': False}, self.now))
        self.assertFalse(analytics_partial_today({'partial': True, 'issues': []}, self.now))


class DailyRowsTests(unittest.TestCase):
    def local(self, date='2025-04-14', total=9000000, **fields):
        codex = dict(available=True, ok=True, partial=False, total=total)
        codex.update(fields)
        return {'date': date, 'codex': codex}

    def test_official_dates_and_totals_take_priority_over_local_history(self):
        official = {'ok': True, 'buckets': [
            {'date': '2025-04-12', 'total': 12000000},
            {'date': '2025-04-13', 'total': 24000000}]}
        local = self.local()
        local['history'] = [
            {'date': '2025-04-12', 'codex': {'total': 17000000}},
            {'date': '2025-04-13', 'codex': {'total': 19000000}}]
        rows = codex_daily_rows(official, local, '2025-04-14')
        self.assertEqual([row['date'] for row in rows], [
            '2025-04-08', '2025-04-09', '2025-04-10', '2025-04-11',
            '2025-04-12', '2025-04-13', '2025-04-14'])
        self.assertEqual(rows[4], {'date': '2025-04-12', 'total': 12000000,
                                  'source': 'official', 'ok': True, 'partial': False})
        self.assertEqual(rows[5]['total'], 24000000)
        self.assertEqual(rows[5]['source'], 'official')

    def test_missing_history_does_not_fall_back_to_local(self):
        local = self.local()
        local['history'] = [{'date': '2025-04-13', 'codex': {'total': 19000000}}]
        rows = codex_daily_rows({'ok': True, 'buckets': []}, local, '2025-04-14')
        self.assertEqual(rows[-2], {'date': '2025-04-13', 'total': None,
                                   'source': 'missing', 'ok': False, 'partial': False})
        self.assertEqual(rows[-1]['total'], 9000000)
        self.assertEqual(rows[-1]['source'], 'local')

    def test_today_local_fallback_requires_matching_date_and_available_data(self):
        for local in (None, self.local(date='2025-04-13'), self.local(available=False)):
            with self.subTest(local=local):
                row = codex_daily_rows(None, local, '2025-04-14')[-1]
                self.assertEqual(row['source'], 'missing')
                self.assertIsNone(row['total'])

    def test_explicit_official_zero_overrides_today_local_total(self):
        official = {'ok': True, 'buckets': [{'date': '2025-04-14', 'total': 0}]}
        row = codex_daily_rows(official, self.local(), '2025-04-14')[-1]
        self.assertEqual(row['total'], 0)
        self.assertEqual(row['source'], 'official')
        self.assertTrue(row['ok'])

    def test_stale_official_bucket_stays_official_and_stale(self):
        official = {'ok': False, 'buckets': [{'date': '2025-04-14', 'total': 4000000}]}
        row = codex_daily_rows(official, self.local(), '2025-04-14')[-1]
        self.assertEqual(row['total'], 4000000)
        self.assertEqual(row['source'], 'official')
        self.assertFalse(row['ok'])

    def test_partial_local_counter_remains_visibly_partial(self):
        row = codex_daily_rows(None, self.local(ok=False, partial=True), '2025-04-14')[-1]
        self.assertEqual(row['source'], 'local')
        self.assertEqual(row['total'], 9000000)
        self.assertFalse(row['ok'])
        self.assertTrue(row['partial'])

    def test_unknown_local_total_is_not_converted_to_zero(self):
        for total in (None, True, -1, 1.5, '100'):
            with self.subTest(total=total):
                row = codex_daily_rows(None, self.local(total=total), '2025-04-14')[-1]
                self.assertEqual(row['source'], 'missing')
                self.assertIsNone(row['total'])
        self.assertEqual(codex_daily_rows(None, self.local(total=0), '2025-04-14')[-1]['total'], 0)

    def test_week_crosses_month_and_year_boundary(self):
        rows = codex_daily_rows(None, None, '2027-01-03')
        self.assertEqual(rows[0]['date'], '2026-12-28')
        self.assertEqual(rows[-1]['date'], '2027-01-03')
        self.assertEqual(len(rows), 7)


class RetainDailyUsageTests(unittest.TestCase):
    def setUp(self):
        self.previous = {'ok': True, 'updated': 100, 'note': 'previous',
                         'buckets': [{'date': '2025-04-13', 'total': 24000000}]}

    def test_failure_retains_previous_data_and_timestamp_with_new_note(self):
        current = {'ok': False, 'updated': 200, 'buckets': [], 'note': 'refresh failed'}
        result = retain_daily_usage(self.previous, current)
        self.assertFalse(result['ok'])
        self.assertEqual(result['buckets'], self.previous['buckets'])
        self.assertEqual(result['updated'], 100)
        self.assertEqual(result['note'], 'refresh failed')

    def test_successful_empty_response_replaces_old_buckets(self):
        current = {'ok': True, 'updated': 200, 'buckets': [], 'note': 'no data'}
        self.assertEqual(retain_daily_usage(self.previous, current), current)

    def test_provider_failure_forces_stale_even_if_current_looks_successful(self):
        current = {'ok': True, 'updated': 200, 'note': 'provider failed',
                   'buckets': [{'date': '2025-04-14', 'total': 1}]}
        result = retain_daily_usage(self.previous, current, provider_ok=False)
        self.assertFalse(result['ok'])
        self.assertEqual(result['buckets'], self.previous['buckets'])
        self.assertEqual(result['updated'], 100)

    def test_failure_without_previous_data_stays_unavailable(self):
        result = retain_daily_usage(None, {'ok': False, 'note': 'unavailable'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['buckets'], [])
        self.assertIsNone(result['updated'])

    def test_retaining_does_not_mutate_inputs(self):
        current = {'ok': False, 'buckets': [], 'note': 'failed'}
        previous_copy, current_copy = copy.deepcopy(self.previous), copy.deepcopy(current)
        retain_daily_usage(self.previous, current)
        self.assertEqual(self.previous, previous_copy)
        self.assertEqual(current, current_copy)


if __name__ == '__main__':
    unittest.main()
