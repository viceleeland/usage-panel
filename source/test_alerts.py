import copy
import json
import unittest

from alerts import check_alerts


NOW = 1_800_000_000


def codex(used=90, reset=NOW+3600, **extra):
    return {'codex': {'ok': True, 'cards': [{'name': 'Codex', 'windows': [
        {'label': '5 小时', 'used': used, 'reset': reset}]}], **extra}}


def deepseek(amount='10.00', currency='CNY', **extra):
    return {'deepseek': {'ok': True, 'balances': [
        {'currency': currency, 'total_balance': amount}], **extra}}


class AlertTests(unittest.TestCase):
    def test_thresholds_and_numeric_string_balances(self):
        for result in (codex(), deepseek(), deepseek('1.00', 'USD')):
            with self.subTest(result=result):
                notices, state = check_alerts(result, {}, NOW)
                self.assertEqual(len(notices), 1)
                self.assertEqual(set(notices[0]), {'id', 'title', 'message'})
                json.dumps(state, allow_nan=False)
        for result in (codex(89.99), deepseek('10.01'), deepseek('1.01', 'USD')):
            self.assertEqual(check_alerts(result, {}, NOW)[0], [])

    def test_same_low_state_only_alerts_once_even_after_serialization(self):
        for result in (codex(), deepseek()):
            notices, state = check_alerts(result, {}, NOW)
            self.assertEqual(len(notices), 1)
            restored = json.loads(json.dumps(state))
            self.assertEqual(check_alerts(result, restored, NOW+10)[0], [])

    def test_healthy_data_rearms_alert(self):
        for low, healthy in ((codex(), codex(20)), (deepseek(), deepseek('20'))):
            _, state = check_alerts(low, {}, NOW)
            notices, state = check_alerts(healthy, state, NOW+10)
            self.assertEqual(notices, [])
            self.assertEqual(len(check_alerts(low, state, NOW+20)[0]), 1)

    def test_codex_later_reset_starts_new_cycle(self):
        _, state = check_alerts(codex(), {}, NOW)
        notices, state = check_alerts(codex(reset=NOW+7200), state, NOW+10)
        self.assertEqual(len(notices), 1)
        self.assertEqual(check_alerts(codex(reset=NOW+7200), state, NOW+20)[0], [])

    def test_missing_or_later_known_reset_does_not_repeat_same_low_state(self):
        _, state = check_alerts(codex(reset=None), {}, NOW)
        self.assertEqual(check_alerts(codex(reset=None), state, NOW+10)[0], [])
        notices, state = check_alerts(codex(), state, NOW+20)
        self.assertEqual(notices, [])
        notices, state = check_alerts(codex(reset=None), state, NOW+30)
        self.assertEqual(notices, [])
        self.assertEqual(state['codex:Codex:5 小时']['cycle'], NOW+3600)

    def test_unknown_and_failed_data_keep_active_state(self):
        _, state = check_alerts({**codex(), **deepseek()}, {}, NOW)
        inputs = [None, {}, codex(None), codex(20, ok=False),
                  deepseek(None), deepseek('100', ok=False),
                  {'codex': {'ok': True, 'cards': []}}]
        for result in inputs:
            with self.subTest(result=result):
                notices, unchanged = check_alerts(result, state, NOW+10)
                self.assertEqual(notices, [])
                self.assertEqual(unchanged, state)

    def test_expired_reset_never_alerts_or_rearms(self):
        _, state = check_alerts(codex(), {}, NOW)
        for used in (20, 95):
            expired = codex(used, reset=NOW)
            notices, unchanged = check_alerts(expired, state, NOW)
            self.assertEqual(notices, [])
            self.assertEqual(unchanged, state)
            self.assertEqual(check_alerts(expired, {}, NOW)[0], [])

    def test_stale_data_never_alerts_or_rearms(self):
        for low, healthy in ((codex(), codex(20, updated=NOW-601)),
                             (deepseek(), deepseek('20', updated=NOW-601))):
            _, state = check_alerts(low, {}, NOW)
            self.assertEqual(check_alerts(healthy, state, NOW), ([], state))
            stale_low = copy.deepcopy(low)
            next(iter(stale_low.values()))['updated'] = NOW-601
            self.assertEqual(check_alerts(stale_low, {}, NOW)[0], [])

    def test_out_of_order_data_does_not_rearm_or_revert_cycle(self):
        _, state = check_alerts(codex(updated=NOW), {}, NOW)
        notices, unchanged = check_alerts(codex(20, updated=NOW-30), state, NOW)
        self.assertEqual((notices, unchanged), ([], state))
        notices, unchanged = check_alerts(codex(20, reset=NOW+60), state, NOW+10)
        self.assertEqual((notices, unchanged), ([], state))

    def test_invalid_numbers_and_unknown_currency_are_unavailable(self):
        for value in (None, True, 'nan', 'Infinity', 'bad', {}, []):
            self.assertEqual(check_alerts(codex(value), {}, NOW)[0], [])
            self.assertEqual(check_alerts(deepseek(value), {}, NOW)[0], [])
        self.assertEqual(check_alerts(deepseek('0', 'EUR'), {}, NOW)[0], [])
        self.assertEqual(check_alerts(codex(reset='bad'), {}, NOW)[0], [])

    def test_independent_windows_and_providers(self):
        result = {**codex(), **deepseek()}
        result['codex']['cards'][0]['windows'].append(
            {'label': '每周', 'used': 95, 'reset': NOW+86400})
        notices, state = check_alerts(result, {}, NOW)
        self.assertEqual(len(notices), 3)
        self.assertEqual(len({notice['id'] for notice in notices}), 3)
        self.assertEqual(check_alerts(result, state, NOW+10)[0], [])

    def test_inputs_are_not_mutated_and_state_contains_only_alert_fields(self):
        result = codex()
        previous = {'unrelated': {'token': 'not-carried'},
                    'deepseek:CNY': {'low': False, 'cycle': None,
                                     'updated': NOW, 'token': 'not-carried'}}
        originals = copy.deepcopy((result, previous))
        _, state = check_alerts(result, previous, NOW)
        self.assertEqual((result, previous), originals)
        self.assertNotIn('unrelated', state)
        self.assertNotIn('token', json.dumps(state))
        self.assertEqual(set(state['deepseek:CNY']), {'low', 'cycle', 'updated'})


if __name__ == '__main__':
    unittest.main()
