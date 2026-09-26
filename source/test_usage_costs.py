import copy
import datetime as dt
import json
import unittest

from usage_costs import build_report, enrich_pricing_events, event_cost, monthly_cycle


TZ = dt.timezone(dt.timedelta(hours=8))


def moment(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=TZ)


def event(at='2026-09-22T00:00:00', **fields):
    result = dict(timestamp=moment(at).timestamp(), session_id='main', root_id='main',
                  parent_id=None, agent_path='/root', role='main', model='gpt-6-astra',
                  effort='ultra', input=100_000, cached=60_000, output=20_000,
                  reasoning=10_000, total=120_000, request_input=100_000,
                  session_price_uncertain=False)
    result.update(fields)
    return result


def snapshot(events=None, **fields):
    result = dict(available=True, partial=False, updated=100, events=events or [],
                  sessions={'main': {'id': 'main', 'agent_path': '/root'}}, notes=[])
    result.update(fields)
    return result


class CycleTests(unittest.TestCase):
    def test_twenty_first_boundary_and_previous_month(self):
        start, end = monthly_cycle('2026-09-21', moment('2026-09-21T00:00:00'))
        self.assertEqual(start, moment('2026-09-21T00:00:00'))
        self.assertEqual(end, moment('2026-10-21T00:00:00'))
        start, end = monthly_cycle('2026-09-21', moment('2026-09-20T23:59:59'))
        self.assertEqual(start.date().isoformat(), '2026-08-21')
        self.assertEqual(end.date().isoformat(), '2026-09-21')

    def test_original_31_anchor_recovers_after_february(self):
        start, end = monthly_cycle('2026-01-31', moment('2026-02-28T12:00:00'))
        self.assertEqual(start.date().isoformat(), '2026-02-28')
        self.assertEqual(end.date().isoformat(), '2026-03-31')
        start, end = monthly_cycle('2026-01-31', moment('2028-02-29T12:00:00'))
        self.assertEqual(start.date().isoformat(), '2028-02-29')
        self.assertEqual(end.date().isoformat(), '2028-03-31')

    def test_year_rollover_timezone_and_missing_anchor(self):
        start, end = monthly_cycle('2026-09-21', moment('2026-12-31T12:00:00'))
        self.assertEqual(end.date().isoformat(), '2027-01-21')
        self.assertEqual(start.utcoffset(), dt.timedelta(hours=8))
        self.assertEqual(monthly_cycle(None, moment('2026-09-26T00:00:00')), (None, None))

    def test_invalid_input_is_not_silently_reinterpreted(self):
        for anchor in ('2026-02-30', '20260921', '21', 21):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                monthly_cycle(anchor, moment('2026-09-26T00:00:00'))
        with self.assertRaises(ValueError):
            monthly_cycle('2026-09-21', dt.datetime(2026, 9, 26))


class EventCostTests(unittest.TestCase):
    def test_cached_input_and_reasoning_are_not_counted_twice(self):
        # 40K ordinary * $10 + 60K cache * $1 + 20K output * $50.
        self.assertAlmostEqual(event_cost(event()), 1.46)
        self.assertEqual(event_cost(event(reasoning=19_999, effort='low')), event_cost(event()))

    def test_unknown_models_and_unconfirmed_snapshot_are_unpriced(self):
        for model in (None, 'gpt-6-astra-2099-01-01', 'gpt-6-astra-ultra', 'other'):
            with self.subTest(model=model):
                self.assertIsNone(event_cost(event(model=model)))
        self.assertEqual(event_cost(event(model='gpt-5.5-2026-04-23')),
                         event_cost(event(model='gpt-5.5')))
        self.assertEqual(event_cost(event(model='gpt-5.6')),
                         event_cost(event(model='gpt-5.6-sol')))

    def test_long_context_uses_single_request_input_and_exact_threshold(self):
        at_limit = event(request_input=272_000)
        self.assertAlmostEqual(event_cost(at_limit), 1.46)
        self.assertAlmostEqual(event_cost(event(request_input=272_001)), 2.42)
        # A cumulative quantity is not a request-size signal.
        self.assertIsNone(event_cost(event(input=900_000, total=920_000, request_input=None)))
        self.assertIsNone(event_cost(event(model='gpt-5.5', request_input=272_001)))

    def test_all_documented_model_rates(self):
        expected = {'gpt-6-astra': 60, 'gpt-6-sol': 12, 'gpt-6-luna': 0.6,
                    'gpt-5.6-sol': 24, 'gpt-5.6-terra': 14,
                    'gpt-5.6-luna': 1.4, 'gpt-5.5': 35}
        for model, cost in expected.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(event_cost(event(model=model, input=1_000_000,
                    cached=0, output=1_000_000, total=2_000_000, request_input=100_000)), cost)

    def test_invalid_counters_are_not_priced_as_zero(self):
        for fields in ({'cached': 100_001}, {'input': None}, {'total': 1},
                       {'output': True}, {'request_input': -1}):
            with self.subTest(fields=fields):
                self.assertIsNone(event_cost(event(**fields)))

    def test_gpt55_request_without_certified_session_is_unpriced(self):
        self.assertIsNone(event_cost(event(model='gpt-5.5', session_price_uncertain=None)))
        self.assertIsNone(event_cost(event(model='gpt-5.5', session_price_uncertain=True)))


