"""Choose official Codex daily totals without mixing in local history."""
import datetime as dt
import math


def analytics_partial_today(snapshot, now):
    """Scope timestamped read issues to today; unknown-time issues stay global."""
    issues = snapshot.get('issues')
    if not isinstance(issues, list):
        return bool(snapshot.get('partial'))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    until = now.timestamp()
    for issue in issues:
        stamp = issue.get('timestamp') if isinstance(issue, dict) else None
        if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp) or start <= stamp <= until):
            return True
    return False


def latest_official_usage(official):
    """Select the latest reported date without relabelling it as local today."""
    return official_usage_for_date(official)


def official_range_rows(official, start_iso=None, end_iso=None):
    """Only reported official dates, with inclusive calendar bounds.

    These labels have no declared timezone. Never shift them into the local
    timezone, fill absent days with local counters, or imply a cost breakdown.
    """
    official = official if isinstance(official, dict) else {}
    rows = {}
    for row in official.get('buckets') or []:
        if not isinstance(row, dict):
            continue
        day, total = row.get('date'), row.get('total')
        try:
            valid_date = isinstance(day, str) and dt.date.fromisoformat(day).isoformat() == day
        except ValueError:
            valid_date = False
        if (not valid_date or not isinstance(total, int) or isinstance(total, bool)
                or total < 0 or (start_iso and day < start_iso) or (end_iso and day > end_iso)):
            continue
        rows[day] = {'date': day, 'total': total, 'ok': bool(official.get('ok'))}
    return [rows[day] for day in sorted(rows)]


def official_usage_for_date(official, date=None):
    """Requested official day, or latest when no day is selected."""
    rows = official_range_rows(official, date, date) if date is not None else official_range_rows(official)
    return rows[-1] if rows else None


def codex_daily_rows(official, local_snapshot, today_iso):
    """Return seven dates, preferring official buckets even when cached.

    Official bucket dates are used verbatim. Only today's missing bucket can
    fall back to the same day's local counter, and its source stays explicit.
    A service date ahead of local today extends the window; its timezone is
    unknown and its date label must not be discarded or shifted.
    """
    today = dt.date.fromisoformat(today_iso)
    official = official or {}
    local_snapshot = local_snapshot or {}
    buckets = {bucket['date']: bucket['total']
               for bucket in official.get('buckets', [])}
    end = max([today] + [dt.date.fromisoformat(date) for date in buckets])
    local = local_snapshot.get('codex') or {}
    rows = []
    for offset in range(6, -1, -1):
        day = (end-dt.timedelta(days=offset)).isoformat()
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
