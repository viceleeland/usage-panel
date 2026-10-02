"""Offline contracts mirrored from codexradar.com's homepage, 2026-10-02."""
import io
import json
import unittest
from unittest.mock import patch

import providers


def software(*points, schema=3):
    return {'schema': schema, 'mode': 'equal_latest_3' if schema == 3 else 'weighted_latest_3',
            'benchmark_id': 'deep-swe', 'points': list(points), 'source_updated_at': '2026-10-02T09:00:00Z'}


def visual(*points):
    return {'schema': 1, 'type': 'visual_spatial_reasoning_summary', 'benchmark_id': 'pompeii-adjacency',
            'points': list(points), 'source_updated_at': '2026-09-30T01:27:54Z'}


def point(model='gpt-6.1-sol', iq=100, samples=30, **extra):
    return {'model': model, 'effort': 'high', 'iq': iq, 'total': samples,
            'valid_tasks': samples, **extra}


class RadarTests(unittest.TestCase):
    def score(self, a, b, mode='综合智能', model='GPT-6.1 Sol'):
        return providers.radar_tables(a, b)[mode][model][3]

    def test_current_model_names_and_order_are_unambiguous(self):
        tables = providers.radar_tables(software(), visual())
        self.assertEqual(list(tables['综合智能']), [
            'GPT-6 Astra', 'GPT-6.1 Sol', 'GPT-6 Sol', 'GPT-6 Luna',
            'GPT-5.6 Sol', 'GPT-5.6 Terra', 'GPT-5.6 Luna', 'GPT-5.5'])
        for mode in tables.values():
            self.assertTrue(all(row == [''] * 6 for row in mode.values()))

    def test_same_integer_rounding_as_javascript_including_half_ties(self):
        for iq, expected in ((98.49, 98), (98.5, 99), (100.5, 101), (0, 0), (150, 150)):
            with self.subTest(iq=iq):
                self.assertEqual(self.score(software(point(iq=iq)), visual()), expected)

    def test_composite_uses_unrounded_iq_and_schema_three_total(self):
        # A stale valid_tasks field must not override schema 3's authoritative total.
        a = software(point(iq=100.49, samples=60, valid_tasks=1))
        b = visual(point(iq=101.49, samples=30))
        self.assertEqual(self.score(a, b), 101)
        self.assertEqual(self.score(a, b, '软件工程'), 100)

    def test_schema_two_uses_weighted_total(self):
        a = software(point(iq=100, total=900, valid_tasks=900, weighted_total=30), schema=2)
        self.assertEqual(self.score(a, visual(point(iq=140, samples=90))), 130)

    def test_sample_gate_is_applied_to_both_individual_scores_and_composite(self):
        for model in ('gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna'):
            label = providers.RADAR_MODELS[model]
            with self.subTest(model=model):
                a, b = software(point(model, samples=29)), visual(point(model, iq=140, samples=30))
                self.assertEqual(self.score(a, b, model=label), '')
                self.assertEqual(self.score(a, b, '软件工程', label), '')
                self.assertEqual(self.score(a, b, '视觉空间', label), 140)
                a = software(point(model, samples=30))
                self.assertEqual(self.score(a, visual(), model=label), 100)
                b = visual(point(model, iq=140, samples=29))
                self.assertEqual(self.score(a, b, model=label), 100)
                self.assertEqual(self.score(a, b, '视觉空间', label), '')
                self.assertEqual(self.score(a, visual(point(model, iq=140)), model=label), 120)

    def test_astra_and_gpt_five_still_require_both_dimensions_without_sample_gate(self):
        for model in ('gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5'):
            with self.subTest(model=model):
                label = providers.RADAR_MODELS[model]
                a = software(point(model, samples=1))
                self.assertEqual(self.score(a, visual(), model=label), '')
                self.assertEqual(self.score(a, visual(point(model, iq=140, samples=1)), model=label), 120)

    def test_visual_only_never_invents_composite(self):
        self.assertEqual(self.score(software(), visual(point())), '')

    def test_low_distinct_task_coverage_does_not_hide_valid_iq(self):
        a = software(point(samples=30, distinct_tasks=1, benchmark_tasks=112))
        self.assertEqual(self.score(a, visual()), 100)

    def test_malformed_points_do_not_poison_valid_models(self):
        bad = [None, 'invalid', {}, point(model=[]), point(iq=True), point(iq=float('nan')),
               point(iq=float('inf')), point(iq=-1), point(iq=151), point(samples=True),
               point(model='unannounced-model'), point(effort='unknown')]
        tables = providers.radar_tables(software(*bad, point(iq='100.5', samples='30')), visual())
        self.assertEqual(tables['软件工程']['GPT-6.1 Sol'][3], 101)

    def test_unknown_schema_or_benchmark_fails_closed(self):
        for field, value in (('schema', 4), ('mode', 'unknown'), ('benchmark_id', 'other'), ('points', None)):
            with self.subTest(field=field):
                a = software(point())
                a[field] = value
                with self.assertRaises(ValueError):
                    providers.radar_tables(a, visual())
        b = visual(point())
        b['benchmark_id'] = 'deep-swe'
        with self.assertRaises(ValueError):
            providers.radar_tables(software(), b)

    def test_raw_visual_table_uses_latest_run_per_task(self):
        model = 'gpt-6-astra'
        raw = {'schema': 1, 'benchmark_id': 'pompeii-adjacency', 'scoring_mode': 'continuous-macro',
               'combos': [{'model': model, 'effort': 'high'}],
               'tasks': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}, {'id': 'd'}], 'cells': {
                   f'a|{model}|high': {'ran_by': [{'score': .2, 'graded_at': '2026-10-01T01:00:00Z'}, {'score': 1}]},
                   f'b|{model}|high': {'ran_by': [{'score': .8, 'graded_at': '2026-10-02T01:00:00Z'}]},
                   f'c|{model}|high': {'ran_by': [{'score': None}, {'score': 1}]},
                   f'd|{model}|high': {'ran_by': [{'score': True}]}}}
        summary = providers._radar_visual_summary(raw)
        self.assertEqual(summary['points'][0]['valid_tasks'], 2)
        self.assertEqual(summary['source_updated_at'], '2026-10-02T01:00:00+00:00')
        self.assertEqual(self.score(software(point(model)), raw, '视觉空间', 'GPT-6 Astra'), 75)

    def fake_fetch(self, statuses=None, seen=None):
        statuses = statuses or {}
        def fetch(request, timeout):
            path = request.full_url.rsplit('/', 1)[-1]
            if seen is not None:
                seen.append(request.full_url)
            payload = software(point()) if path == 'intelligence-efficiency-metrics' else visual()
            response = io.StringIO(json.dumps(payload))
            status = statuses.get(path, 'HIT')
            response.headers = {'X-Codex-Cache': status} if status is not None else {}
            return response
        return fetch

    def test_live_fetch_path_keeps_independent_source_times(self):
        seen = []
        with patch.object(providers.urllib.request, 'urlopen', side_effect=self.fake_fetch(seen=seen)):
            result = providers.radar_scores()
        self.assertTrue(result['ok'])
        self.assertEqual(result['tables']['综合智能']['GPT-6.1 Sol'][3], 100)
        self.assertEqual(result['software_updated'], '2026-10-02T09:00:00Z')
        self.assertEqual(result['visual_updated'], '2026-09-30T01:27:54Z')
        self.assertCountEqual(seen, ['https://codexradar.com/api/intelligence-efficiency-metrics',
                                     'https://codexradar.com/api/visual-spatial-reasoning'])

    def test_stale_or_unidentified_edge_cache_is_not_shown_as_fresh(self):
        for endpoint in ('intelligence-efficiency-metrics', 'visual-spatial-reasoning'):
            for status in ('STALE', 'STALE-ERROR', 'ERROR', '', None):
                with self.subTest(endpoint=endpoint, status=status):
                    with patch.object(providers.urllib.request, 'urlopen',
                                      side_effect=self.fake_fetch({endpoint: status})):
                        self.assertFalse(providers.radar_scores()['ok'])


if __name__ == '__main__':
    unittest.main()
