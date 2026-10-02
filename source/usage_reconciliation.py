"""Privacy-safe reconciliation of an existing snapshot; performs no I/O.

The collector has already deduplicated events. This module never deduplicates
again or assumes that the service uses either of the local calendar choices.
An empty local day has unknown coverage, not a confirmed zero.
"""
import datetime as dt
import math

from usage_view import official_range_rows


MAX_DAYS = 7

# Match only collector-owned messages. Never serialize an arbitrary reason:
# it may contain a path, identifier, or other private diagnostic information.
_ISSUE_TYPES = {
    '部分用量记录时间无效，无法确定归属周期。': 'invalid_usage_timestamp',
    '部分单次用量记录的计数或标识无效。': 'invalid_response_counts_or_id',
    '部分累计用量记录的计数无效。': 'invalid_cumulative_counts',
    '部分旧日志缺少可辨识的单次用量。': 'missing_legacy_last_usage',
    '部分旧日志缺少起始累计基线，仅计入可辨识的后续用量。': 'missing_legacy_baseline',
    '部分日志目录无法读取，无法确定缺失用量的周期。': 'unreadable_log_directory',
    '部分日志文件无法读取，无法确定缺失用量的周期。': 'unreadable_log_file',
    '部分日志记录无法解析，无法确定归属周期。': 'unparseable_log_record',
    '部分 Agent 关系存在循环，任务归属未知。': 'agent_attribution_cycle',
}


def _timestamp(value):
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        return value
    return None


def _local_date(stamp):
    # Resolve OS timezone rules for this instant, not today's fixed UTC offset.
    return dt.datetime.fromtimestamp(stamp).date()


def _utc_date(stamp):
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).date()


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _issue_breakdown(issues, dates):
    """Count existing issue entries, without estimating missing tokens."""
    result = {'count_semantics': 'snapshot_issue_entries_not_missing_tokens',
              'attribution_only_types': ['agent_attribution_cycle'], 'totals': {}}
    daily = {basis: {day: {} for day in dates} for basis in ('local', 'utc')}
    for basis in daily:
        result[basis] = {'undated': {}, 'outside_range': {}}

    def add(counts, code):
        counts[code] = counts.get(code, 0)+1

    for issue in issues if isinstance(issues, list) else []:
        issue = issue if isinstance(issue, dict) else {}
        reason = issue.get('reason')
        code = (_ISSUE_TYPES.get(reason, 'unknown_issue_type')
                if isinstance(reason, str) else 'unknown_issue_type')
        add(result['totals'], code)
        stamp = _timestamp(issue.get('timestamp'))
        for basis, date_for in (('local', _local_date), ('utc', _utc_date)):
            try:
                day = date_for(stamp) if stamp is not None else None
            except (OverflowError, OSError, ValueError):
                day = None
            if day is None:
                counts = result[basis]['undated']
            elif day in daily[basis]:
                counts = daily[basis][day]
            else:
                counts = result[basis]['outside_range']
            add(counts, code)
    for basis in daily:
        result[basis]['daily'] = [{'date': day.isoformat(), 'counts': daily[basis][day]}
                                  for day in dates]
    return result


