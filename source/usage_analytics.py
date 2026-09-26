"""Incremental local usage attribution. Retains counters/metadata, never messages.

Recent Codex versions emit one ``token_usage_record`` per response as well as
the older cumulative ``token_count`` notifications. Prefer response records
for each turn; adding both would double count the same requests.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import uuid


_KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
         'reasoning_output_tokens', 'total_tokens')
_FIELDS = ('input', 'cached', 'output', 'reasoning', 'total')
_GPT55_MODELS = {'gpt-5.5', 'gpt-5.5-2026-04-23'}


def _stamp(value):
    try:
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _text(value):
    return value if isinstance(value, str) and value else None


def _usage(value):
    if not isinstance(value, dict):
        return None
    # Missing essential counters must not masquerade as confirmed zero usage.
    if any(not isinstance(value.get(k), int) or isinstance(value.get(k), bool)
           or value[k] < 0 for k in ('input_tokens', 'output_tokens', 'total_tokens')):
        return None
    if value['total_tokens'] != value['input_tokens'] + value['output_tokens']:
        return None
    result = []
    for key in _KEYS:
        item = value.get(key, 0)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            if key == 'cached_input_tokens':
                result.append(0)  # Output preserves this counter as unknown.
                continue
            return None
        result.append(item)
    return tuple(result)


def _cache_known(value):
    if not isinstance(value, dict):
        return False
    cached, inputs = value.get('cached_input_tokens'), value.get('input_tokens')
    return (isinstance(cached, int) and not isinstance(cached, bool)
            and isinstance(inputs, int) and 0 <= cached <= inputs)


class UsageAnalytics:
    def __init__(self, codex_home=None):
        self.codex = Path(codex_home or os.environ.get('CODEX_HOME', Path.home()/'.codex'))
        self.files = {}
        self._index_stat = None
        self._titles = {}

    def _read_titles(self):
        """Task names are explicit metadata; never infer them from messages."""
        path = self.codex/'session_index.jsonl'
        try:
            stat = path.stat()
            fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if fingerprint == self._index_stat:
                return self._titles
            titles = {}
            with path.open('rb') as stream:
                for line in stream:
                    if not line.endswith(b'\n'):
                        break
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            continue
                        identity = str(uuid.UUID(item.get('id')))
                        title = _text(item.get('thread_name'))
                        if title and title.strip():
                            titles[identity] = title.strip()[:512]
                    except (ValueError, TypeError, AttributeError):
                        continue
            self._titles = titles
            self._index_stat = fingerprint
        except OSError:
            # A missing title index does not invalidate recorded token usage.
            return {}
        return self._titles

    @staticmethod
    def _state(path, stat):
        unknown = 'unknown:' + hashlib.sha256(str(path).encode('utf8')).hexdigest()[:16]
        return {'offset': 0, 'inode': stat.st_ino, 'mtime': None, 'boundary': None,
                'owner': unknown, 'meta': None, 'contexts': {}, 'context': {},
                'previous': None, 'previous_cache_known': False, 'events': {}, 'modern_turns': set(),
                'issues': set()}

    @staticmethod
    def _issue(state, stamp, reason, source=None, owner=None, turn=None):
        state['issues'].add((stamp, reason, source, owner or state['owner'],
                             turn or state['context'].get('turn_id')))

    def _record(self, state, record):
        payload = record.get('payload')
        if not isinstance(payload, dict):
            return
        kind = record.get('type')
        stamp = _stamp(record.get('timestamp'))
        if kind == 'session_meta':
            # Forked history can embed the parent's metadata AFTER the child's.
            if state['meta'] is not None:
                return
            source = payload.get('source')
            sub = source.get('subagent', {}) if isinstance(source, dict) else {}
            spawn = sub.get('thread_spawn', {}) if isinstance(sub, dict) else {}
            if not isinstance(spawn, dict):
                spawn = {}
            parent = _text(payload.get('parent_thread_id')) or _text(spawn.get('parent_thread_id'))
            owner = _text(payload.get('id'))
            role = 'subagent' if parent else ('main' if owner else 'unknown')
            if payload.get('thread_source') == 'subagent' and not parent:
                role = 'unknown'
            state['owner'] = owner or state['owner']
            state['meta'] = {
                'id': state['owner'], 'parent_id': parent,
                'root_hint': _text(payload.get('session_id')),
                'agent_path': _text(payload.get('agent_path')) or _text(spawn.get('agent_path')),
                'role': role, 'created': _stamp(payload.get('timestamp')) or stamp,
                'forked': bool(payload.get('forked_from_id') or parent),
            }
            return
        if kind == 'turn_context':
            context = {'turn_id': _text(payload.get('turn_id')),
                       'model': _text(payload.get('model')), 'effort': _text(payload.get('effort'))}
            state['context'] = context
            if context['turn_id']:
                state['contexts'][context['turn_id']] = context
            return
        if stamp is None:
            if kind == 'token_usage_record' or (payload.get('type') == 'token_count'
                                               and isinstance(payload.get('info'), dict)):
                self._issue(state, None, '部分用量记录时间无效，无法确定归属周期。',
                            'cumulative' if payload.get('type') == 'token_count' else 'response',
                            _text(payload.get('thread_id')), _text(payload.get('turn_id')))
            return
        owner = state['owner']
        meta = state['meta'] or {}
        inherited = (meta.get('forked') and meta.get('created') is not None
                     and stamp < meta['created'])
        if kind == 'token_usage_record':
            if inherited:
                return
            values = _usage(payload.get('usage'))
            identity = _text(payload.get('response_id'))
            if values is None or not identity:
                self._issue(state, stamp, '部分单次用量记录的计数或标识无效。', 'response',
                            _text(payload.get('thread_id')), _text(payload.get('turn_id')))
                return
            thread = _text(payload.get('thread_id')) or owner
            if state['meta'] is None and _text(payload.get('thread_id')):
                state['owner'] = thread
            turn = _text(payload.get('turn_id')) or state['context'].get('turn_id')
            if not state['context'].get('turn_id'):
                state['context'] = {'turn_id': turn, 'model': None, 'effort': None}
            state['modern_turns'].add((thread, turn))
            context = state['contexts'].get(turn, {})
            event = self._event(stamp, thread, turn, values, context, values[0], 'response')
            if not _cache_known(payload.get('usage')):
                event['cached'] = None
            event['_root_hint'] = _text(payload.get('session_id'))
            state['events'][('response', identity)] = event
            return
        if payload.get('type') != 'token_count':
            return
        info = payload.get('info')
        if not isinstance(info, dict):
            return  # Quota-only notifications have no token usage.
        total = _usage(info.get('total_token_usage'))
        last = _usage(info.get('last_token_usage'))
        if total is None:
            if not inherited:
                self._issue(state, stamp, '部分累计用量记录的计数无效。', 'cumulative')
            return
        previous = state['previous']
        previous_cache_known = state['previous_cache_known']
        state['previous'] = total
        state['previous_cache_known'] = _cache_known(info.get('total_token_usage'))
        if inherited:
            return
        if previous is not None and all(v >= p for v, p in zip(total, previous)):
            values = tuple(v-p for v, p in zip(total, previous))
            request_input = last[0] if last == values else None
            cache_known = state['previous_cache_known'] and previous_cache_known
        else:
            if last is None:
                self._issue(state, stamp, '部分旧日志缺少可辨识的单次用量。', 'cumulative')
                return
            values = last
            request_input = last[0]
            cache_known = _cache_known(info.get('last_token_usage'))
            if previous is None and total != last:
                self._issue(state, stamp, '部分旧日志缺少起始累计基线，仅计入可辨识的后续用量。',
                            'cumulative')
        if not values[-1]:
            return
        context = state['context']
        turn = context.get('turn_id')
        event = self._event(stamp, owner, turn, values, context, request_input, 'cumulative')
        if not cache_known:
            event['cached'] = None
        # Same owner + original timestamp + counters deduplicates copied logs,
        # without conflating different agents making equally sized requests.
        state['events'][('cumulative', owner, stamp, total)] = event

    @staticmethod
    def _event(stamp, owner, turn, values, context, request_input, source):
        return {'timestamp': stamp, 'session_id': owner, 'model': context.get('model'),
                'effort': context.get('effort'), 'request_input': request_input,
                **dict(zip(_FIELDS, values)), '_turn_id': turn, '_source': source}

    def read(self, now=None, since=None):
        now = now or dt.datetime.now().astimezone()
        since = since or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.tzinfo is None or since.tzinfo is None:
            raise ValueError('now and since must include a timezone')
        first, end = since.timestamp(), now.timestamp()
        roots = [self.codex/'sessions', self.codex/'archived_sessions']
        available = any(root.is_dir() for root in roots)
        seen, notes, scan_issues = set(), set(), set()
        for root in roots:
            try:
                paths = list(root.rglob('*.jsonl')) if root.is_dir() else []
            except OSError:
                scan_issues.add((None, '部分日志目录无法读取，无法确定缺失用量的周期。'))
                continue
            for path in paths:
                try:
                    stat = path.stat()
                    if stat.st_mtime < first:
                        continue
                    seen.add(path)
                    state = self.files.get(path)
                    if state is None or stat.st_size < state['offset'] or stat.st_ino != state['inode'] or (
                            stat.st_size == state['offset'] and stat.st_mtime_ns != state['mtime']):
                        state = self._state(path, stat)
                        self.files[path] = state
                    with path.open('rb') as stream:
                        if state['offset'] and state['boundary']:
                            stream.seek(max(0, state['offset']-4096))
                            boundary = hashlib.sha256(stream.read(min(4096, state['offset']))).digest()
                            if boundary != state['boundary']:
                                state = self._state(path, stat)
                                self.files[path] = state
                        stream.seek(state['offset'])
                        while line := stream.readline():
                            if not line.endswith(b'\n'):
                                break
                            state['offset'] = stream.tell()
                            try:
                                record = json.loads(line)
                                if isinstance(record, dict):
                                    self._record(state, record)
                            except (ValueError, TypeError, AttributeError):
                                self._issue(state, None, '部分日志记录无法解析，无法确定归属周期。')
                        stream.seek(max(0, state['offset']-4096))
                        state['boundary'] = hashlib.sha256(stream.read(min(4096, state['offset']))).digest()
                        state['mtime'] = stat.st_mtime_ns
                except OSError:
                    scan_issues.add((None, '部分日志文件无法读取，无法确定缺失用量的周期。'))
        for path in list(self.files):
            if path not in seen:
                del self.files[path]

        sessions, events, modern_turns = {}, {}, set()
        for state in self.files.values():
            meta = state['meta']
            if meta:
                sessions[meta['id']] = dict(meta)
            for identity, event in state['events'].items():
                # Retain pre-window events here for session-wide long-context
                # pricing state; only the returned event list is period-sliced.
                if event['timestamp'] <= end:
                    if event['_source'] == 'response':
                        modern_turns.add((event['session_id'], event['_turn_id']))
                    previous = events.get(identity)
                    if previous is None or (not previous.get('model') and event.get('model')):
                        events[identity] = dict(event)
        kept, history = [], []
        for event in events.values():
            owner = event['session_id']
            if event['_source'] == 'cumulative' and (owner, event['_turn_id']) in modern_turns:
                continue
            if owner not in sessions:
                sessions[owner] = {'id': owner, 'parent_id': None, 'root_hint': event.get('_root_hint'),
                                   'agent_path': None, 'role': 'unknown'}
            history.append(event)
            if first <= event['timestamp']:
                kept.append(event)

        issues = set(scan_issues)
        uncertain_owners = set()
        for state in self.files.values():
            for stamp, reason, source, owner, turn in state['issues']:
                if source == 'cumulative' and (owner, turn) in modern_turns:
                    continue  # The preferred per-response source is complete.
                if stamp is None or stamp <= end:
                    uncertain_owners.add(owner)
                if stamp is None or first <= stamp <= end:
                    issues.add((stamp, reason))

        for owner, session in sessions.items():
            session['gpt55_price_uncertain'] = bool(scan_issues or owner in uncertain_owners
                or session.get('created') is None or session.get('role') == 'unknown')
        for event in history:
            request = event.get('request_input')
            if event.get('model') is None or (event.get('model') in _GPT55_MODELS and (
                    not isinstance(request, int) or isinstance(request, bool) or request > 272_000)):
                sessions[event['session_id']]['gpt55_price_uncertain'] = True

        def root_id(owner):
            current, visited = owner, set()
            while current in sessions:
                if current in visited:
                    return None
                visited.add(current)
                item = sessions[current]
                parent = item.get('parent_id')
                if not parent:
                    # Root hints are useful for an orphaned subagent record,
                    # but a user-created fork remains a distinct main task.
                    hint = item.get('root_hint') if item['role'] != 'main' else None
                    return hint or current
                if parent not in sessions:
                    hint = item.get('root_hint')
                    return hint if hint and hint != current else parent
                current = parent
            return current

        for owner, session in sessions.items():
            session['root_id'] = root_id(owner)
            if session['root_id'] is None:
                session['role'] = 'unknown'
                session['gpt55_price_uncertain'] = True
        for root in {item['root_id'] for item in sessions.values()}:
            if root and root not in sessions:
                sessions[root] = {'id': root, 'parent_id': None, 'root_id': root,
                                  'agent_path': None, 'role': 'unknown', 'gpt55_price_uncertain': True}
        for event in kept:
            session = sessions[event['session_id']]
            if session['root_id'] is None:
                issues.add((event['timestamp'], '部分 Agent 关系存在循环，任务归属未知。'))
            event.update({k: session.get(k) for k in ('root_id', 'parent_id', 'agent_path', 'role')})
            for key in tuple(event):
                if key.startswith('_'):
                    del event[key]
        for session in sessions.values():
            for key in ('created', 'forked', 'root_hint'):
                session.pop(key, None)
        titles = self._read_titles()
        for owner, session in sessions.items():
            session['title'] = titles.get(owner)
        if any(e['model'] is None for e in kept):
            notes.add('部分用量未记录模型，保持未知。')
        if any(e['request_input'] is None for e in kept):
            notes.add('部分累计用量无法还原单次输入，长上下文价格不可确定。')
        if any(e['cached'] is None for e in kept):
            notes.add('部分用量未记录可靠的缓存输入，费用保持未知。')
        notes.update(reason for _, reason in issues)
        issue_rows = [{'timestamp': stamp, 'reason': reason} for stamp, reason in
                      sorted(issues, key=lambda item: (item[0] is not None, item[0] or 0, item[1]))]
        kept.sort(key=lambda item: (item['timestamp'], item['session_id']))
        return {'available': available, 'partial': bool(issues), 'updated': time.time(),
                'events': kept, 'sessions': sessions, 'notes': sorted(notes), 'issues': issue_rows}
