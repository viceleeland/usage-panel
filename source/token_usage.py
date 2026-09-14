"""Incremental, local-only daily token counters; never retain conversation content."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class DailyTokens:
    def __init__(self, codex_home=None, claude_home=None):
        self.codex = Path(codex_home or os.environ.get('CODEX_HOME', Path.home()/'.codex'))
        self.claude = Path(claude_home or os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))
        self.files = {}
        self.day = None

    def _record(self, state, record, now, kind):
        stamp = record.get('timestamp')
        try:
            when = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if when.tzinfo is None:
                return
            day = when.astimezone(now.tzinfo).date()
        except (AttributeError, TypeError, ValueError):
            return
        if kind == 'codex':
            payload = record.get('payload') or {}
            if payload.get('type') != 'token_count':
                return
            info = payload.get('info') or {}
            total = info.get('total_token_usage')
            if not isinstance(total, dict):
                return
            keys = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens')
            values = tuple(number(total.get(k)) for k in keys)
            previous = state['previous']
            state['previous'] = values
            if not now.date()-dt.timedelta(days=6) <= day <= now.date():
                return
            if previous is not None and values[3] >= previous[3]:
                delta = tuple(max(0, v-p) for v, p in zip(values, previous))
            else:
                # A truncated log or counter reset cannot establish the lifetime delta.
                last = info.get('last_token_usage') or {}
                delta = tuple(number(last.get(k)) for k in keys)
            if delta[3]:
                # Forked/copied logs and repeated token_count notifications count once.
                state['events'][(stamp, values)] = (day, delta)
        else:
            message = record.get('message') or {}
            if record.get('type') != 'assistant' or day > now.date():
                return
            if not str(message.get('model', '')).lower().startswith('deepseek'):
                return
            usage = message.get('usage')
            identity = message.get('id') or record.get('requestId') or record.get('uuid')
            if not identity or not isinstance(usage, dict):
                return
            cache = number(usage.get('cache_read_input_tokens'))
            inputs = number(usage.get('input_tokens')) + cache + number(usage.get('cache_creation_input_tokens'))
            outputs = number(usage.get('output_tokens'))
            values = (inputs, outputs, cache, inputs+outputs)
            # A streaming message may finish after midnight or appear in copied
            # logs. Keep its earliest date and largest counters as one event.
            # Older dates are retained in memory to establish that first date.
            self._merge_event(state['events'], identity, day, values)

    @staticmethod
    def _merge_event(events, identity, day, values):
        previous_day, previous = events.get(identity, (day, (0, 0, 0, 0)))
        events[identity] = (min(day, previous_day),
                            tuple(max(a, b) for a, b in zip(previous, values)))

    def read(self, now=None):
        now = now or dt.datetime.now().astimezone()
        if now.date() != self.day:
            self.files.clear()
            self.day = now.date()
        first_day = self.day-dt.timedelta(days=6)
        start = (now-dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        days = [first_day+dt.timedelta(days=i) for i in range(7)]
        history = {day: {'date': day.isoformat()} for day in days}
        result = {'date': self.day.isoformat(), 'updated': time.time(),
                  'history': list(history.values())}
        for kind, roots in [('codex', [self.codex/'sessions', self.codex/'archived_sessions']),
                            ('deepseek', [self.claude/'projects'])]:
            available = any(p.is_dir() for p in roots)
            seen, events, failures = set(), {}, 0
            for root in roots:
                try:
                    paths = list(root.rglob('*.jsonl')) if root.is_dir() else []
                except OSError:
                    failures += 1
                    continue
                for path in paths:
                    try:
                        stat = path.stat()
                        if stat.st_mtime < start:
                            continue
                        seen.add(path)
                        state = self.files.get(path)
                        if state is None or stat.st_size < state['offset'] or stat.st_ino != state['inode'] or (
                                stat.st_size == state['offset'] and stat.st_mtime_ns != state['mtime']):
                            state = {'offset': 0, 'previous': None, 'events': {}, 'inode': stat.st_ino, 'mtime': None}
                            self.files[path] = state
                        with path.open('rb') as stream:
                            # An in-place rewrite can grow beyond the old offset. Compare
                            # the consumed boundary before treating it as an append.
                            if state['offset'] and state.get('boundary'):
                                stream.seek(max(0, state['offset']-128))
                                boundary = hashlib.sha256(stream.read(min(128, state['offset']))).digest()
                                if boundary != state['boundary']:
                                    state.update(offset=0, previous=None, events={}, partial=False)
                            stream.seek(state['offset'])
                            while line := stream.readline():
                                if not line.endswith(b'\n'):
                                    break  # Retry an unfinished write on the next refresh.
                                state['offset'] = stream.tell()
                                try:
                                    record = json.loads(line)
                                    if isinstance(record, dict):
                                        self._record(state, record, now, kind)
                                except (ValueError, TypeError, AttributeError):
                                    state['partial'] = True
                            stream.seek(max(0, state['offset']-128))
                            state['boundary'] = hashlib.sha256(stream.read(min(128, state['offset']))).digest()
                            state['mtime'] = stat.st_mtime_ns
                        if state.get('partial'):
                            failures += 1
                        for identity, (day, values) in state['events'].items():
                            self._merge_event(events, identity, day, values)
                    except OSError:
                        failures += 1
            totals = {day: [0, 0, 0, 0] for day in days}
            for day, values in events.values():
                if day in totals:
                    totals[day] = [a+b for a, b in zip(totals[day], values)]
            for day in days:
                daily = dict(zip(('input', 'output', 'cached', 'total'), totals[day]))
                daily.update(ok=available and not failures, available=available, partial=bool(failures))
                history[day][kind] = daily
            result[kind] = dict(history[self.day][kind])
            for path in list(self.files):
                if any(path.is_relative_to(root) for root in roots) and path not in seen:
                    del self.files[path]
        return result
