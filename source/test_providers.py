import unittest
from unittest.mock import patch
import providers

class UsageTests(unittest.TestCase):
    def test_window_is_not_assumed_to_be_five_hours(self):
        row=providers.normalize_window({'usedPercent':23,'windowDurationMins':10080,'resetsAt':1900000000})
        self.assertEqual(row['label'],'每周')
        self.assertEqual(100-row['used'],77)

    def test_missing_windows_stay_unknown(self):
        self.assertIsNone(providers.normalize_window(None))
        self.assertIsNone(providers.normalize_window({'usedPercent':None}))
        self.assertEqual(providers.normalize_window({'usedPercent':0})['used'],0)

    def test_iso_reset_and_clamping(self):
        row=providers.normalize_window({'utilization':120,'resets_at':'2026-09-14T00:00:00Z'},'每周')
        self.assertEqual(row['used'],100)
        self.assertIsInstance(row['reset'],float)

    def test_daily_usage_preserves_official_dates_and_totals(self):
        daily = providers.normalize_daily_usage({'dailyUsageBuckets': [
            {'startDate': '2026-09-13', 'tokens': 122116000},
            {'startDate': '2026-09-12', 'tokens': 76572000}]})
        self.assertTrue(daily['ok'])
        self.assertEqual(daily['buckets'], [
            {'date': '2026-09-12', 'total': 76572000},
            {'date': '2026-09-13', 'total': 122116000}])

    def test_daily_usage_missing_is_unknown_but_explicit_zero_is_valid(self):
        for raw in (None, {}, {'dailyUsageBuckets': None}, {'dailyUsageBuckets': {}}):
            with self.subTest(raw=raw):
                daily = providers.normalize_daily_usage(raw)
                self.assertFalse(daily['ok'])
                self.assertEqual(daily['buckets'], [])
        daily = providers.normalize_daily_usage({'dailyUsageBuckets': [
            {'startDate': '2026-09-12', 'tokens': None},
            {'startDate': '2026-09-13', 'tokens': 0},
            {'startDate': '2026-09-14'}]})
        self.assertEqual(daily['buckets'], [{'date': '2026-09-13', 'total': 0}])

    def test_daily_usage_empty_does_not_fill_dates(self):
        daily = providers.normalize_daily_usage({'dailyUsageBuckets': []})
        self.assertTrue(daily['ok'])
        self.assertEqual(daily['buckets'], [])

    def test_daily_usage_rejects_invalid_dates_and_totals(self):
        rows = [{'startDate': date, 'tokens': 1} for date in (
            None, 20260913, '20260913', '2026-02-30', '2026-09-13T00:00:00Z', '2026-9-13')]
        rows += [{'startDate': '2026-09-13', 'tokens': value} for value in (-1, True, 1.5, '100')]
        rows += [None, 'invalid', {'startDate': '2026-09-13', 'tokens': 12}]
        daily = providers.normalize_daily_usage({'dailyUsageBuckets': rows})
        self.assertEqual(daily['buckets'], [{'date': '2026-09-13', 'total': 12}])

    def test_daily_usage_duplicate_dates_are_not_added(self):
        daily = providers.normalize_daily_usage({'dailyUsageBuckets': [
            {'startDate': '2026-09-13', 'tokens': 12},
            {'startDate': '2026-09-13', 'tokens': 12}]})
        self.assertEqual(daily['buckets'], [{'date': '2026-09-13', 'total': 12}])

    def test_daily_usage_failure_does_not_hide_quota_or_reset_credits(self):
        quota = {'rateLimits': {'planType': 'pro', 'primary': {'usedPercent': 30}},
                 'rateLimitResetCredits': {'availableCount': 3, 'credits': []}}
        with patch.object(providers, 'CodexRPC') as rpc_class:
            rpc = rpc_class.return_value
            rpc.call.side_effect = [{}, quota, TimeoutError()]
            result = providers.codex_usage()
        self.assertTrue(result['ok'])
        self.assertEqual(result['cards'][0]['windows'][0]['used'], 30)
        self.assertEqual(result['reset_credits']['count'], 3)
        self.assertFalse(result['daily_usage']['ok'])
        self.assertEqual(result['daily_usage']['buckets'], [])
        rpc.close.assert_called_once()

    def test_daily_usage_is_fetched_using_the_same_read_only_rpc(self):
        quota = {'rateLimits': {'primary': {'usedPercent': 30}}}
        with patch.object(providers, 'CodexRPC') as rpc_class:
            rpc = rpc_class.return_value
            rpc.call.side_effect = [{}, quota, {'dailyUsageBuckets': [
                {'startDate': '2026-09-13', 'tokens': 42}]}]
            result = providers.codex_usage()
        self.assertTrue(result['daily_usage']['ok'])
        self.assertEqual(result['daily_usage']['buckets'], [{'date': '2026-09-13', 'total': 42}])
        self.assertIsInstance(result['daily_usage']['updated'], float)
        self.assertEqual([call.args[0] for call in rpc.call.call_args_list],
                         ['initialize', 'account/rateLimits/read', 'account/usage/read'])
        rpc_class.assert_called_once_with()
        rpc.close.assert_called_once()

    def test_iq_matches_site_task_weighting(self):
        a={'points':[{'model':'gpt-6-astra','effort':'high','iq':100,'total':120}]}
        b={'points':[{'model':'gpt-6-astra','effort':'high','iq':140,'valid_tasks':30}]}
        tables=providers.radar_tables(a,b)
        self.assertEqual(tables['综合智能']['Astra'][3],108)
        self.assertEqual(tables['软件工程']['Astra'][3],100)
        self.assertEqual(tables['视觉空间']['Astra'][3],140)

    def test_iq_requires_both_components(self):
        a={'points':[{'model':'gpt-6-astra','effort':'ultra','iq':110,'total':30}]}
        tables=providers.radar_tables(a,{'points':[]})
        self.assertEqual(tables['综合智能']['Astra'][0],'')
        self.assertEqual(tables['软件工程']['Astra'][0],110)

    def test_other_provider_key_never_sent_to_deepseek(self):
        with patch.dict(providers.os.environ,{'DEEPSEEK_API_KEY':''}), patch.object(providers,'read_json',return_value={
            'env':{'ANTHROPIC_BASE_URL':'https://example.org','ANTHROPIC_AUTH_TOKEN':'TEST_OTHER_PROVIDER'}}), \
            patch.object(providers.urllib.request,'build_opener') as network:
            result=providers.deepseek_usage()
            self.assertFalse(result['ok'])
            network.assert_not_called()

if __name__=='__main__': unittest.main()
