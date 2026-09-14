"""Read-only low-quota alert decisions; no UI, network, or credentials.

Call ``check_alerts(result, saved_state)`` after a provider refresh, display each
notice, and persist the returned state. When alerts are disabled, do not call
this function or advance its state. Each notice contains ``id``, ``title``, and
``message``. Unknown, failed, expired, or stale data never clears an active alert.
An omitted provider ``updated`` timestamp means the caller just fetched the data.
"""
import math
import time


MAX_DATA_AGE_SECONDS = 600
CODEX_REMAINING_THRESHOLD = 10
DEEPSEEK_THRESHOLDS = {'CNY': 10, 'USD': 1}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh_stamp(provider, now):
    if not isinstance(provider, dict) or provider.get('ok') is not True:
        return None
    if 'updated' not in provider:
        return now
    stamp = _number(provider['updated'])
    if stamp is None or now-stamp > MAX_DATA_AGE_SECONDS or stamp-now > 60:
        return None
    return stamp


def _clean_state(previous):
    if not isinstance(previous, dict):
        return {}
    return {key: {'low': value['low'], 'cycle': _number(value.get('cycle')),
                  'updated': _number(value.get('updated'))}
            for key, value in previous.items()
            if isinstance(key, str) and key.startswith(('codex:', 'deepseek:'))
            and isinstance(value, dict) and isinstance(value.get('low'), bool)}


def check_alerts(result, previous_state, now=None):
    """Return ``(notices, new_state)`` without mutating either input.

    Codex alerts at <=10% remaining; DeepSeek at <=CNY 10 or <=USD 1.
    The same low condition is notified once, and healthy data re-arms it.
    A later Codex reset timestamp also starts a new notification cycle.
    ``now`` is an optional Unix timestamp for deterministic tests.
    """
    now = time.time() if now is None else float(now)
    state = _clean_state(previous_state)
    notices = []
    if not isinstance(result, dict):
        return notices, state

    def observe(key, low, cycle, stamp, title, message):
        previous = state.get(key)
        if previous and previous['updated'] is not None and stamp < previous['updated']:
            return
        previous_cycle = previous.get('cycle') if previous else None
        if cycle is not None and previous_cycle is not None and cycle < previous_cycle:
            return
        # A temporarily missing reset is not evidence of another quota cycle.
        if cycle is None:
            cycle = previous_cycle
        new_cycle = previous_cycle is not None and cycle != previous_cycle
        if low and (not previous or not previous['low'] or new_cycle):
            notices.append({'id': key, 'title': title, 'message': message})
        state[key] = {'low': low, 'cycle': cycle, 'updated': stamp}

    codex = result.get('codex')
    stamp = _fresh_stamp(codex, now)
    if stamp is not None:
        cards = codex.get('cards') or []
        for card in cards if isinstance(cards, list) else []:
            if not isinstance(card, dict):
                continue
            name = card.get('name') or 'Codex'
            windows = card.get('windows') or []
            for window in windows if isinstance(windows, list) else []:
                if not isinstance(window, dict):
                    continue
                used = _number(window.get('used'))
                if used is None or not 0 <= used <= 100:
                    continue
                raw_reset = window.get('reset')
                reset = _number(raw_reset)
                if raw_reset is not None and (reset is None or reset <= now):
                    continue
                label = window.get('label') or '额度'
                remaining = 100-used
                observe(f'codex:{name}:{label}', remaining <= CODEX_REMAINING_THRESHOLD,
                        reset, stamp, f'{name} 低额度提醒',
                        f'{name} {label}额度仅剩 {remaining:g}%，请留意使用量。')

    deepseek = result.get('deepseek')
    stamp = _fresh_stamp(deepseek, now)
    if stamp is not None:
        balances = deepseek.get('balances') or []
        for balance in balances if isinstance(balances, list) else []:
            if not isinstance(balance, dict):
                continue
            currency = str(balance.get('currency', '')).upper()
            amount = _number(balance.get('total_balance'))
            if currency not in DEEPSEEK_THRESHOLDS or amount is None:
                continue
            symbol = '¥' if currency == 'CNY' else '$'
            observe(f'deepseek:{currency}', amount <= DEEPSEEK_THRESHOLDS[currency],
                    None, stamp, 'DeepSeek 低余额提醒',
                    f'DeepSeek 余额为 {symbol}{amount:.2f}，请留意后续 API 用量。')

    return notices, state
