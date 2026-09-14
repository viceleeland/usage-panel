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


def claude(identity, inputs=10, outputs=5, cache=20, creation=3, model='deepseek-v4-pro'):
    return {'timestamp': '2026-09-14T01:00:00Z', 'type': 'assistant', 'message': {
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
        self.assertEqual(self.counter.read(NOW)['codex']['total'], 35)
        tomorrow = NOW+dt.timedelta(days=1)
        self.write(self.path, [codex('2026-09-14T16:00:01Z', 150, 20)], 'a')
        os.utime(self.path, (tomorrow.timestamp(), tomorrow.timestamp()))
        self.assertEqual(self.counter.read(tomorrow)['codex']['total'], 25)

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