class SessionPricingTests(unittest.TestCase):
    def test_long_or_ambiguous_event_marks_every_gpt55_row_in_its_session(self):
        for request_input in (None, 272_001):
            with self.subTest(request_input=request_input):
                data = snapshot([event(model='gpt-5.5'),
                                 event(model='gpt-5.5-2026-04-23', request_input=request_input),
                                 event(model='gpt-6-astra'),
                                 event(model='gpt-5.5', session_id='other')],
                                sessions={'main': {'gpt55_price_uncertain': False},
                                          'other': {'gpt55_price_uncertain': False}})
                marked = enrich_pricing_events(data)['events']
                self.assertIsNone(event_cost(marked[0]))
                self.assertIsNone(event_cost(marked[1]))
                self.assertIsNotNone(event_cost(marked[2]))
                self.assertIsNotNone(event_cost(marked[3]))

    def test_long_before_cycle_blocks_short_event_pricing_inside_cycle(self):
        data = snapshot([event('2026-09-20T00:00:00', model='gpt-5.5', request_input=300_000),
                         event(model='gpt-5.5')],
                        sessions={'main': {'gpt55_price_uncertain': False}})
        report = build_report(data, '2026-09-21', moment('2026-09-26T00:00:00'))
        self.assertEqual(report['total_tokens'], 120_000)
        self.assertEqual(report['unpriced_tokens'], 120_000)
        self.assertEqual(report['known_cost'], 0)

    def test_collector_history_flag_survives_filtered_snapshot_and_no_mutation(self):
        data = snapshot([event(model='gpt-5.5')],
                        sessions={'main': {'gpt55_price_uncertain': True}})
        original = copy.deepcopy(data)
        marked = enrich_pricing_events(data)
        self.assertTrue(marked['events'][0]['session_price_uncertain'])
        self.assertIsNone(event_cost(marked['events'][0]))
        self.assertEqual(enrich_pricing_events(marked), marked)
        self.assertEqual(data, original)

    def test_absent_history_and_unknown_model_history_are_unpriced(self):
        for data in (snapshot([event(model='gpt-5.5')]),
                     snapshot([event(model=None), event(model='gpt-5.5')],
                              sessions={'main': {'gpt55_price_uncertain': False}})):
            marked = enrich_pricing_events(data)
            self.assertIsNone(event_cost(marked['events'][-1]))

    def test_certified_short_history_can_use_short_price(self):
        data = snapshot([event(model='gpt-5.5')],
                        sessions={'main': {'gpt55_price_uncertain': False}})
        marked = enrich_pricing_events(data)
        self.assertFalse(marked['events'][0]['session_price_uncertain'])
        self.assertAlmostEqual(event_cost(marked['events'][0]), .83)


