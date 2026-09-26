"""Monthly, local-only token valuation at documented Standard API rates.

This is a comparison estimate, not the user's ChatGPT bill. Service-tier
premiums, unrecorded cache-write premiums and tools are deliberately excluded.
"""
import calendar
import datetime as dt
import math


PRICING_VERIFIED = '2026-09-26'
PRICING_URL = 'https://developers.openai.com/api/docs/pricing'
# USD / million tokens: ordinary input, cached input, output.
RATES = {
    'gpt-6-astra': (10.0, 1.0, 50.0),
    'gpt-6-sol': (2.0, 0.2, 10.0),
    'gpt-6-luna': (0.1, 0.01, 0.5),
    'gpt-5.6-sol': (4.0, 0.4, 20.0),
    'gpt-5.6-terra': (2.0, 0.2, 12.0),
    'gpt-5.6-luna': (0.2, 0.02, 1.2),
    'gpt-5.5': (5.0, 0.5, 30.0),
}
# Only aliases/snapshots individually confirmed by official model pages.
ALIASES = {'gpt-5.6': 'gpt-5.6-sol', 'gpt-5.5-2026-04-23': 'gpt-5.5'}
LONG_CONTEXT_INPUT = 272_000
SCOPE_NOTE = '仅统计本机留存日志；不代表其他设备、云端或账户全部用量。'
VALUATION_NOTE = ('按当前官方 Standard API 基础 Token 单价折算，非订阅实际账单；'
                  '未计 Fast 等服务档位差价、缓存写入溢价、工具及其他媒体费用。')
_COUNTERS = ('total', 'input', 'cached', 'output')


def _now(value):
    value = dt.datetime.now().astimezone() if value is None else value
    if not isinstance(value, dt.datetime) or value.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    return value


def monthly_cycle(anchor_date, now=None):
    """Return aware local-midnight boundaries, retaining the original anchor day.

    For a day-31 anchor, February ends on its last day and March restores 31.
    The saved date anchors a recurring monthly day, not a fixed 30-day span.
    """
    now = _now(now)
    if anchor_date is None or anchor_date == '':
        return None, None
    if not isinstance(anchor_date, str):
        raise ValueError('anchor_date must be an ISO date')
    try:
        anchor = dt.date.fromisoformat(anchor_date)
        if anchor.isoformat() != anchor_date:
            raise ValueError
    except ValueError as exc:
        raise ValueError('anchor_date must be an ISO date') from exc

    def boundary(month_index):
        year, month = divmod(month_index, 12)
        month += 1
        day = min(anchor.day, calendar.monthrange(year, month)[1])
        return dt.datetime(year, month, day, tzinfo=now.tzinfo)

    month = now.year * 12 + now.month - 1
    start = boundary(month)
    if now < start:
        month -= 1
        start = boundary(month)
    return start, boundary(month + 1)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_counts(event):
    return (_valid_usage(event)
            and _count(event.get('cached'))
            and event['cached'] <= event['input'])


def _valid_usage(event):
    return (isinstance(event, dict)
            and all(_count(event.get(key)) for key in ('total', 'input', 'output'))
            and event['total'] == event['input'] + event['output'])


def _model(event):
    value = _text(event.get('model'))
    return ALIASES.get(value, value)


def enrich_pricing_events(snapshot):
    """Copy a snapshot and attach safe session-wide GPT-5.5 price context.

    The collector's per-session ``gpt55_price_uncertain`` must consider the
    complete retained history, before the requested period is filtered out.
    Only explicit False certifies short-context history; missing context is
    unknown. A known long/ambiguous 5.5 event in this snapshot overrides False
    for every 5.5 event in its session, without affecting other models there.
    """
    source = snapshot if isinstance(snapshot, dict) else {}
    sessions = source.get('sessions') or {}
    sessions = sessions if isinstance(sessions, dict) else {}
    events = source.get('events') or []
    uncertain = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        owner = _text(event.get('session_id'), 'unknown')
        model = _model(event)
        request_input = event.get('request_input')
        if model is None or (model == 'gpt-5.5' and (
                event.get('session_price_uncertain') is True
                or not _count(request_input) or request_input > LONG_CONTEXT_INPUT)):
            uncertain.add(owner)
    result, marked = dict(source), []
    has_uncertain_gpt55 = False
    for event in events:
        if not isinstance(event, dict):
            marked.append(event)
            continue
        item = dict(event)
        if _model(item) == 'gpt-5.5':
            owner = _text(item.get('session_id'), 'unknown')
            session = sessions.get(owner)
            certified = (isinstance(session, dict)
                         and session.get('gpt55_price_uncertain') is False)
            item['session_price_uncertain'] = owner in uncertain or not certified
            has_uncertain_gpt55 |= item['session_price_uncertain']
        marked.append(item)
    result['events'] = marked
    result['notes'] = list(source.get('notes') or [])
    if has_uncertain_gpt55:
        note = 'GPT-5.5 的长上下文价适用于整个会话；历史状态无法确认的会话，其 GPT-5.5 用量全部保持未定价。'
        if note not in result['notes']:
            result['notes'].append(note)
    return result