def build_reconciliation(snapshot, official, now=None, start_date=None):
    """Compare at most seven dates ending at the system-local yesterday.

    ``now`` must be timezone-aware when supplied. ``start_date`` is an ISO
    calendar date or date object; earlier ranges are capped at seven days.
    ``partial`` describes uncertain total coverage. Unknown cache counts are
    separate, since they do not invalidate known input/output/total counts.
    Differences are official minus recorded local totals, not inferred losses.
    """
    now = dt.datetime.now().astimezone() if now is None else now
    if not isinstance(now, dt.datetime) or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    until = _timestamp(now.timestamp())
    if until is None:
        raise ValueError('now must have a valid timestamp')
    local_today, utc_today = _local_date(until), _utc_date(until)
    end = local_today-dt.timedelta(days=1)
    earliest = end-dt.timedelta(days=MAX_DAYS-1)
    if start_date is None:
        requested = earliest
    elif isinstance(start_date, dt.date) and not isinstance(start_date, dt.datetime):
        requested = start_date
    elif isinstance(start_date, str):
        requested = dt.date.fromisoformat(start_date)
        if requested.isoformat() != start_date:
            raise ValueError('start_date must be an ISO calendar date')
    else:
        raise ValueError('start_date must be an ISO calendar date')
    if requested > end:
        raise ValueError('start_date cannot be later than local yesterday')
    start = max(requested, earliest)
    dates = [start+dt.timedelta(days=i) for i in range((end-start).days+1)]
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    official = official if isinstance(official, dict) else {}
    reasons = {}

    def note(code, count=1):
        if count:
            reasons[code] = reasons.get(code, 0)+count

    available = snapshot.get('available') is True
    issues = snapshot.get('issues')
    issue_count = len(issues) if isinstance(issues, list) else 0
    source_partial = bool(snapshot.get('partial') or issue_count or not available)
    note('snapshot_unavailable', int(not available))
    note('snapshot_partial', int(bool(snapshot.get('partial'))))
    note('snapshot_issue', issue_count)
    groups = {basis: {day: [] for day in dates} for basis in ('local', 'utc')}
    events = snapshot.get('events')
    if not isinstance(events, list):
        note('missing_event_list')
        source_partial = True
        events = []
    for event in events:
        if not isinstance(event, dict):
            note('invalid_event')
            source_partial = True
            continue
        stamp = _timestamp(event.get('timestamp'))
        if stamp is None:
            note('invalid_timestamp')
            source_partial = True
            continue
        if stamp > until:
            note('future_event')
            continue
        try:
            event_days = {'local': _local_date(stamp), 'utc': _utc_date(stamp)}
        except (OverflowError, OSError, ValueError):
            note('invalid_timestamp')
            source_partial = True
            continue
        # Local-today events can still belong to UTC yesterday. Never discard
        # them using a local date filter before the second calendar is checked.
        if not any(day in groups[basis] for basis, day in event_days.items()):
            continue
        if (not all(_count(event.get(key)) for key in ('input', 'output', 'total'))
                or event['total'] != event['input']+event['output']):
            note('invalid_counts')
            source_partial = True
            continue
        cached = event.get('cached')
        cache_known = _count(cached) and cached <= event['input']
        if not cache_known:
            note('unknown_cache')
        counters = (event['input'], event['output'], event['total'], cached if cache_known else None)
        for basis, day in event_days.items():
            if day in groups[basis]:
                groups[basis][day].append(counters)

    daily = {}
    for basis, today in (('local', local_today), ('utc', utc_today)):
        rows = []
        for day in dates:
            group = groups[basis][day]
            missing = not group
            cache_unknown = any(item[3] is None for item in group)
            closed = day < today
            rows.append({'date': day.isoformat(),
                         'input': sum(item[0] for item in group) if group else None,
                         'output': sum(item[1] for item in group) if group else None,
                         'total': sum(item[2] for item in group) if group else None,
                         'cached': sum(item[3] for item in group) if group and not cache_unknown else None,
                         'event_count': len(group), 'missing': missing,
                         'partial': source_partial or not closed,
                         'cache_unknown': cache_unknown, 'day_closed': closed})
        daily[basis] = rows

    raw_buckets = official.get('buckets')
    buckets = official_range_rows({'ok': official.get('ok') is True,
        'buckets': raw_buckets if isinstance(raw_buckets, list) else []},
        start.isoformat(), end.isoformat())
    by_date = {row['date']: row for row in buckets}
    daily['official'] = []
    for day in dates:
        row = by_date.get(day.isoformat())
        daily['official'].append({'date': day.isoformat(), 'total': row['total'] if row else None,
            'missing': row is None, 'stale': row is not None and not row['ok']})
    note('official_missing_day', sum(row['missing'] for row in daily['official']))
    note('official_stale_day', sum(row['stale'] for row in daily['official']))
    target = {'date': end.isoformat(), 'official_total': daily['official'][-1]['total'],
              'official_missing': daily['official'][-1]['missing'],
              'official_stale': daily['official'][-1]['stale']}
    for basis in ('local', 'utc'):
        row = daily[basis][-1]
        target[basis+'_total'] = row['total']
        target[basis+'_missing'] = row['missing']
        target[basis+'_partial'] = row['partial']
        target[basis+'_day_closed'] = row['day_closed']
        target['official_minus_'+basis] = (target['official_total']-row['total']
            if target['official_total'] is not None and row['total'] is not None else None)
    return {'range': {'start': start.isoformat(), 'end': end.isoformat(), 'days': len(dates),
                      'clipped': requested < earliest},
            'target_date': end.isoformat(), 'date_bases': {'local': 'system_local', 'utc': 'UTC',
                'official': 'reported_calendar_date_timezone_unspecified'},
            'query_times': {'generated_at': until,
                'snapshot_updated_at': _timestamp(snapshot.get('updated')),
                'official_updated_at': _timestamp(official.get('updated'))},
            'snapshot_available': available, 'daily': daily, 'target': target,
            'reason_counts': reasons, 'issue_breakdown': _issue_breakdown(issues, dates)}
