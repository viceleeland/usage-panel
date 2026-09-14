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