def event_cost(event):
    """Return Standard base-token USD, or None when pricing is indeterminate.

    Cached input is a subset of input and reasoning is already in output.
    Model/effort are separate: effort has no invented per-token multiplier.
    Long-context classification must use a single request's input count.
    """
    if not _valid_counts(event):
        return None
    model = _model(event)
    if model not in RATES:
        return None
    request_input = event.get('request_input')
    if not _count(request_input):
        return None
    long_context = request_input > LONG_CONTEXT_INPUT
    # A short request alone cannot certify 5.5's full-session price tier.
    if model == 'gpt-5.5' and (long_context or event.get('session_price_uncertain') is not False):
        return None
    input_rate, cached_rate, output_rate = RATES[model]
    if long_context:
        input_rate *= 2
        cached_rate *= 2
        output_rate *= 1.5
    return ((event['input'] - event['cached']) * input_rate
            + event['cached'] * cached_rate
            + event['output'] * output_rate) / 1_000_000


def _empty(**fields):
    return {**fields, **dict.fromkeys(_COUNTERS, 0), 'known_cost': 0.0,
            'priced_tokens': 0, 'unpriced_tokens': 0}


def _add(row, event, cost):
    for field in ('total', 'input', 'output'):
        row[field] += event[field]
    if (row['cached'] is None or not _count(event.get('cached'))
            or event['cached'] > event['input']):
        row['cached'] = None
    else:
        row['cached'] += event['cached']
    if cost is None:
        row['unpriced_tokens'] += event['total']
    else:
        row['known_cost'] += cost
        row['priced_tokens'] += event['total']


def _text(value, fallback=None):
    return value if isinstance(value, str) and value else fallback


