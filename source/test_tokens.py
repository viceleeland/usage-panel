import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from providers import normalize_credits
from token_usage import DailyTokens


NOW = dt.datetime(2026, 9, 14, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def codex(stamp, inputs, outputs, cached=0, last=None):
    total = dict(input_tokens=inputs, output_tokens=outputs,
                 cached_input_tokens=cached, total_tokens=inputs+outputs)
    return {'timestamp': stamp, 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': total, 'last_token_usage': last or total}}}


def claude(identity, inputs=10, outputs=5, cache=20, creation=3, model='deepseek-v4-pro',
           stamp='2026-09-14T01:00:00Z'):
    return {'timestamp': stamp, 'type': 'assistant', 'message': {
        'id': identity, 'model': model, 'usage': {'input_tokens': inputs,
        'output_tokens': outputs, 'cache_read_input_tokens': cache,
        'cache_creation_input_tokens': creation}}}


class DailyTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = DailyTokens(self.root/'codex', self.root/'claude')
        self.path = self.root/'codex/sessions/current.jsonl'
        self.deepseek = self.root/'claude/projects/project/current.jsonl'

    def write(self, path, records, mode='w'):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding='utf8') as stream:
            for record in records:
                stream.write(json.dumps(record)+'\n')
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    def test_incremental_reads_and_duplicate_notifications_and_copies(self):
        first = codex('2026-09-14T00:00:00Z', 100, 10, 40)
        self.write(self.path, [first, first])
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 110)
        with patch.object(self.counter, '_record', wraps=self.counter._record) as parse:
            self.assertEqual(self.counter.read(NOW)['codex']['total'], 110)
            parse.assert_not_called()
        second = codex('2026-09-14T00:01:00Z', 150, 20, 60)
        self.write(self.path, [second], 'a')
        copy = self.root/'codex/archived_sessions/copy.jsonl'
        self.write(copy, [first, first, second])
        result = self.counter.read(NOW)['codex']
        self.assertEqual((result['input'], result['output'], result['cached'], result['total']),
                         (150, 20, 60, 170))

    def test_local_midnight_uses_prior_cumulative_baseline(self):
        self.write(self.path, [codex('2026-09-13T15:59:59Z', 100, 10),
                               codex('2026-09-13T16:00:01Z', 130, 15)])
        result = self.counter.read(NOW)
        self.assertEqual(result['codex']['total'], 35)
        self.assertEqual(result['history'][-2]['codex']['total'], 110)
        tomorrow = NOW+dt.timedelta(days=1)
        self.write(self.path, [codex('2026-09-14T16:00:01Z', 150, 20)], 'a')
        os.utime(self.path, (tomorrow.timestamp(), tomorrow.timestamp()))
        result = self.counter.read(tomorrow)
        self.assertEqual(result['codex']['total'], 25)
        self.assertEqual(result['history'][-2]['codex']['total'], 35)
        self.assertEqual(result['history'][0]['date'], '2026-09-09')
        self.assertEqual(result['date'], result['history'][-1]['date'])
        self.assertEqual(result['codex'], result['history'][-1]['codex'])

    def test_history_has_seven_local_dates_with_cumulative_daily_deltas(self):
        self.write(self.path, [
            codex('2026-09-07T15:59:59Z', 50, 5, 20),
            codex('2026-09-07T16:00:00Z', 70, 7, 30),
            codex('2026-09-10T04:00:00Z', 100, 12, 50),
            codex('2026-09-14T04:00:00Z', 140, 18, 80),
            codex('2026-09-14T16:00:00Z', 1000, 100, 500)])
        self.write(self.deepseek, [claude('past', stamp='2026-09-10T04:00:00Z'),
                                   claude('today')])
        result = self.counter.read(NOW)
        self.assertEqual([day['date'] for day in result['history']],
                         [f'2026-09-{day:02}' for day in range(8, 15)])
        self.assertEqual([day['codex']['total'] for day in result['history']],
                         [22, 0, 35, 0, 0, 0, 46])
        self.assertEqual([day['deepseek']['total'] for day in result['history']],
                         [0, 0, 38, 0, 0, 0, 38])
        self.assertEqual(result['codex']['cached'], 30)
        self.assertEqual(result['deepseek'], result['history'][-1]['deepseek'])

    def test_history_loads_older_unchanged_files_once_and_excludes_old_mtime(self):
        self.write(self.path, [codex('2026-09-08T04:00:00Z', 10, 1)])
        old_time = (NOW-dt.timedelta(days=6)).timestamp()
        os.utime(self.path, (old_time, old_time))
        outside = self.path.with_name('before-window.jsonl')
        self.write(outside, [codex('2026-09-14T04:00:00Z', 900, 90)])
        older_time = (NOW-dt.timedelta(days=7)).timestamp()
        os.utime(outside, (older_time, older_time))
        result = self.counter.read(NOW)
        self.assertEqual(result['history'][0]['codex']['total'], 11)
        self.assertEqual(result['codex']['total'], 0)
        with patch.object(self.counter, '_record', wraps=self.counter._record) as parse:
            self.assertEqual(self.counter.read(NOW)['history'], result['history'])
            parse.assert_not_called()
        tomorrow = NOW+dt.timedelta(days=1)
        result = self.counter.read(tomorrow)
        self.assertTrue(all(day['codex']['total'] == 0 for day in result['history']))
        self.assertNotIn(self.path, self.counter.files)

    def test_deepseek_message_across_midnight_and_copies_belongs_to_first_day(self):
        self.write(self.deepseek, [claude('overnight', stamp='2026-09-13T15:59:59Z'),
                                   claude('overnight', outputs=8, stamp='2026-09-13T16:00:01Z')])
        self.write(self.deepseek.with_name('copy.jsonl'), [
            claude('overnight', outputs=12, stamp='2026-09-14T00:00:00Z'),
            claude('today')])
        result = self.counter.read(NOW)
        self.assertEqual(result['history'][-2]['deepseek']['total'], 45)
        self.assertEqual(result['deepseek']['total'], 38)
        self.write(self.deepseek, [claude('overnight', outputs=20)], 'a')
        result = self.counter.read(NOW)
        self.assertEqual(result['history'][-2]['deepseek']['total'], 53)
        self.assertEqual(result['deepseek']['total'], 38)

    def test_deepseek_first_message_date_is_independent_of_file_scan_order(self):
        self.write(self.deepseek, [claude('same', outputs=20)])
        self.write(self.deepseek.with_name('copy.jsonl'), [
            claude('same', stamp='2026-09-12T15:00:00Z')])
        result = self.counter.read(NOW)
        self.assertEqual(result['history'][4]['deepseek']['total'], 53)
        self.assertEqual(result['deepseek']['total'], 0)

    def test_message_started_before_history_is_not_counted_again_on_later_updates(self):
        self.write(self.deepseek, [claude('old', stamp='2026-09-07T15:59:59Z'),
                                   claude('old', outputs=20, stamp='2026-09-07T16:00:01Z')])
        result = self.counter.read(NOW)
        self.assertTrue(all(day['deepseek']['total'] == 0 for day in result['history']))

    def test_first_record_uses_last_usage_when_full_baseline_is_missing(self):
        self.write(self.path, [codex('2026-09-14T00:00:00Z', 1000, 100,
            last=dict(input_tokens=20, output_tokens=5, cached_input_tokens=10, total_tokens=25))])
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 25)

    def test_deepseek_deduplicates_message_ids_and_counts_all_cache_inputs(self):
        self.write(self.deepseek, [claude('a'), claude('a', outputs=8),
                                   claude('ignored', model='claude-sonnet-4')])
        self.write(self.deepseek.with_name('copy.jsonl'), [claude('a'), claude('b')])
        result = self.counter.read(NOW)['deepseek']
        self.assertEqual((result['input'], result['output'], result['cached'], result['total']),
                         (66, 13, 40, 79))

    def test_missing_directory_is_unavailable_instead_of_confirmed_zero(self):
        result = self.counter.read(NOW)
        for kind in ('codex', 'deepseek'):
            self.assertFalse(result[kind]['available'])
            self.assertFalse(result[kind]['ok'])
            for day in result['history']:
                self.assertFalse(day[kind]['available'])
                self.assertFalse(day[kind]['ok'])
        self.deepseek.parent.mkdir(parents=True)
        result = self.counter.read(NOW)['deepseek']
        self.assertTrue(result['ok'])
        self.assertEqual(result['total'], 0)

    def test_partial_line_is_retried_after_completion(self):
        self.path.parent.mkdir(parents=True)
        raw = json.dumps(codex('2026-09-14T00:00:00Z', 100, 10)).encode()
        self.path.write_bytes(raw[:80])
        os.utime(self.path, (NOW.timestamp(), NOW.timestamp()))
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 0)
        with self.path.open('ab') as stream:
            stream.write(raw[80:]+b'\n')
        os.utime(self.path, (NOW.timestamp(), NOW.timestamp()))
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 110)

    def test_truncated_log_replaces_old_events(self):
        self.write(self.path, [codex('2026-09-14T00:00:00Z', 1000, 100),
                               codex('2026-09-14T00:01:00Z', 2000, 200)])
        self.counter.read(NOW)
        self.write(self.path, [codex('2026-09-14T00:02:00Z', 10, 1)])
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 11)

    def test_larger_in_place_rewrite_replaces_old_events(self):
        self.write(self.path, [codex('2026-09-14T00:00:00Z', 10, 1)])
        self.counter.read(NOW)
        self.write(self.path, [codex('2026-09-14T00:01:00Z', 100, 10,
            last=dict(input_tokens=4, output_tokens=1, total_tokens=5)),
                               codex('2026-09-14T00:02:00Z', 120, 15)])
        result = self.counter.read(NOW)['codex']
        self.assertEqual(result['total'], 30)
        self.assertTrue(result['ok'])

    def test_same_size_rewrite_with_preserved_mtime_is_detected(self):
        self.write(self.path, [codex('2026-09-14T00:00:00Z', 10, 1)])
        self.counter.read(NOW)
        self.write(self.path, [codex('2026-09-14T00:01:00Z', 20, 2)])
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 22)

    def test_counter_reset_uses_last_usage_and_keeps_earlier_today_events(self):
        self.write(self.path, [codex('2026-09-14T00:00:00Z', 100, 10),
            codex('2026-09-14T00:01:00Z', 10, 1)])
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 121)

    def test_malformed_completed_line_stays_partial_until_log_replaced(self):
        self.write(self.path, [codex('2026-09-14T00:00:00Z', 10, 1)])
        with self.path.open('ab') as stream:
            stream.write(b'not json\n')
        os.utime(self.path, (NOW.timestamp(), NOW.timestamp()))
        for _ in range(2):
            result = self.counter.read(NOW)['codex']
            self.assertEqual(result['total'], 11)
            self.assertTrue(result['partial'])
            self.assertFalse(result['ok'])
        self.write(self.path, [codex('2026-09-14T00:01:00Z', 20, 2)])
        result = self.counter.read(NOW)['codex']
        self.assertEqual(result['total'], 22)
        self.assertTrue(result['ok'])
        self.assertFalse(result['partial'])


class CreditTests(unittest.TestCase):
    def test_authoritative_count_is_not_inferred_from_partial_list(self):
        result = normalize_credits({'availableCount': 3, 'credits': [
            {'status': 'available', 'expiresAt': 200},
            {'status': 'used', 'expiresAt': 50},
            {'status': 'available', 'expiresAt': 100}]})
        self.assertEqual(result, {'count': 3, 'expires': [100, 200]})

    def test_missing_count_remains_unknown_and_zero_remains_zero(self):
        for raw in (None, {}, {'availableCount': -1}, {'availableCount': True},
                    {'credits': [{'status': 'available', 'expiresAt': 100}]}):
            self.assertIsNone(normalize_credits(raw)['count'])
        self.assertEqual(normalize_credits({'availableCount': 0, 'credits': None})['count'], 0)


if __name__ == '__main__':
    unittest.main()
