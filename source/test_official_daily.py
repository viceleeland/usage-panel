"""Official totals retain their dates and never acquire invented breakdowns."""
import unittest
from usage_view import official_range_rows, official_usage_for_date, retain_daily_usage


class OfficialDailyTests(unittest.TestCase):
    def fixture(self, ok=True):
        return {'ok': ok, 'updated': 123, 'buckets': [
            {'date': '2026-10-02', 'total': 130000456},
            {'date': '2026-10-01', 'total': 340000123},
            {'date': '2026-09-30', 'total': 160000789}]}

    def test_same_date_exact_value_and_inclusive_range(self):
        rows = official_range_rows(self.fixture(), '2026-10-01', '2026-10-02')
        self.assertEqual([row['total'] for row in rows], [340000123, 130000456])
        self.assertEqual(official_usage_for_date(self.fixture(), '2026-10-01'), rows[0])
        self.assertEqual(official_usage_for_date(self.fixture()), rows[1])

    def test_missing_date_and_empty_official_stay_missing(self):
        self.assertIsNone(official_usage_for_date(self.fixture(), '2026-09-29'))
        self.assertEqual(official_range_rows({'ok': True, 'buckets': []}), [])
        self.assertIsNone(official_usage_for_date(None))

    def test_explicit_zero_is_known_without_invented_dimensions(self):
        official = {'ok': True, 'buckets': [{'date': '2026-10-03', 'total': 0}]}
        row = official_usage_for_date(official)
        self.assertEqual(row, {'date': '2026-10-03', 'total': 0, 'ok': True})
        self.assertTrue(all(key not in row for key in ('input', 'output', 'cached', 'model', 'cost')))

    def test_failed_refresh_retains_exact_date_value_and_old_read_time(self):
        old = self.fixture()
        merged = retain_daily_usage(old, {'ok': False, 'updated': 999, 'buckets': []})
        row = official_usage_for_date(merged, '2026-10-01')
        self.assertEqual(row['total'], 340000123)
        self.assertFalse(row['ok'])
        self.assertEqual(merged['updated'], 123)

    def test_calendar_date_is_not_shifted_by_local_day_or_offset(self):
        row = official_usage_for_date(self.fixture(), '2026-10-02')
        self.assertEqual(row['date'], '2026-10-02')
        self.assertEqual(official_range_rows(self.fixture(), '2026-10-01', '2026-10-01')[0]['date'], '2026-10-01')

    def test_malformed_cache_rows_do_not_become_zero_or_totals(self):
        official = {'ok': True, 'buckets': [None,
            {'date': '2026-10-1', 'total': 42}, {'date': '2026-10-01', 'total': True},
            {'date': '2026-10-02', 'total': -1}, {'date': '2026-10-03', 'total': 2.5}]}
        self.assertEqual(official_range_rows(official), [])


if __name__ == '__main__':
    unittest.main()