def build_report(snapshot, anchor_date, now=None):
    """Build JSON-ready monthly totals, model/agent attribution and forecast.

    Forecast needs >=24 elapsed hours, nonempty usage, a complete local scan,
    and known rates for all counted tokens. Its scope remains local logs.
    """
    now = _now(now)
    start, end = monthly_cycle(anchor_date, now)
    snapshot = enrich_pricing_events(snapshot)
    available = bool(snapshot.get('available'))
    partial = bool(snapshot.get('partial'))
    notes = [SCOPE_NOTE, VALUATION_NOTE]
    notes.extend(item for item in snapshot.get('notes', []) if isinstance(item, str))
    models, tasks, days = {}, {}, {}
    total = _empty()
    first = start.timestamp() if start else None
    stop = end.timestamp() if end else None
    until = now.timestamp()
    issues = snapshot.get('issues')
    if isinstance(issues, list):
        # Timestamped read issues only affect the cycle that contains them.
        # Unknown-time I/O or parse errors remain global; legacy collectors
        # without issue metadata continue using their original partial flag.
        selected_issues = []
        for issue in issues:
            stamp = issue.get('timestamp') if isinstance(issue, dict) else None
            global_issue = (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                            or not math.isfinite(stamp))
            if global_issue or (stamp <= until and (first is None or first <= stamp < stop)):
                selected_issues.append(issue)
        partial = bool(selected_issues)
        notes.extend(issue['reason'] for issue in selected_issues
                     if isinstance(issue, dict) and isinstance(issue.get('reason'), str))
    sessions = snapshot.get('sessions') or {}
    sessions = sessions if isinstance(sessions, dict) else {}
    malformed = False
    for event in snapshot.get('events') or []:
        if not isinstance(event, dict):
            malformed = True
            continue
        stamp = event.get('timestamp')
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp)):
            malformed = True
            continue
        if stamp > until or (first is not None and not first <= stamp < stop):
            continue
        try:
            date = dt.datetime.fromtimestamp(stamp, now.tzinfo).date().isoformat()
        except (ValueError, OverflowError, OSError):
            malformed = True
            continue
        if not _valid_usage(event):
            malformed = True
            continue
        owner = _text(event.get('session_id'), 'unknown')
        root = _text(event.get('root_id'), owner)
        model, effort = _text(event.get('model')), _text(event.get('effort'))
        role = event.get('role') if event.get('role') in ('main', 'subagent') else 'unknown'
        cost = event_cost(event)
        _add(total, event, cost)
        model_row = models.setdefault((model, effort), _empty(model=model, effort=effort))
        _add(model_row, event, cost)
        root_meta = sessions.get(root) or {}
        root_meta = root_meta if isinstance(root_meta, dict) else {}
        label = _text(root_meta.get('title'), _text(root_meta.get('agent_path'), root[:12]))
        task = tasks.setdefault(root, _empty(id=root, label=label, main_tokens=0,
                                           subagent_tokens=0, unknown_tokens=0, agents={}))
        task[{'main': 'main_tokens', 'subagent': 'subagent_tokens',
              'unknown': 'unknown_tokens'}[role]] += event['total']
        _add(task, event, cost)
        agent = task['agents'].setdefault((owner, model, effort), _empty(
            id=owner, role=role, agent_path=_text(event.get('agent_path')),
            model=model, effort=effort))
        _add(agent, event, cost)
        day = days.setdefault(date, _empty(date=date))
        _add(day, event, cost)
    if malformed:
        partial = True
        notes.append('部分记录的时间或 Token 计数无效，未并入统计。')
    if total['unpriced_tokens']:
        notes.append('未知模型、缺失缓存或单次输入计数、无法确定长上下文计价的 Token 保持未定价。')
    if partial:
        notes.append('本机日志存在缺失或读取异常，周期合计为已记录部分。')
    elapsed_seconds = until - first if first is not None else None
    cycle_seconds = stop - first if first is not None else None
    projected = None
    projected_known = None
    if (start is not None and available and not partial
            and elapsed_seconds >= 86_400 and total['priced_tokens']):
        projected_known = total['known_cost'] / elapsed_seconds * cycle_seconds
        notes.append('按本周期截至现在的平均消耗速度外推；未来使用变化会改变结果。')
        if total['unpriced_tokens']:
            notes.append('已知部分预计仅外推已定价用量，未定价部分不视为零。')
    if start is None:
        status = 'missing_cycle'
        notes.append('设置每月周期起始日后可估算周期结束费用。')
    elif not available:
        status = 'unavailable'
        notes.append('暂未找到本机用量日志。')
    elif partial:
        status = 'partial'
    elif total['unpriced_tokens']:
        status = 'unpriced'
    elif elapsed_seconds < 86_400:
        status = 'low_sample'
        notes.append('本周期未满 24 小时，暂不外推。')
    elif not total['total']:
        status = 'no_usage'
        notes.append('本周期尚无可用于预测的用量记录。')
    else:
        status = 'ready'
        projected = projected_known
    # Explicit zero rows describe the local recorded series, never account
    # completeness. Future days are excluded rather than fabricated as zero.
    if start:
        date = start.date()
        while date <= now.date() and date < end.date():
            key = date.isoformat()
            days.setdefault(key, _empty(date=key))
            date += dt.timedelta(days=1)
    for task in tasks.values():
        task['agents'] = sorted(task['agents'].values(), key=lambda row: (
            {'main': 0, 'subagent': 1, 'unknown': 2}[row['role']],
            -row['total'], row['id'], row['model'] or '', row['effort'] or ''))
    return {
        'available': available, 'partial': partial, 'updated': snapshot.get('updated'),
        'anchor_date': anchor_date or None,
        'start': start.date().isoformat() if start else None,
        'end': end.date().isoformat() if end else None,
        'elapsed_days': elapsed_seconds / 86_400 if start else None,
        'total_days': (end.date() - start.date()).days if start else None,
        'total_tokens': total['total'], 'known_cost': total['known_cost'],
        'priced_tokens': total['priced_tokens'], 'unpriced_tokens': total['unpriced_tokens'],
        'projected_cost': projected, 'projected_known_cost': projected_known,
        'projection_is_partial': bool(total['unpriced_tokens']), 'status': status,
        'scope': 'local_logs', 'notes': list(dict.fromkeys(notes)),
        'pricing_verified': PRICING_VERIFIED, 'pricing_url': PRICING_URL,
        'models': sorted(models.values(), key=lambda row: (-row['total'], row['model'] or '', row['effort'] or '')),
        'tasks': sorted(tasks.values(), key=lambda row: (-row['total'], row['id'])),
        'daily': [days[date] for date in sorted(days)],
    }
