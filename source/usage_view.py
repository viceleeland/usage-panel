"""Choose official Codex daily totals without mixing in local history."""
import datetime as dt


def codex_daily_rows(official, local_snapshot, today_iso):
    """Return seven dates, preferring official buckets even when cached.

    Official bucket dates are used verbatim. Only today's missing bucket can
    fall back to the same day's local counter, and its source stays explicit.
    """
    today = dt.date.fromisoformat(today_iso)
    official = official or {}
    local_snapshot = local_snapshot or {}
    buckets = {bucket['date']: bucket['total']
               for bucket in official.get('buckets', [])}
    local = local_snapshot.get('codex') or {}
    rows = []
    for offset in range(6, -1, -1):
        day = (today-dt.timedelta(days=offset)).isoformat()
        row = {'date': day, 'total': None, 'source': 'missing',
               'ok': False, 'partial': False}
        if day in buckets:
            row.update(total=buckets[day], source='official',
                       ok=bool(official.get('ok')))
        elif day == today_iso and local_snapshot.get('date') == today_iso and local.get('available'):
            total = local.get('total')
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                row.update(total=total, source='local', ok=bool(local.get('ok')),
                           partial=bool(local.get('partial')))
        rows.append(row)
    return rows


def retain_daily_usage(previous, current, provider_ok=True):
    """Keep the last official buckets and timestamp when a refresh fails."""
    previous = previous or {}
    current = current or {}
    if provider_ok and current.get('ok'):
        return dict(current)
    result = dict(current)
    result.update(ok=False, buckets=list(previous.get('buckets') or []),
                  updated=previous.get('updated'),
                  note=current.get('note', previous.get('note', '官方每日用量暂不可用')))
    return result
