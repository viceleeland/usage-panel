import datetime as dt
import json
import unittest
from unittest.mock import patch

import usage_reconciliation as reconcile


UTC = dt.timezone.utc
LOCAL = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 2, 12, tzinfo=LOCAL)


def event(when, total=100, cached=40, **extra):
    return {'timestamp': dt.datetime.fromisoformat(when).timestamp(),
            'input': total-10, 'output': 10, 'total': total, 'cached': cached, **extra}


class ReconciliationTests(unittest.TestCase):
    def build(self, events=None, official=None, now=NOW, **snapshot_fields):
        snapshot = {'available': True, 'partial': False, 'events': events or [],
                    'updated': now.timestamp(), **snapshot_fields}
        with patch.object(reconcile, '_local_date',
                          side_effect=lambda stamp: dt.datetime.fromtimestamp(stamp, LOCAL).date()):
            return reconcile.build_reconciliation(snapshot, official or {}, now)

    def test_utc_yesterday_includes_local_today_before_eight_am(self):
        records = [event('2026-10-01T01:00:00+08:00', 100),
                   event('2026-10-01T12:00:00+08:00', 200),
                   event('2026-10-02T01:00:00+08:00', 300)]
        official = {'ok': True, 'buckets': [{'date': '2026-10-01', 'total': 500}]}
        report = self.build(records, official)
        target = report['target']
        self.assertEqual((target['local_total'], target['utc_total']), (300, 500))
        self.assertEqual((target['official_minus_local'], target['official_minus_utc']), (200, 0))
        self.assertEqual(report['daily']['local'][-1]['event_count'], 2)
        self.assertEqual(report['daily']['utc'][-1]['event_count'], 2)
        self.assertEqual(report['date_bases']['official'], 'reported_calendar_date_timezone_unspecified')

    def test_midnight_boundaries_are_inclusive_and_next_midnight_exclusive(self):
        records = [event('2026-10-01T00:00:00+08:00', 100),
                   event('2026-10-02T00:00:00+08:00', 200),
                   event('2026-10-01T00:00:00+00:00', 300),
                   event('2026-10-02T00:00:00+00:00', 400)]
        target = self.build(records)['target']
        self.assertEqual(target['local_total'], 400)
        self.assertEqual(target['utc_total'], 500)

    def test_empty_local_dates_stay_missing_but_explicit_official_zero_survives(self):
        report = self.build(official={'ok': True, 'buckets': [{'date': '2026-10-01', 'total': 0}]})
        self.assertIsNone(report['target']['local_total'])
        self.assertIsNone(report['target']['official_minus_local'])
        self.assertEqual(report['target']['official_total'], 0)
        self.assertFalse(report['target']['official_missing'])
        self.assertTrue(report['target']['local_missing'])
        self.assertEqual(report['daily']['local'][-1]['event_count'], 0)

    def test_unknown_cache_does_not_make_known_token_total_partial(self):
        report = self.build([event('2026-10-01T12:00:00+08:00', cached=None),
                             event('2026-10-01T13:00:00+08:00', cached=500)])
        row = report['daily']['local'][-1]
        self.assertEqual((row['input'], row['output'], row['total']), (180, 20, 200))
        self.assertIsNone(row['cached'])
        self.assertTrue(row['cache_unknown'])
        self.assertFalse(row['partial'])
        self.assertEqual(report['reason_counts']['unknown_cache'], 2)

    def test_existing_duplicate_events_are_not_deduplicated_again(self):
        item = event('2026-10-01T12:00:00+08:00')
        report = self.build([item, dict(item)])
        self.assertEqual(report['target']['local_total'], 200)
        self.assertEqual(report['daily']['local'][-1]['event_count'], 2)

    def test_invalid_events_are_rejected_and_mark_total_coverage_partial(self):
        records = [event('2026-10-01T12:00:00+08:00'), None,
                   {'timestamp': float('nan')}, {'timestamp': True},
                   event('2026-10-01T13:00:00+08:00', input=True),
                   event('2026-10-01T13:00:00+08:00', output=-1),
                   event('2026-10-02T13:00:00+08:00')]
        report = self.build(records)
        self.assertEqual(report['target']['local_total'], 100)
        self.assertTrue(report['target']['local_partial'])
        self.assertEqual(report['reason_counts']['invalid_timestamp'], 2)
        self.assertEqual(report['reason_counts']['invalid_counts'], 2)
        self.assertEqual(report['reason_counts']['future_event'], 1)

    def test_source_partial_and_issues_are_conservative_without_exposing_reasons(self):
        report = self.build([event('2026-10-01T12:00:00+08:00')],
            partial=True, issues=[{'timestamp': NOW.timestamp()-100000, 'reason': 'PRIVATE PATH'}])
        self.assertTrue(report['target']['local_partial'])
        self.assertTrue(report['target']['utc_partial'])
        self.assertEqual(report['reason_counts']['snapshot_issue'], 1)
        self.assertNotIn('PRIVATE PATH', json.dumps(report))

    def test_local_yesterday_can_still_be_an_open_utc_day(self):
        now = NOW.replace(hour=2)
        target = self.build([event('2026-10-01T12:00:00+08:00')], now=now)['target']
        self.assertTrue(target['local_day_closed'])
        self.assertFalse(target['utc_day_closed'])
        self.assertFalse(target['local_partial'])
        self.assertTrue(target['utc_partial'])

    def test_official_values_are_never_shifted_or_filled_from_local(self):
        report = self.build([event('2026-10-01T12:00:00+08:00')],
            {'ok': False, 'buckets': [{'date': '2026-09-30', 'total': 600},
                                    {'date': '2026-10-02', 'total': 700}]})
        self.assertIsNone(report['target']['official_total'])
        self.assertIsNone(report['target']['official_minus_local'])
        prior = report['daily']['official'][-2]
        self.assertEqual((prior['date'], prior['total'], prior['stale']), ('2026-09-30', 600, True))

    def test_range_is_capped_and_handles_year_boundary(self):
        now = dt.datetime(2027, 1, 1, 12, tzinfo=UTC)
        with patch.object(reconcile, '_local_date', side_effect=reconcile._utc_date):
            report = reconcile.build_reconciliation({}, {}, now, '2026-01-01')
            short = reconcile.build_reconciliation({}, {}, now, dt.date(2026, 12, 30))
        self.assertEqual(report['range'], {'start': '2026-12-25', 'end': '2026-12-31', 'days': 7, 'clipped': True})
        self.assertEqual(len(short['daily']['local']), 2)
        self.assertEqual(report['target_date'], '2026-12-31')

    def test_naive_now_and_invalid_or_future_start_are_rejected(self):
        with self.assertRaises(ValueError):
            reconcile.build_reconciliation({}, {}, NOW.replace(tzinfo=None))
        for start in ('2026-10-02', '2026-9-1', 42):
            with self.subTest(start=start), self.assertRaises(ValueError):
                reconcile.build_reconciliation({}, {}, NOW, start)

    def test_each_event_uses_its_own_system_date_rule(self):
        # Synthetic offset transition: historical events use a different offset
        # from now. The implementation must resolve each timestamp separately.
        before = dt.datetime(2026, 10, 1, 22, tzinfo=UTC).timestamp()
        after = dt.datetime(2026, 10, 2, 0, tzinfo=UTC).timestamp()
        now = dt.datetime(2026, 10, 3, 12, tzinfo=UTC)
        calls = []
        def local_day(stamp):
            calls.append(stamp)
            offset = dt.timezone(dt.timedelta(hours=2 if stamp < after else 0))
            return dt.datetime.fromtimestamp(stamp, offset).date()
        with patch.object(reconcile, '_local_date', side_effect=local_day):
            report = reconcile.build_reconciliation({'available': True, 'events': [
                {'timestamp': before, 'input': 90, 'output': 10, 'total': 100, 'cached': 0}]}, {}, now)
        self.assertIn(before, calls)
        self.assertEqual(report['target']['local_total'], 100)
        self.assertIsNone(report['target']['utc_total'])

    def test_output_is_a_closed_whitelist_not_a_copy_of_metadata(self):
        secret = 'NEVER_EXPORT_THIS'
        item = event('2026-10-01T12:00:00+08:00', session_id=secret, root_id=secret,
                     model=secret, title=secret, path=secret, content=secret)
        report = self.build([item], {'ok': True, 'note': secret, 'account': secret,
            'buckets': [{'date': '2026-10-01', 'total': 123, 'secret': secret}]},
            sessions={secret: {'title': secret}}, notes=[secret], updated=float('inf'))
        encoded = json.dumps(report, allow_nan=False)
        self.assertNotIn(secret, encoded)
        for key in ('session_id', 'root_id', 'model', 'title', 'path', 'content', 'sessions', 'notes'):
            self.assertNotIn('"'+key+'"', encoded)
        self.assertIsNone(report['query_times']['snapshot_updated_at'])


if __name__ == '__main__':
    unittest.main()
