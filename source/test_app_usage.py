"""Exercise production usage methods without importing GUI dependencies.

The standard-library CI suite runs before Pillow / pystray are installed.
Compile the unchanged application methods and inject only the clock, widgets,
and thread runner; token parsing, source selection and pricing remain real.
"""
import ast
import datetime as dt
import json
import os
from pathlib import Path
import queue
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from token_usage import DailyTokens
from usage_analytics import UsageAnalytics
from usage_costs import build_report, enrich_pricing_events, monthly_cycle
from usage_view import analytics_partial_today, codex_daily_rows, latest_official_usage, retain_daily_usage


NOW = dt.datetime(2026, 10, 2, 12).astimezone()
TODAY = NOW.date().isoformat()


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else dt.datetime.fromtimestamp(NOW.timestamp())


class ImmediateThread:
    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


class Label:
    def __init__(self):
        self.text = ''

    def configure(self, *, text):
        self.text = text


def application_usage_class():
    source = Path(__file__).with_name('app.py')
    tree = ast.parse(source.read_text(encoding='utf8'), filename=str(source))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('token_text', 'cache_hit_text')]
    panel = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'UsagePanel')
    wanted = {'update_token_labels', 'update_refresh_status', 'codex_rows', 'refresh_tokens', 'poll'}
    panel.body = [node for node in panel.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in panel.body} == wanted
    namespace = dict(dt=SimpleNamespace(datetime=FixedDateTime, timedelta=dt.timedelta),
                     threading=SimpleNamespace(Thread=ImmediateThread), queue=queue, time=time,
                     monthly_cycle=monthly_cycle, build_report=build_report,
                     enrich_pricing_events=enrich_pricing_events,
                     analytics_partial_today=analytics_partial_today, codex_daily_rows=codex_daily_rows,
                     latest_official_usage=latest_official_usage, retain_daily_usage=retain_daily_usage)
    exec(compile(ast.Module(body=functions + [panel], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['UsagePanel']


UsagePanel = application_usage_class()


def local(total=12_000_000, **fields):
    return dict(available=True, ok=True, partial=False, total=total,
                input=10_000_000, cached=9_690_000, output=2_000_000) | fields


class AppUsageTests(unittest.TestCase):
    def setUp(self):
        self.panel = UsagePanel()
        self.panel.root = Mock()
        self.panel.token_labels = {'codex': Label(), 'deepseek': Label()}
        self.panel.local_token_labels = {'codex': Label()}
        self.panel.updated_label = Label()
        self.panel.result = {'codex': {'ok': True, 'daily_usage': {'ok': True, 'updated': 150,
            'buckets': [{'date': TODAY, 'total': 11_322_000}]}}}
        self.panel.tokens = {'date': TODAY, 'updated': 200, 'codex': local(), 'deepseek': local(2_000_000)}

    def labels(self):
        self.panel.update_token_labels()
        return self.panel.token_labels['codex'].text, self.panel.local_token_labels['codex'].text

    def test_primary_is_official_while_local_growth_and_cache_remain_separate(self):
        first, local_first = self.labels()
        self.panel.tokens['codex']['total'] = 18_000_000
        second, local_second = self.labels()
        self.assertEqual(first, second)
        self.assertIn(f'官方 {TODAY}  11.322m', second)
        self.assertIn('12.000m', local_first)
        self.assertIn('18.000m', local_second)
        self.assertIn('本机缓存命中 96.9%', local_second)
        self.assertNotIn('18.000m', second)
        self.assertEqual(self.panel.codex_rows()[-1]['total'], 11_322_000)
        self.assertIn('本机今日 2.000m', self.panel.token_labels['deepseek'].text)

    def test_explicit_zero_and_stale_official_values_are_not_replaced_by_local(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                daily = self.panel.result['codex']['daily_usage']
                daily.update(ok=ok, buckets=[{'date': TODAY, 'total': 0}])
                primary, secondary = self.labels()
                self.assertIn('0.000m', primary)
                self.assertEqual('旧数据' in primary, not ok)
                self.assertIn('12.000m', secondary)
                self.assertEqual(self.panel.codex_rows()[-1]['total'], 0)

    def test_official_date_before_or_after_local_today_stays_verbatim(self):
        for offset in (-1, 1):
            with self.subTest(offset=offset):
                date = (NOW.date()+dt.timedelta(days=offset)).isoformat()
                self.panel.result['codex']['daily_usage']['buckets'] = [{'date': date, 'total': 9_000_000}]
                primary, _ = self.labels()
                self.assertIn(f'官方 {date}  9.000m', primary)
                self.assertNotIn('今日', primary)
                row = next(row for row in self.panel.codex_rows() if row['date'] == date)
                self.assertEqual(row['source'], 'official')
                self.assertEqual(row['total'], 9_000_000)

    def test_missing_official_value_never_promotes_local_to_official(self):
        for daily in ({'ok': True, 'buckets': []}, {'ok': False, 'buckets': []}):
            with self.subTest(daily=daily):
                self.panel.result['codex']['daily_usage'] = daily
                primary, secondary = self.labels()
                self.assertIn('官方日用量 —', primary)
                self.assertNotIn('12.000m', primary)
                self.assertIn('12.000m', secondary)

    def test_local_missing_partial_zero_and_previous_day_do_not_change_official(self):
        cases = [({'available': False}, TODAY, '未找到本机记录'),
                 (local(partial=True), TODAY, '不完整'),
                 (local(0, input=0, cached=0, output=0), TODAY, '0.000m'),
                 (local(), (NOW.date()-dt.timedelta(days=1)).isoformat(), '正在读取')]
        for data, date, text in cases:
            with self.subTest(text=text):
                self.panel.tokens.update(date=date, codex=data)
                primary, secondary = self.labels()
                self.assertIn('11.322m', primary)
                self.assertIn(text, secondary)
                self.assertNotIn('96.9%', secondary)

    def test_daily_and_quota_read_times_are_independent_of_local_updates(self):
        self.panel.result['codex']['updated'] = 100
        self.labels()
        before = self.panel.updated_label.text
        self.panel.tokens['updated'] = 210
        self.labels()
        after = self.panel.updated_label.text
        self.assertNotEqual(before, after)
        for name, timestamp in (('额度', 100), ('官方日统计', 150), ('本机', 210)):
            self.assertIn(f'{name} {dt.datetime.fromtimestamp(timestamp):%H:%M:%S}', after)

    def test_poll_preserves_successful_daily_read_when_quota_refresh_fails(self):
        self.panel.result['codex'].update(cards=[{'name': 'Codex', 'windows': []}], updated=100,
                                          reset_credits={'count': 3})
        new_daily = {'ok': True, 'updated': 250, 'buckets': [{'date': TODAY, 'total': 42}]}
        self.panel.messages = queue.Queue()
        self.panel.messages.put(('result', {'codex': {'ok': False, 'cards': [], 'daily_usage': new_daily}}))
        self.panel.args = SimpleNamespace(smoke=False)
        self.panel.closed = False
        self.panel.save_cache = Mock()
        self.panel.process_alerts = Mock()
        self.panel.draw = Mock()
        self.panel.poll()
        account = self.panel.result['codex']
        self.assertEqual(account['daily_usage'], new_daily)
        self.assertFalse(account['ok'])
        self.assertEqual(account['updated'], 100)
        self.assertEqual(account['reset_credits']['count'], 3)
        self.assertEqual(account['cards'][0]['name'], 'Codex')


class AppUsageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        reader = DailyTokens(self.home, self.home/'claude')
        self.panel = UsagePanel()
        self.panel.closed = self.panel.token_busy = False
        self.panel.token_after_id = None
        self.panel.settings = {'billing_anchor': '2026-09-21'}
        self.panel.messages = queue.Queue()
        self.panel.token_reader = SimpleNamespace(
            read=lambda: reader.read(now=dt.datetime.fromtimestamp(NOW.timestamp())))
        self.panel.analytics_reader = UsageAnalytics(self.home)

    def write(self, records):
        path = self.home/'sessions/root.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(record)+'\n' for record in records), encoding='utf8')
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    def records(self, stamp=None, identity='response-1', turn='turn-1'):
        stamp = stamp or (NOW-dt.timedelta(hours=1)).isoformat()
        usage = dict(input_tokens=100, output_tokens=10, total_tokens=110,
                     cached_input_tokens=90, reasoning_output_tokens=3)
        return [dict(type='session_meta', timestamp=stamp, payload=dict(id='root', timestamp=stamp)),
                dict(type='turn_context', timestamp=stamp,
                     payload=dict(turn_id=turn, model='gpt-6.1-sol', effort='high')),
                dict(type='token_usage_record', timestamp=stamp, payload=dict(
                    thread_id='root', response_id=identity, turn_id=turn, usage=usage))]

    def run_worker(self):
        self.panel.refresh_tokens()
        messages = dict(self.panel.messages.queue)
        self.assertEqual(set(messages), {'tokens', 'analytics'})
        return messages['tokens'], messages['analytics'][1]

    def test_worker_prefers_actual_response_events_and_deduplicates_legacy_counter(self):
        records = self.records()
        records.append(dict(type='event_msg', timestamp=records[-1]['timestamp'], payload=dict(
            type='token_count', info=dict(total_token_usage=records[-1]['payload']['usage'],
                                         last_token_usage=records[-1]['payload']['usage']))))
        self.write(records)
        result, snapshot = self.run_worker()
        self.assertEqual(result['codex']['total'], 110)
        self.assertEqual(result['codex']['cached'], 90)
        self.assertTrue(result['codex']['ok'])
        self.assertEqual(result['history'][-1]['codex'], result['codex'])
        self.assertEqual(len(snapshot['events']), 1)

    def test_worker_uses_local_today_and_excludes_future_response_records(self):
        yesterday = NOW.replace(hour=0)-dt.timedelta(seconds=1)
        future = NOW+dt.timedelta(minutes=1)
        records = self.records(yesterday.isoformat(), 'yesterday', 'yesterday')
        records += self.records(identity='today', turn='today')[1:]
        records += self.records(future.isoformat(), 'future', 'future')[1:]
        self.write(records)
        result, snapshot = self.run_worker()
        self.assertEqual(result['codex']['total'], 110)
        self.assertEqual(len(snapshot['events']), 2)

    def test_worker_unknown_time_issue_remains_partial(self):
        records = self.records()
        records += self.records('invalid', 'invalid', 'invalid')[1:]
        self.write(records)
        result, snapshot = self.run_worker()
        self.assertEqual(result['codex']['total'], 110)
        self.assertTrue(result['codex']['partial'])
        self.assertFalse(result['codex']['ok'])
        self.assertTrue(any(issue['timestamp'] is None for issue in snapshot['issues']))

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires system timezone switching')
    def test_worker_partial_uses_actual_midnight_across_dst_and_repeated_hour(self):
        cases = [('2026-03-08T19:00:00+00:00', '2026-03-08T07:30:00Z', False),
                 ('2026-11-01T20:00:00+00:00', '2026-11-01T07:30:00Z', True),
                 # The second 01:30 must not become the first 01:30: the
                 # intervening 01:45 PDT issue has already happened.
                 ('2026-11-01T09:30:00+00:00', '2026-11-01T08:45:00Z', True)]
        try:
            with patch.dict(os.environ, {'TZ': 'America/Los_Angeles'}):
                time.tzset()
                for now, stamp, expected in cases:
                    with self.subTest(now=now), patch(__name__+'.NOW', dt.datetime.fromisoformat(now)):
                        self.panel.token_busy = False
                        self.panel.messages = queue.Queue()
                        self.panel.analytics_reader = UsageAnalytics(self.home)
                        records = self.records(stamp)
                        records[-1]['payload']['response_id'] = None
                        self.write(records)
                        result, snapshot = self.run_worker()
                        self.assertTrue(snapshot['partial'])
                        self.assertEqual(result['codex']['partial'], expected)
                        self.assertEqual(result['codex']['ok'], not expected)
        finally:
            time.tzset()

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires system timezone switching')
    def test_worker_singapore_and_shanghai_midnight_keep_same_epoch_boundary(self):
        try:
            for zone in ('Asia/Singapore', 'Asia/Shanghai'):
                with self.subTest(zone=zone), patch.dict(os.environ, {'TZ': zone}), \
                        patch(__name__+'.NOW', dt.datetime(2026, 10, 1, 16, 1, tzinfo=dt.timezone.utc)):
                    time.tzset()
                    self.panel.token_busy = False
                    self.panel.messages = queue.Queue()
                    self.panel.analytics_reader = UsageAnalytics(self.home)
                    records = self.records('2026-10-01T15:59:59Z', 'yesterday', 'yesterday')
                    records += self.records('2026-10-01T16:00:00Z', 'midnight', 'midnight')[1:]
                    records += self.records('2026-10-01T16:01:01Z', 'future', 'future')[1:]
                    self.write(records)
                    result, snapshot = self.run_worker()
                    self.assertEqual(result['date'], '2026-10-02')
                    self.assertEqual(result['codex']['total'], 110)
                    self.assertTrue(result['codex']['ok'])
                    self.assertEqual(len(snapshot['events']), 2)
        finally:
            time.tzset()

    def test_analytics_failure_never_exposes_legacy_complete_zero(self):
        self.write(self.records())
        self.panel.analytics_reader = SimpleNamespace(read=Mock(side_effect=OSError('synthetic failure')))
        result, snapshot = self.run_worker()
        self.assertFalse(result['codex']['available'])
        self.assertFalse(result['codex']['ok'])
        self.assertTrue(result['codex']['partial'])
        self.assertNotIn('total', result['codex'])
        self.assertEqual(result['history'][-1]['codex'], result['codex'])
        self.assertTrue(snapshot['partial'])

    def test_legacy_reader_failure_does_not_hide_successful_modern_usage(self):
        self.write(self.records())
        self.panel.token_reader = SimpleNamespace(read=Mock(side_effect=OSError('synthetic failure')))
        result, _ = self.run_worker()
        self.assertEqual(result['date'], TODAY)
        self.assertEqual(result['codex']['total'], 110)
        self.assertTrue(result['codex']['ok'])
        self.assertFalse(result['deepseek']['available'])


if __name__ == '__main__':
    unittest.main()
