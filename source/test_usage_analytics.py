import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usage_analytics import UsageAnalytics


TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 9, 26, 20, tzinfo=TZ)
SINCE = dt.datetime(2026, 9, 21, tzinfo=TZ)
STAMP = '2026-09-26T10:00:00Z'


def usage(inputs=100, outputs=10, cached=40, reasoning=3):
    return dict(input_tokens=inputs, output_tokens=outputs, cached_input_tokens=cached,
                reasoning_output_tokens=reasoning, total_tokens=inputs+outputs)


def meta(identity='root', parent=None, stamp='2026-09-20T00:00:00Z', **kwargs):
    payload = dict(id=identity, timestamp=stamp, session_id=parent or identity,
                   source='vscode', thread_source='subagent' if parent else 'user')
    if parent:
        payload.update(parent_thread_id=parent, forked_from_id=parent,
                       agent_path='/root/'+identity)
    payload.update(kwargs)
    return dict(type='session_meta', timestamp=stamp, payload=payload)


def context(turn='turn-1', model='gpt-6-astra', effort='ultra'):
    return dict(type='turn_context', timestamp=STAMP,
                payload=dict(turn_id=turn, model=model, effort=effort,
                             developer_instructions='MUST NEVER BE RETAINED'))


def modern(response='response-1', thread='root', turn='turn-1', values=None, stamp=STAMP, root='root'):
    return dict(type='token_usage_record', timestamp=stamp, payload=dict(
        response_id=response, thread_id=thread, session_id=root, turn_id=turn,
        root_turn_id='root-turn', usage=values or usage(), thread_token_usage=usage()))


def cumulative(values=None, last=None, stamp=STAMP):
    values = values or usage()
    return dict(type='event_msg', timestamp=stamp, payload=dict(type='token_count', info=dict(
        total_token_usage=values, last_token_usage=last or values)))


class UsageAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home/'sessions/root.jsonl'
        self.reader = UsageAnalytics(self.home)

    def write(self, records, path=None, mode='w'):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding='utf8') as stream:
            for record in records:
                stream.write(json.dumps(record)+'\n')
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        return path

    def read(self, **kwargs):
        return self.reader.read(now=kwargs.get('now', NOW), since=kwargs.get('since', SINCE))

    def test_modern_records_count_once_with_cumulative_notifications_and_copies(self):
        records = [meta(), context(), modern(), cumulative(), cumulative(stamp='2026-09-26T10:00:01Z')]
        self.write(records)
        self.write(records, self.home/'archived_sessions/copy.jsonl')
        result = self.read()
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual((event['total'], event['request_input'], event['role']), (110, 100, 'main'))
        self.assertEqual((event['model'], event['effort']), ('gpt-6-astra', 'ultra'))
        self.assertNotIn('MUST NEVER', repr(self.reader.files))

    def test_first_metadata_owns_child_and_inherited_metadata_does_not_replace_it(self):
        self.write([meta(), context(), modern()])
        self.write([meta('child', 'root', stamp='2026-09-26T09:00:00Z'), meta(),
                    context('child-turn', 'gpt-6-luna', 'max'),
                    modern('child-response', 'child', 'child-turn')], self.home/'sessions/child.jsonl')
        result = self.read()
        child = next(e for e in result['events'] if e['session_id'] == 'child')
        self.assertEqual((child['root_id'], child['parent_id'], child['role']), ('root', 'root', 'subagent'))
        self.assertEqual((child['model'], child['effort']), ('gpt-6-luna', 'max'))

    def test_fork_inherited_tokens_are_excluded_even_when_parent_file_is_absent(self):
        self.write([meta('child', 'root', stamp='2026-09-26T09:00:00Z'), meta(), context(),
                    modern(stamp='2026-09-26T08:00:00Z'),
                    cumulative(stamp='2026-09-26T08:00:01Z'),
                    context('child-turn'), modern('new', 'child', 'child-turn')])
        result = self.read()
        self.assertEqual([e['session_id'] for e in result['events']], ['child'])
        self.assertEqual(result['events'][0]['root_id'], 'root')

    def test_legacy_fork_baseline_and_reset_do_not_recount_parent_history(self):
        self.write([meta('child', 'root', stamp='2026-09-26T09:00:00Z'), context(),
                    cumulative(usage(1000, 100), stamp='2026-09-26T08:00:00Z'),
                    cumulative(usage(1050, 105), last=usage(50, 5, 0, 0))])
        events = self.read()['events']
        self.assertEqual(sum(e['total'] for e in events), 55)

    def test_multi_level_agent_tree_uses_root_hint_when_root_file_is_old(self):
        self.write([meta('child', 'root'), context('ct'), modern('c', 'child', 'ct')])
        self.write([meta('grandchild', 'child', session_id='root'), context('gt'),
                    modern('g', 'grandchild', 'gt')], self.home/'sessions/grandchild.jsonl')
        result = self.read()
        self.assertEqual(result['sessions']['grandchild']['root_id'], 'root')
        self.assertTrue(all(e['role'] == 'subagent' for e in result['events']))

    def test_parent_source_fallback_and_user_fork_are_distinct(self):
        child_meta = meta('child', source={'subagent': {'thread_spawn': {
            'parent_thread_id': 'root', 'agent_path': '/root/child'}}})
        self.write([child_meta, context(), modern(thread='child')])
        self.write([meta('fork', forked_from_id='root', session_id='root'), context('fork-turn'),
                    modern('f', 'fork', 'fork-turn', root='fork')], self.home/'sessions/fork.jsonl')
        result = self.read()
        self.assertEqual(result['sessions']['child']['parent_id'], 'root')
        self.assertEqual(result['sessions']['fork']['root_id'], 'fork')
        self.assertEqual(result['sessions']['fork']['role'], 'main')

    def test_missing_metadata_and_model_remain_unknown(self):
        self.write([modern(), cumulative()])
        result = self.read()
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual(event['session_id'], 'root')
        self.assertEqual(event['role'], 'unknown')
        self.assertIsNone(event['model'])
        self.assertIsNone(event['effort'])

    def test_incremental_read_only_parses_appended_complete_records(self):
        self.write([meta(), context(), modern()])
        self.read()
        with patch.object(self.reader, '_record', wraps=self.reader._record) as parse:
            self.read()
            parse.assert_not_called()
            self.write([modern('two', values=usage(200, 20))], mode='a')
            result = self.read()
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(sum(e['total'] for e in result['events']), 330)

    def test_unfinished_line_is_retried_and_completed_malformed_line_marks_partial(self):
        self.write([meta(), context()])
        raw = json.dumps(modern()).encode()
        with self.path.open('ab') as stream:
            stream.write(raw[:50])
        self.assertEqual(self.read()['events'], [])
        with self.path.open('ab') as stream:
            stream.write(raw[50:]+b'\nnot-json\n')
        result = self.read()
        self.assertEqual(len(result['events']), 1)
        self.assertTrue(result['partial'])

    def test_truncate_and_larger_in_place_rewrite_replace_cached_events(self):
        self.write([meta(), context(), modern('one'), modern('two')])
        self.assertEqual(len(self.read()['events']), 2)
        self.write([meta(), context(), modern('replacement', values=usage(2, 1, 0, 0))])
        self.assertEqual(self.read()['events'][0]['total'], 3)
        self.write([meta(), context(), modern('three'), modern('four'), modern('five')])
        self.assertEqual(sum(e['total'] for e in self.read()['events']), 330)

    def test_model_and_effort_follow_the_actual_turn(self):
        self.write([meta(), context(), modern(), context('turn-2', 'gpt-6-sol', 'high'),
                    modern('two', turn='turn-2')])
        self.assertEqual([(e['model'], e['effort']) for e in self.read()['events']],
                         [('gpt-6-astra', 'ultra'), ('gpt-6-sol', 'high')])

    def test_cumulative_first_baseline_gap_uses_last_not_lifetime(self):
        self.write([meta(), context(), cumulative(usage(1000, 100), last=usage(20, 5, 10, 2))])
        result = self.read()
        self.assertEqual((result['events'][0]['total'], result['events'][0]['request_input']), (25, 20))
        self.assertTrue(any('基线' in note for note in result['notes']))
        self.assertTrue(result['partial'])
        self.assertEqual(len(result['issues']), 1)

    def test_cumulative_delta_matching_last_is_one_request_but_mismatch_is_unknown(self):
        self.write([meta(), context(), cumulative(usage(100, 10, 40, 3)),
                    cumulative(usage(150, 15, 60, 5), last=usage(50, 5, 20, 2),
                               stamp='2026-09-26T10:01:00Z'),
                    cumulative(usage(250, 25, 90, 8), last=usage(40, 4, 10, 1),
                               stamp='2026-09-26T10:02:00Z')])
        events = self.read()['events']
        self.assertEqual([e['total'] for e in events], [110, 55, 110])
        self.assertEqual([e['request_input'] for e in events], [100, 50, None])

    def test_modern_preference_is_per_turn_not_entire_file_and_handles_different_totals(self):
        self.write([meta(), context('old'), cumulative(), context('new'),
                    modern(turn='new'), cumulative(usage(9999, 99), last=usage(9899, 89),
                                                  stamp='2026-09-26T10:00:01Z')])
        self.assertEqual(sum(e['total'] for e in self.read()['events']), 220)

    def test_late_modern_record_replaces_fallback_instead_of_adding_to_it(self):
        self.write([meta(), context(), cumulative()])
        self.assertEqual(len(self.read()['events']), 1)
        self.write([modern()], mode='a')
        result = self.read()
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['events'][0]['total'], 110)

    def test_since_uses_exact_timezone_boundary_and_keeps_earlier_baseline(self):
        self.write([meta(), context(), cumulative(usage(100, 10), stamp='2026-09-20T15:59:59Z'),
                    cumulative(usage(150, 15), last=usage(50, 5, 0, 0), stamp='2026-09-20T16:00:00Z'),
                    cumulative(usage(500, 50), stamp='2026-09-27T00:00:00Z')])
        self.assertEqual([e['total'] for e in self.read()['events']], [55])

    def test_missing_and_empty_log_directories_are_distinct(self):
        self.assertFalse(self.read()['available'])
        self.path.parent.mkdir()
        result = self.read()
        self.assertTrue(result['available'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['events'], [])

    def test_invalid_counter_and_relation_cycle_are_partial(self):
        invalid = modern('bad', values=usage())
        invalid['payload']['usage']['input_tokens'] = True
        self.write([meta('a', 'b'), context(), modern('a-response', 'a'), invalid])
        self.write([meta('b', 'a'), context(), modern('b-response', 'b')], self.home/'sessions/b.jsonl')
        result = self.read()
        self.assertTrue(result['partial'])
        self.assertTrue(all(e['root_id'] is None and e['role'] == 'unknown' for e in result['events']))

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            self.reader.read(now=NOW.replace(tzinfo=None), since=SINCE)

    def test_titles_come_only_from_valid_index_metadata_and_last_entry_wins(self):
        identity = '01a0dd37-75a9-7661-b22f-0436bcdca24f'
        self.write([meta(identity), context(), modern(thread=identity)])
        self.write([{'id': identity, 'thread_name': 'First title'},
                    {'id': 'not-a-uuid', 'thread_name': 'Invalid'},
                    {'id': identity, 'thread_name': 'Updated title', 'ignored': 'SECRET'},
                    {'id': identity, 'thread_name': 42}], self.home/'session_index.jsonl')
        result = self.read()
        self.assertEqual(result['sessions'][identity]['title'], 'Updated title')
        self.assertNotIn('SECRET', repr(self.reader.__dict__))

    def test_unchanged_title_index_is_not_reopened_and_updates_invalidate_cache(self):
        identity = '01a0dd37-75a9-7661-b22f-0436bcdca24f'
        self.write([meta(identity), context(), modern(thread=identity)])
        index = self.write([{'id': identity, 'thread_name': 'First'}], self.home/'session_index.jsonl')
        self.read()
        original_open = Path.open
        index_reads = []

        def tracked_open(path, *args, **kwargs):
            if path == index:
                index_reads.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', tracked_open):
            self.assertEqual(self.read()['sessions'][identity]['title'], 'First')
            self.assertEqual(index_reads, [])
            self.write([{'id': identity, 'thread_name': 'Renamed title'}], index)
            index_reads.clear()
            self.assertEqual(self.read()['sessions'][identity]['title'], 'Renamed title')
            self.assertEqual(len(index_reads), 1)

    def test_missing_and_partial_title_index_do_not_change_token_availability(self):
        identity = '01a0dd37-75a9-7661-b22f-0436bcdca24f'
        self.write([meta(identity), context(), modern(thread=identity)])
        self.assertIsNone(self.read()['sessions'][identity]['title'])
        index = self.home/'session_index.jsonl'
        index.write_bytes(b'{"id":')
        result = self.read()
        self.assertIsNone(result['sessions'][identity]['title'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['events'][0]['total'], 110)

    def test_missing_or_impossible_cached_input_is_unknown_not_zero(self):
        one = modern('missing')
        del one['payload']['usage']['cached_input_tokens']
        two = modern('impossible')
        two['payload']['usage']['cached_input_tokens'] = 101
        three = modern('negative')
        three['payload']['usage']['cached_input_tokens'] = -1
        self.write([meta(), context(), one, two, three])
        result = self.read()
        self.assertEqual([e['cached'] for e in result['events']], [None, None, None])
        self.assertEqual(sum(e['total'] for e in result['events']), 330)
        self.assertTrue(any('缓存' in note for note in result['notes']))

    def test_legacy_missing_cached_baseline_does_not_turn_unknown_cache_into_a_delta(self):
        first = cumulative()
        del first['payload']['info']['total_token_usage']['cached_input_tokens']
        second = cumulative(usage(150, 15, 70, 5), last=usage(50, 5, 30, 2),
                            stamp='2026-09-26T10:01:00Z')
        self.write([meta(), context(), first, second])
        events = self.read()['events']
        self.assertIsNone(events[0]['cached'])
        self.assertIsNone(events[1]['cached'])

    def test_copied_legacy_fork_counters_are_not_charged_to_parent_and_child(self):
        older = cumulative(stamp='2026-09-26T08:00:00Z')
        self.write([meta(), context(), older])
        self.write([meta('child', 'root', stamp='2026-09-26T09:00:00Z'), meta(), context(), older,
                    cumulative(usage(150, 15, 60, 5), last=usage(50, 5, 20, 2))],
                   self.home/'sessions/child.jsonl')
        events = self.read()['events']
        self.assertEqual(sum(e['total'] for e in events), 165)
        self.assertEqual({e['session_id']: e['total'] for e in events}, {'root': 110, 'child': 55})

    def test_missing_root_log_can_still_receive_its_index_title(self):
        root = '01a0dd37-75a9-7661-b22f-0436bcdca24f'
        self.write([meta('child', root), context(), modern(thread='child', root=root)])
        self.write([{'id': root, 'thread_name': 'Indexed task'}], self.home/'session_index.jsonl')
        result = self.read()
        self.assertEqual(result['sessions'][root]['title'], 'Indexed task')
        self.assertEqual(result['sessions'][root]['role'], 'unknown')

    def test_current_baseline_gap_disables_forecast_instead_of_claiming_a_complete_period(self):
        from usage_costs import build_report
        self.write([meta(stamp='2026-09-26T09:00:00Z'), context(),
                    cumulative(usage(1000, 100), last=usage(20, 5, 10, 2))])
        result = self.read()
        self.assertEqual(result['events'][0]['total'], 25)
        self.assertTrue(result['partial'])
        report = build_report(result, '2026-09-21', NOW)
        self.assertTrue(report['partial'])
        self.assertIsNone(report['projected_cost'])
        self.assertIsNone(report['projected_known_cost'])

    def test_baseline_gap_before_window_does_not_make_current_period_partial(self):
        self.write([meta(), context(),
                    cumulative(usage(1000, 100, 400, 30), last=usage(20, 5, 10, 2),
                               stamp='2026-09-20T12:00:00Z'),
                    cumulative(usage(1050, 105, 420, 32), last=usage(50, 5, 20, 2))])
        result = self.read()
        self.assertEqual([e['total'] for e in result['events']], [55])
        self.assertFalse(result['partial'])
        self.assertEqual(result['issues'], [])
        self.assertFalse(any('基线' in note for note in result['notes']))

    def test_broader_seven_day_scan_keeps_issue_time_for_cycle_specific_forecast(self):
        from usage_costs import build_report
        self.write([meta(), context(),
                    cumulative(usage(1000, 100, 400, 30), last=usage(20, 5, 10, 2),
                               stamp='2026-09-20T12:00:00Z'),
                    cumulative(usage(1050, 105, 420, 32), last=usage(50, 5, 20, 2))])
        result = self.read(since=SINCE-dt.timedelta(days=1))
        self.assertTrue(result['partial'])
        self.assertLess(result['issues'][0]['timestamp'], SINCE.timestamp())
        report = build_report(result, '2026-09-21', NOW)
        self.assertFalse(report['partial'])
        self.assertEqual(report['total_tokens'], 55)
        self.assertIsNotNone(report['projected_cost'])

    def test_invalid_usage_timestamps_are_global_issues_not_silent_zero(self):
        for record in (modern(stamp='not-a-date'), cumulative(stamp='not-a-date')):
            with self.subTest(kind=record['type']):
                self.write([meta(), context(), record])
                result = self.read()
                self.assertTrue(result['partial'])
                self.assertEqual(result['events'], [])
                self.assertEqual(result['issues'][0]['timestamp'], None)
                self.assertTrue(any('时间' in n for n in result['notes']))

    def test_unrelated_invalid_timestamp_is_not_a_token_issue(self):
        self.write([meta(), context(), {'type': 'response_item', 'timestamp': 'invalid',
                                       'payload': {'type': 'message', 'content': 'not retained'}}, modern()])
        result = self.read()
        self.assertFalse(result['partial'])
        self.assertEqual(result['events'][0]['total'], 110)

    def test_inconsistent_total_and_components_are_rejected_for_both_sources(self):
        for record in (modern(), cumulative()):
            with self.subTest(kind=record['type']):
                if record['type'] == 'token_usage_record':
                    record['payload']['usage']['total_tokens'] = 999
                else:
                    record['payload']['info']['total_token_usage']['total_tokens'] = 999
                self.write([meta(), context(), record])
                result = self.read()
                self.assertTrue(result['partial'])
                self.assertEqual(result['events'], [])
                self.assertIsInstance(result['issues'][0]['timestamp'], float)

    def test_old_invalid_count_does_not_poison_a_new_complete_window(self):
        invalid = modern('old', stamp='2026-09-20T12:00:00Z')
        invalid['payload']['usage']['total_tokens'] = 1
        self.write([meta(), context(), invalid, modern('new')])
        result = self.read()
        self.assertFalse(result['partial'])
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['issues'], [])

    def test_preferred_modern_turn_suppresses_only_legacy_baseline_warning(self):
        self.write([meta(), context(), modern(),
                    cumulative(usage(1000, 100), last=usage())])
        result = self.read()
        self.assertFalse(result['partial'])
        self.assertEqual(result['events'][0]['total'], 110)
        invalid = modern('missing-response', stamp='invalid')
        self.write([invalid], mode='a')
        self.assertTrue(self.read()['partial'], 'An invalid preferred-source record was suppressed')

    def test_file_read_failure_has_unknown_scope_and_marks_partial(self):
        self.write([meta(), context(), modern()])
        original_open = Path.open

        def deny_usage(path, *args, **kwargs):
            if path == self.path:
                raise OSError('synthetic read failure')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', deny_usage):
            result = self.read()
        self.assertTrue(result['partial'])
        self.assertTrue(any(issue['timestamp'] is None for issue in result['issues']))

    def test_gpt55_prior_long_request_marks_current_short_request_uncertain(self):
        for model in ('gpt-5.5', 'gpt-5.5-2026-04-23'):
            with self.subTest(model=model):
                self.write([meta(), context('old', model, 'high'),
                            modern('old', turn='old', values=usage(272001, 10, 0),
                                   stamp='2026-09-20T12:00:00Z'),
                            context('new', model, 'high'), modern('new', turn='new')])
                result = self.read()
                self.assertEqual([e['request_input'] for e in result['events']], [100])
                self.assertTrue(result['sessions']['root']['gpt55_price_uncertain'])
                self.assertFalse(result['partial'])

    def test_gpt55_complete_short_history_is_explicitly_certified(self):
        self.write([meta(), context(model='gpt-5.5'), modern()])
        result = self.read()
        self.assertIs(result['sessions']['root']['gpt55_price_uncertain'], False)
        self.write([context('next', model='gpt-5.5'), modern('next', turn='next')], mode='a')
        self.assertIs(self.read()['sessions']['root']['gpt55_price_uncertain'], False)

    def test_gpt55_missing_metadata_is_not_certified(self):
        self.write([context(model='gpt-5.5'), modern()])
        self.assertIs(self.read()['sessions']['root']['gpt55_price_uncertain'], True)

    def test_gpt55_old_baseline_gap_stays_uncertain_without_poisoning_current_scope(self):
        self.write([meta(), context(model='gpt-5.5'),
                    cumulative(usage(1000, 100, 400, 30), last=usage(20, 5, 10, 2),
                               stamp='2026-09-20T12:00:00Z'),
                    cumulative(usage(1050, 105, 420, 32), last=usage(50, 5, 20, 2))])
        result = self.read()
        self.assertFalse(result['partial'])
        self.assertTrue(result['sessions']['root']['gpt55_price_uncertain'])

    def test_gpt55_unknown_historical_request_input_keeps_session_uncertain(self):
        self.write([meta(), context(model='gpt-5.5'),
                    cumulative(usage(100, 10, 40, 3), stamp='2026-09-20T10:00:00Z'),
                    cumulative(usage(200, 20, 80, 6), last=usage(20, 2, 0, 1),
                               stamp='2026-09-20T12:00:00Z'),
                    cumulative(usage(250, 25, 100, 8), last=usage(50, 5, 20, 2))])
        result = self.read()
        self.assertTrue(result['sessions']['root']['gpt55_price_uncertain'])
        self.assertEqual(result['events'][0]['request_input'], 50)

    def test_gpt55_unknown_historical_model_and_parse_failure_remain_uncertain(self):
        self.write([meta(), context('old', None, None),
                    modern('old', turn='old', stamp='2026-09-20T12:00:00Z'),
                    context('new', 'gpt-5.5', 'high'), modern('new', turn='new')])
        self.assertTrue(self.read()['sessions']['root']['gpt55_price_uncertain'])
        self.write([meta(), context(model='gpt-5.5'), modern()])
        with self.path.open('ab') as stream:
            stream.write(b'not-json\n')
        self.assertTrue(self.read()['sessions']['root']['gpt55_price_uncertain'])


if __name__ == '__main__':
    unittest.main()