class ReportTests(unittest.TestCase):
    now = moment('2026-09-26T00:00:00')

    def report(self, data, **fields):
        return build_report(data, fields.get('anchor', '2026-09-21'), fields.get('now', self.now))

    def test_projection_and_daily_series_use_monthly_cycle(self):
        report = self.report(snapshot([event()]))
        self.assertEqual((report['start'], report['end']), ('2026-09-21', '2026-10-21'))
        self.assertEqual((report['elapsed_days'], report['total_days']), (5, 30))
        self.assertAlmostEqual(report['projected_cost'], 1.46 / 5 * 30)
        self.assertEqual(report['projected_cost'], report['projected_known_cost'])
        self.assertFalse(report['projection_is_partial'])
        self.assertEqual(report['status'], 'ready')
        self.assertEqual([row['date'] for row in report['daily']],
                         ['2026-09-21', '2026-09-22', '2026-09-23',
                          '2026-09-24', '2026-09-25', '2026-09-26'])
        self.assertEqual(sum(row['total'] for row in report['daily']), report['total_tokens'])
        self.assertIn('local_logs', report['scope'])

    def test_old_future_and_previous_cycle_events_are_excluded(self):
        data = snapshot([event('2026-09-20T23:59:59'), event('2026-09-21T00:00:00'),
                         event('2026-09-26T00:00:01'), event('2026-10-21T00:00:00')])
        self.assertEqual(self.report(data)['total_tokens'], 120_000)
        next_cycle = self.report(data, now=moment('2026-10-21T00:00:00'))
        self.assertEqual(next_cycle['total_tokens'], 120_000)
        self.assertEqual(next_cycle['status'], 'low_sample')

    def test_main_children_unknown_and_model_changes_sum_consistently(self):
        events = [event(), event(session_id='child', role='subagent', parent_id='main',
                               agent_path='/root/research', model='gpt-6-luna'),
                  event(session_id='child', role='subagent', parent_id='main',
                        agent_path='/root/research', model='gpt-5.6-luna', effort='max'),
                  event(session_id='orphan', role='unknown')]
        report = self.report(snapshot(events))
        task = report['tasks'][0]
        self.assertEqual((task['main_tokens'], task['subagent_tokens'], task['unknown_tokens']),
                         (120_000, 240_000, 120_000))
        self.assertEqual(task['total'], report['total_tokens'])
        self.assertEqual(sum(agent['total'] for agent in task['agents']), task['total'])
        self.assertEqual(sum(model['total'] for model in report['models']), report['total_tokens'])
        self.assertEqual(len(task['agents']), 4)
        self.assertEqual(task['label'], '/root')
        self.assertAlmostEqual(sum(agent['known_cost'] for agent in task['agents']), task['known_cost'])

    def test_unpriced_tokens_remain_visible_and_block_total_projection(self):
        report = self.report(snapshot([event(), event(model='unknown')]))
        self.assertEqual((report['priced_tokens'], report['unpriced_tokens']), (120_000, 120_000))
        self.assertAlmostEqual(report['known_cost'], 1.46)
        self.assertIsNone(report['projected_cost'])
        self.assertAlmostEqual(report['projected_known_cost'], 1.46 / 5 * 30)
        self.assertTrue(report['projection_is_partial'])
        self.assertEqual(report['status'], 'unpriced')
        self.assertEqual(report['tasks'][0]['unpriced_tokens'], 120_000)
        self.assertEqual(sum(row['unpriced_tokens'] for row in report['daily']), 120_000)

    def test_forecast_requires_cycle_available_complete_data_and_24_hours(self):
        for data, fields, status in (
                (snapshot([event()]), {'anchor': None}, 'missing_cycle'),
                (snapshot(available=False), {}, 'unavailable'),
                (snapshot([event()], partial=True), {}, 'partial'),
                (snapshot([event('2026-09-21T02:00:00')]),
                 {'now': moment('2026-09-21T23:59:59')}, 'low_sample'),
                (snapshot(), {}, 'no_usage')):
            with self.subTest(status=status):
                report = self.report(data, **fields)
                self.assertIsNone(report['projected_cost'])
                self.assertIsNone(report['projected_known_cost'])
                self.assertEqual(report['status'], status)
        at_24h = self.report(snapshot([event()]), now=moment('2026-09-22T00:00:00'))
        self.assertIsNotNone(at_24h['projected_cost'])

    def test_malformed_record_preserves_known_part_but_blocks_forecast(self):
        report = self.report(snapshot([event(), event(timestamp=float('nan')),
                                      event(total=999_999)]))
        self.assertTrue(report['partial'])
        self.assertEqual(report['total_tokens'], 120_000)
        self.assertIsNone(report['projected_cost'])

    def test_fully_unpriced_data_has_no_numeric_known_forecast(self):
        report = self.report(snapshot([event(model='unknown')]))
        self.assertIsNone(report['projected_cost'])
        self.assertIsNone(report['projected_known_cost'])
        self.assertTrue(report['projection_is_partial'])

    def test_timestamped_issues_only_affect_selected_cycle(self):
        old = {'timestamp': moment('2026-09-20T23:59:59').timestamp(), 'reason': 'old gap'}
        future = {'timestamp': moment('2026-10-21T00:00:00').timestamp(), 'reason': 'next gap'}
        current = {'timestamp': moment('2026-09-21T00:00:00').timestamp(), 'reason': 'cycle gap'}
        report = self.report(snapshot([event()], partial=True, issues=[old, future]))
        self.assertFalse(report['partial'])
        self.assertIsNotNone(report['projected_cost'])
        report = self.report(snapshot([event()], partial=True, issues=[old, current]))
        self.assertTrue(report['partial'])
        self.assertIsNone(report['projected_cost'])
        self.assertIn('cycle gap', report['notes'])

    def test_unknown_time_io_issue_is_global_and_legacy_partial_still_applies(self):
        for fields in ({'issues': [{'timestamp': None, 'reason': 'file unreadable'}]},
                       {'issues': [{'timestamp': float('nan'), 'reason': 'invalid time'}]},
                       {'partial': True}):
            with self.subTest(fields=fields):
                report = self.report(snapshot([event()], **fields))
                self.assertTrue(report['partial'])
                self.assertIsNone(report['projected_known_cost'])

    def test_missing_cache_count_preserves_usage_as_unpriced(self):
        for cached in (None, -1, 999_999):
            with self.subTest(cached=cached):
                report = self.report(snapshot([event(), event(cached=cached)]))
                self.assertEqual(report['total_tokens'], 240_000)
                self.assertEqual(report['unpriced_tokens'], 120_000)
                self.assertAlmostEqual(report['known_cost'], 1.46)
                self.assertIsNone(report['models'][0]['cached'])
                self.assertIsNone(report['projected_cost'])

    def test_report_is_json_serializable_and_does_not_mutate_source(self):
        data = snapshot([event()])
        original = copy.deepcopy(data)
        json.dumps(self.report(data), allow_nan=False)
        self.assertEqual(data, original)


if __name__ == '__main__':
    unittest.main()
