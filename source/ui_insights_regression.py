"""Offline real-Tk regression for the usage dashboard, with synthetic data only.

Run with the desktop test environment (Tk, Pillow and pystray installed).
Each scenario owns one child process/Tk interpreter, so native shutdown
failures are reported by the parent rather than corrupting subsequent tests.
"""
import argparse
import csv
import ctypes
import datetime as dt
import gc
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
import weakref
from unittest.mock import patch


NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.datetime.now().astimezone().tzinfo)


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def fixture():
    def event(owner, root, role, model, effort, inputs, outputs, cached,
              today=True, minutes=20, path=None):
        stamp = NOW-dt.timedelta(minutes=minutes) if today else NOW-dt.timedelta(days=1)
        return {'timestamp': stamp.timestamp(), 'session_id': owner, 'root_id': root,
                'parent_id': root if role == 'subagent' else None,
                'agent_path': path, 'role': role, 'model': model, 'effort': effort,
                'input': inputs, 'output': outputs, 'cached': cached,
                'reasoning': min(outputs, 7), 'total': inputs+outputs,
                'request_input': inputs}
    events = [
        event('alpha', 'alpha', 'main', 'gpt-6-astra', 'ultra', 200_000, 5_000, 160_000),
        event('alpha', 'alpha', 'main', 'gpt-6-astra', 'ultra', 100, 10, 0),
        event('alpha', 'alpha', 'main', 'gpt-6-sol', 'high', 5_000, 500, None, today=False),
        event('alpha-research', 'alpha', 'subagent', 'gpt-6-luna', 'max',
              12_000, 800, 8_000, path='/root/research'),
        event('beta', 'beta', 'main', 'gpt-6-sol', 'medium', 9_000_000, 500_000,
              7_000_000, today=False),
        event('beta-review', 'beta', 'subagent', 'gpt-6-sol', 'high', 1_000_000,
              10_000, 800_000, path='/root/review'),
        event('sort-low', 'sort-low', 'main', 'gpt-6-astra', 'high', 900, 101, 500),
        event('sort-high', 'sort-high', 'main', 'gpt-6-astra', 'high', 1_300, 199, 500),
        event('unknown', 'unknown', 'unknown', None, None, 700, 70, None),
    ]
    titles = {'alpha': '示例 · 架构审查', 'beta': '=1+1 · 示例导出',
              'sort-low': '示例 · 排序 1001', 'sort-high': '示例 · 排序 1499',
              'unknown': '示例 · 未知归属'}
    sessions = {e['session_id']: {'id': e['session_id'], 'root_id': e['root_id'],
                 'parent_id': e['parent_id'], 'role': e['role'], 'agent_path': e['agent_path'],
                 'title': titles.get(e['session_id'])} for e in events}
    return {'available': True, 'partial': False, 'updated': NOW.timestamp(),
            'events': events, 'sessions': sessions, 'notes': []}


def setup():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    import app
    import insights_ui
    from usage_costs import build_report
    # Replace only this module's clock, leaving the global datetime module intact.
    insights_ui.dt = SimpleNamespace(datetime=FixedDateTime, date=dt.date, timedelta=dt.timedelta)
    panel = app.UsagePanel.__new__(app.UsagePanel)
    panel.args = SimpleNamespace(smoke=False, hidden=True, insights=False)
    panel.root = app.tk.Tk()
    panel.root.withdraw()
    panel.root.tk.call('tk', 'scaling', 1.30)
    panel.settings = {'billing_anchor': '2026-09-21', 'monthly_budget': 250,
                      'radar_mode': 'overall', 'topmost': False, 'alerts_enabled': True,
                      'unrelated_setting': {'keep': 'unchanged'}}
    panel.radar_mode = app.tk.StringVar(panel.root, value='overall')
    panel.topmost = app.tk.BooleanVar(panel.root, value=False)
    panel.alerts_enabled = app.tk.BooleanVar(panel.root, value=True)
    panel.analytics_snapshot = fixture()
    panel.analytics_report = build_report(panel.analytics_snapshot, '2026-09-21', NOW)
    panel.insights_window = None
    panel.cost_label = None
    panel.cost_note = None
    panel.token_after_id = None
    panel.closed = False
    panel.refresh_calls = 0
    panel.tray = SimpleNamespace(stop=lambda: None)

    def refresh_tokens():
        panel.refresh_calls += 1

    panel.refresh_tokens = refresh_tokens
    errors = []
    panel.root.report_callback_exception = lambda kind, value, tb: errors.append(f'{kind.__name__}: {value}')
    return panel, errors


def open_window(panel):
    window = panel.show_insights()
    panel.root.update()
    return panel.insights_window, window


def dispose(panel):
    if not panel.closed:
        panel.quit()
    # Break the synthetic refresh closure on the owning thread.
    panel.refresh_tokens = None
    gc.collect()


def dashboard(screenshot=None):
    from insights_ui import tokens
    panel, errors = setup()
    try:
        view, window = open_window(panel)
        all_events = panel.analytics_snapshot['events']
        assert len(view.selected_events()) == len(all_events)
        assert panel.show_insights() is window, 'Opening twice created another dashboard'
        expected = sum(e['total'] for e in all_events)
        assert view.metrics['total'][0].cget('text') == tokens(expected)
        for selected_role, expected_role in [('主 Agent', 'main'), ('子 Agent', 'subagent'),
                                             ('归属未知', 'unknown')]:
            view.role.set(selected_role)
            view.render_tables()
            assert view.selected_events()
            assert all(e['role'] == expected_role for e in view.selected_events())
        view.role.set('全部角色')
        view.model.set('gpt-6-luna')
        view.render_tables()
        assert len(view.selected_events()) == 1
        assert view.selected_events()[0]['session_id'] == 'alpha-research'
        view.model.set('全部模型')
        view.query.set('架构审查')
        view.render_tables()
        assert {e['root_id'] for e in view.selected_events()} == {'alpha'}, 'Root task title did not match its agents'
        view.query.set('RESEARCH')
        view.render_tables()
        assert [e['session_id'] for e in view.selected_events()] == ['alpha-research']
        view.query.set('')
        view.scope.set('今日')
        view.render_tables()
        assert len(view.selected_events()) == len(all_events)-2
        assert all(dt.datetime.fromtimestamp(e['timestamp']).date() == NOW.date() for e in view.selected_events())
        view.scope.set('最近 7 天')
        view.render_tables()
        assert len(view.selected_events()) == len(all_events)
        view.scope.set('本周期')
        view.query.set('definitely-no-synthetic-task')
        view.render_tables()
        assert not view.tasks.get_children() and not view.models.get_children()
        assert view.export_data[view.tasks][1] == []
        view.query.set('')
        view.render_tables()
        for parent in view.tasks.get_children():
            view.tasks.item(parent, open=True)
        assert view.aggregate([e for e in all_events if e['cached'] is None])['cached'] is None
        assert '?' in view.metrics['cost'][0].cget('text'), 'Unknown usage was displayed as fully priced'
        if screenshot:
            from PIL import ImageGrab
            target = Path(screenshot).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            window.attributes('-topmost', True)
            window.lift()
            panel.root.update()
            time.sleep(.25)
            panel.root.update()
            box = (window.winfo_rootx(), window.winfo_rooty(),
                   window.winfo_rootx()+window.winfo_width(), window.winfo_rooty()+window.winfo_height())
            ImageGrab.grab(bbox=box).save(target)
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: dashboard filters, task search, unknown cache, and empty state'
          + ('; synthetic screenshot saved' if screenshot else ''))


def exports():
    import insights_ui
    panel, errors = setup()
    try:
        view, _ = open_window(panel)
        total = sum(e['total'] for e in panel.analytics_snapshot['events'])
        columns, rows = view.export_data[view.tasks]
        total_index = columns.index('total_tokens')
        assert sum(row[total_index] for row in rows) == total, 'Parent totals were added again to exported agent rows'
        keys = [(r[columns.index('task_id')], r[columns.index('agent_id')],
                 r[columns.index('model')], r[columns.index('effort')]) for r in rows]
        assert len(keys) == len(set(keys))
        assert len(rows) == len(panel.analytics_snapshot['events'])-1, 'Same agent/model requests were not aggregated'
        for table in (view.models, view.days):
            names, table_rows = view.export_data[table]
            assert sum(row[names.index('total_tokens')] for row in table_rows) == total
        # Tree totals and children agree independently for every root task.
        for parent in view.tasks.get_children():
            task_title = view.tasks.item(parent, 'text')
            root = next(k for k, v in panel.analytics_snapshot['sessions'].items() if v['title'] == task_title)
            task_total = sum(e['total'] for e in panel.analytics_snapshot['events'] if e['root_id'] == root)
            assert view.tasks.set(parent, 'total') == insights_ui.tokens(task_total)
            exported = [r for r in rows if r[columns.index('task_id')] == root]
            assert sum(r[total_index] for r in exported) == task_total
        with tempfile.TemporaryDirectory() as folder:
            csv_path, json_path = Path(folder)/'synthetic.csv', Path(folder)/'synthetic.json'
            for path in (csv_path, json_path):
                with patch.object(insights_ui.filedialog, 'asksaveasfilename', return_value=str(path)):
                    view.tabs.select(0)
                    view.export()
                assert path.is_file()
            with csv_path.open(encoding='utf-8-sig', newline='') as stream:
                csv_rows = list(csv.DictReader(stream))
            assert sum(int(r['total_tokens']) for r in csv_rows) == total
            assert all(r['task_title'].startswith("'") for r in csv_rows if '示例导出' in r['task_title'])
            json_rows = json.loads(json_path.read_text(encoding='utf8'))
            assert sum(r['total_tokens'] for r in json_rows) == total
            assert all(isinstance(r['total_tokens'], int) for r in json_rows)
            assert any(r['cached_input_tokens'] is None for r in json_rows)
            assert any(r['task_title'].startswith('=') for r in json_rows), 'JSON metadata was changed into spreadsheet text'
            for dangerous in ('=1+1', '+SUM(1,1)', '-2+3', '@SUM(1,1)', ' \t=1+1', '\t=1+1', '\r=1+1'):
                assert insights_ui.safe_cell(dangerous).startswith("'"), repr(dangerous)
            view.role.set('子 Agent')
            view.render_tables()
            subset_columns, subset_rows = view.export_data[view.tasks]
            assert sum(r[subset_columns.index('total_tokens')] for r in subset_rows) == sum(
                e['total'] for e in panel.analytics_snapshot['events'] if e['role'] == 'subagent')
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: task/model/day export sums, unique agent rows, CSV formula safety, and raw JSON counters')


def settings():
    import app
    panel, errors = setup()
    try:
        view, _ = open_window(panel)
        before = dict(panel.settings)
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'DATA', Path(folder)):
            view.anchor.set('2026-09-22')
            view.budget.set('345.67')
            view.save()
            saved = json.loads((Path(folder)/'settings.json').read_text(encoding='utf8'))
            assert saved['billing_anchor'] == '2026-09-22'
            assert saved['monthly_budget'] == 345.67
            for key, value in before.items():
                if key not in ('billing_anchor', 'monthly_budget'):
                    assert saved[key] == value, f'Unrelated setting changed: {key}'
            assert panel.refresh_calls == 1
            good = dict(panel.settings)
            for anchor, budget in [('invalid-date', '20'), ('2026-09-21', '-1'),
                                   ('2026-09-21', 'nan'), ('2026-09-21', 'inf')]:
                view.anchor.set(anchor)
                view.budget.set(budget)
                view.save()
                assert panel.settings == good, 'Invalid settings mutated prior configuration'
                assert panel.refresh_calls == 1
            view.anchor.set('2026-09-23')
            view.budget.set('500')
            with patch.object(panel, 'save_settings', side_effect=OSError('synthetic denied write')):
                view.save()
            assert panel.settings == good, 'Failed persistence left unsaved settings active'
            assert json.loads((Path(folder)/'settings.json').read_text(encoding='utf8')) == saved
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: actual settings writer preserves unrelated settings, rejects invalid values, and rolls back I/O failure')


def sorting():
    panel, errors = setup()
    try:
        view, _ = open_window(panel)
        expected = {meta['title']: sum(e['total'] for e in panel.analytics_snapshot['events'] if e['root_id'] == owner)
                    for owner, meta in panel.analytics_snapshot['sessions'].items() if meta['title']}
        view.sort(view.tasks, 'total', False)
        values = [expected[view.tasks.item(i, 'text')] for i in view.tasks.get_children()]
        assert values == sorted(values), f'Ascending token sort used rounded display text: {values}'
        view.sort(view.tasks, 'total', True)
        values = [expected[view.tasks.item(i, 'text')] for i in view.tasks.get_children()]
        assert values == sorted(values, reverse=True), values
        view.render_tables()
        values = [expected[view.tasks.item(i, 'text')] for i in view.tasks.get_children()]
        assert values == sorted(values, reverse=True), 'Refresh lost numeric sort'
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: exact numeric token sorting including equal rounded displays, and sort retention across refresh')


def scopes():
    from insights_ui import cost_text, tokens
    from usage_costs import build_report
    panel, errors = setup()
    try:
        snapshot = panel.analytics_snapshot
        day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        cycle = day.replace(day=21)
        week = day-dt.timedelta(days=6)

        def add(identity, when, inputs):
            event = dict(snapshot['events'][0], session_id=identity, root_id=identity,
                         timestamp=when.timestamp(), input=inputs, cached=0,
                         output=1, total=inputs+1, request_input=inputs)
            snapshot['events'].append(event)
            snapshot['sessions'][identity] = {'id': identity, 'root_id': identity,
                                              'title': 'Synthetic boundary '+identity}

        add('before-week', week-dt.timedelta(seconds=1), 10)
        add('at-week', week, 20)
        add('before-cycle', cycle-dt.timedelta(seconds=1), 30)
        add('at-cycle', cycle, 40)
        add('before-today', day-dt.timedelta(seconds=1), 50)
        add('at-today', day, 60)
        add('future', NOW+dt.timedelta(seconds=1), 70)
        add('at-cycle-end', day.replace(month=10, day=21), 80)
        valid_events = list(snapshot['events'])
        # Raw input deliberately differs from the report: prior-cycle, future,
        # malformed timestamps, contradictory counters, and a non-object row.
        snapshot['events'].extend([
            dict(valid_events[0], timestamp='broken timestamp'),
            dict(valid_events[0], timestamp=float('nan')),
            dict(valid_events[0], total=999_999_999), None])
        panel.analytics_report = report = build_report(snapshot, '2026-09-21', NOW)
        assert report['partial'], 'Malformed input fixture did not exercise report validation'
        view, _ = open_window(panel)
        assert view.metrics['total'][0].cget('text') == tokens(report['total_tokens'])
        assert view.metrics['cost'][0].cget('text') == cost_text(report)
        expected_main = sum(t['main_tokens'] for t in report['tasks'])
        expected_sub = sum(t['subagent_tokens'] for t in report['tasks'])
        assert view.metrics['total'][1].cget('text') == f'主 {tokens(expected_main)} / 子 {tokens(expected_sub)}'
        for scope, lower in [('本周期', cycle), ('最近 7 天', week), ('今日', day)]:
            view.scope.set(scope)
            view.render_tables()
            selected = view.selected_events()
            expected = [e for e in valid_events if lower.timestamp() <= e['timestamp'] <= NOW.timestamp()]
            assert selected == expected, f'{scope} crossed its exact time boundary'
            names, rows = view.export_data[view.tasks]
            assert sum(r[names.index('total_tokens')] for r in rows) == sum(e['total'] for e in expected)
            assert view.metrics['total'][0].cget('text') == tokens(report['total_tokens']), 'Table scope changed cycle card'
        view.scope.set('最近 7 天')
        selected_ids = {e['session_id'] for e in view.selected_events()}
        assert {'at-week', 'before-cycle'}.issubset(selected_ids)
        assert 'before-week' not in selected_ids
        view.scope.set('今日')
        selected_ids = {e['session_id'] for e in view.selected_events()}
        assert 'at-today' in selected_ids and 'before-today' not in selected_ids
        assert 'future' not in selected_ids and 'at-cycle-end' not in selected_ids
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: report-backed cards, malformed raw input, strict cycle/today bounds, and seven days across prior cycle')


def stale_anchor():
    import app
    from usage_costs import build_report
    panel, errors = setup()
    try:
        view, _ = open_window(panel)
        panel.messages = queue.Queue()
        panel.token_busy = True
        panel.cost_label = app.tk.Label(panel.root, text='SYNTHETIC OLD COST $123.45')
        panel.cost_note = app.tk.Label(panel.root, text='SYNTHETIC OLD PERIOD')
        starts, label_updates = [], []

        def controlled_refresh():
            if panel.closed or panel.token_busy:
                return
            panel.token_busy = True
            starts.append(panel.settings['billing_anchor'])

        panel.refresh_tokens = controlled_refresh
        def update_cost_labels():
            label_updates.append(panel.analytics_report.get('anchor_date'))
            app.UsagePanel.update_cost_labels(panel)

        panel.update_cost_labels = update_cost_labels
        previous_snapshot = panel.analytics_snapshot
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'DATA', Path(folder)):
            view.anchor.set('2026-09-22')
            view.save()
        assert panel.settings['billing_anchor'] == '2026-09-22'
        assert panel.analytics_report == {} and panel.token_busy and starts == []
        assert '123.45' not in panel.cost_label.cget('text'), 'Compact panel kept the old period cost while pending'
        assert '正在读取' in panel.cost_label.cget('text')
        assert not view.tasks.get_children() and view.export_data[view.tasks][1] == []
        # The dashboard's existing timer must also turn its cards into an
        # explicit pending state without adding another recurring timer.
        panel.root.after(1600, panel.root.quit)
        panel.root.mainloop()
        assert view.metrics['cost'][0].cget('text') == '—'
        assert view.metrics['total'][0].cget('text') == '—'
        assert '正在' in view.period_label.cget('text')
        stale_snapshot = dict(fixture(), updated=NOW.timestamp()+1)
        stale_report = build_report(stale_snapshot, '2026-09-21', NOW)
        panel.messages.put(('analytics', ('2026-09-21', stale_snapshot, stale_report)))
        panel.poll()
        assert panel.analytics_snapshot is previous_snapshot
        assert panel.analytics_report == {}, 'Old anchor result replaced the cleared report'
        assert label_updates == [None]
        # Pump the actual Tk timer scheduled by app.poll for the replacement scan.
        deadline = time.monotonic()+2
        while not starts and time.monotonic() < deadline:
            panel.root.update()
            time.sleep(.005)
        assert starts == ['2026-09-22'] and panel.token_busy
        fresh_snapshot = dict(fixture(), updated=NOW.timestamp()+2)
        fresh_report = build_report(fresh_snapshot, '2026-09-22', NOW)
        panel.messages.put(('analytics', ('2026-09-22', fresh_snapshot, fresh_report)))
        panel.poll()
        assert panel.analytics_snapshot is fresh_snapshot
        assert panel.analytics_report is fresh_report
        assert label_updates == [None, '2026-09-22']
        # An even later obsolete completion still cannot overwrite accepted data.
        panel.messages.put(('analytics', ('2026-09-21', stale_snapshot, stale_report)))
        panel.poll()
        assert panel.analytics_snapshot is fresh_snapshot and panel.analytics_report is fresh_report
        assert label_updates == [None, '2026-09-22']

        # Exercise the real refresh method while replacing only its I/O and
        # thread launcher. Eight manual clicks/completions must maintain one
        # token refresh timer, never eight future concurrent scans.
        reads = []
        panel.messages = queue.Queue()
        panel.token_reader = SimpleNamespace(read=lambda: {'history': []})
        panel.analytics_reader = SimpleNamespace(read=lambda **kw: (reads.append(kw) or fixture()))
        panel.update_token_labels = lambda: None
        panel.refresh_tokens = MethodType(app.UsagePanel.refresh_tokens, panel)
        panel.token_busy = False
        with patch.object(app.threading, 'Thread', side_effect=lambda **kw: SimpleNamespace(start=kw['target'])):
            for iteration in range(8):
                prior_timer = panel.token_after_id
                panel.refresh_tokens()
                assert panel.token_busy and len(reads) == iteration+1
                assert panel.token_after_id is None
                if prior_timer is not None:
                    assert prior_timer not in panel.root.tk.call('after', 'info')
                panel.refresh_tokens()
                assert len(reads) == iteration+1, 'Busy refresh spawned a duplicate scan'
                panel.poll()
                timers = panel.root.tk.call('after', 'info')
                token_timers = [timer for timer in timers
                                if 'refresh_tokens' in str(panel.root.tk.call('after', 'info', timer))]
                assert token_timers == [panel.token_after_id], f'Duplicate refresh timers: {token_timers}'
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: pending save clears old costs/exports, stale anchors cannot overwrite data, and eight manual refreshes retain one timer')


def layout():
    panel, errors = setup()
    try:
        view, window = open_window(panel)
        for width, height in ((1220, 870), (900, 620)):
            window.geometry(f'{width}x{height}+20+20')
            panel.root.update()
            frame = view.settings_frame
            frame_top = frame.winfo_rooty()-window.winfo_rooty()
            frame_bottom = frame_top+frame.winfo_height()
            assert frame.winfo_ismapped(), f'Settings hidden at {width}x{height}'
            assert 0 <= frame_top < frame_bottom <= window.winfo_height(), (
                f'Settings outside {window.winfo_width()}x{window.winfo_height()}: {frame_top}..{frame_bottom}')
            buttons = [child for child in frame.winfo_children()
                       if child.winfo_class() == 'Button' and child.cget('text') in ('保存', '定价与统计说明')]
            assert len(buttons) == 2
            for child in buttons:
                top = child.winfo_rooty()-window.winfo_rooty()
                left = child.winfo_rootx()-window.winfo_rootx()
                assert child.winfo_ismapped() and child.winfo_height() >= 20
                assert 0 <= top and top+child.winfo_height() <= window.winfo_height()
                assert 0 <= left and left+child.winfo_width() <= window.winfo_width()
            assert view.tabs.winfo_height() >= 70, (
                f'Table has only {view.tabs.winfo_height()}px at '
                f'{window.winfo_width()}x{window.winfo_height()} supported minimum size')
        assert not errors, errors
    finally:
        dispose(panel)
    print('PASS: settings/save/help and usable table remain inside both 1220x870 and supported 900x620 layouts')


def lifecycle():
    import app
    panel, errors = setup()
    original_del = app.tk.Variable.__del__
    finalized = []
    rounds = [0]
    failures = []

    def variable_del(variable):
        finalized.append(threading.current_thread() is threading.main_thread())
        original_del(variable)

    def iteration():
        before = set(panel.root.tk.call('after', 'info'))
        view, window = open_window(panel)
        reference = weakref.ref(view)
        window.destroy()
        assert panel.insights_window is None
        assert set(panel.root.tk.call('after', 'info')) == before, 'Closed dashboard retained a scheduled callback'
        del view, window
        done = threading.Event()

        def collect():
            gc.collect()
            done.set()

        threading.Thread(target=collect, daemon=True).start()
        deadline = time.monotonic()+5

        def wait_for_gc():
            if not done.is_set() and time.monotonic() < deadline:
                panel.root.after(10, wait_for_gc)
                return
            if not done.is_set():
                failures.append('Background collection did not finish')
            if reference() is not None:
                failures.append('A closed dashboard is still retained')
            rounds[0] += 1
            if rounds[0] < 20 and not failures and not errors:
                panel.root.after(1, iteration)
            else:
                panel.root.quit()

        panel.root.after(10, wait_for_gc)

    try:
        with patch.object(app.tk.Variable, '__del__', variable_del):
            panel.root.after(1, iteration)
            watchdog = panel.root.after(25000, lambda: (failures.append('Lifecycle watchdog expired'), panel.root.quit()))
            panel.root.mainloop()
            panel.root.after_cancel(watchdog)
            assert not failures and not errors, failures+errors
            assert rounds[0] == 20, rounds
            assert len(finalized) == 140 and all(finalized), (
                f'Expected 140 dashboard variables finalized on Tk thread; '
                f'got {len(finalized)}, {finalized.count(False)} off-thread')
    finally:
        dispose(panel)
    print('PASS: 20 dashboard open/close cycles, no retained after callbacks, 140 variables released on Tk thread')


def shutdown():
    panel, errors = setup()
    stopped = []
    try:
        panel.tray = SimpleNamespace(stop=lambda: stopped.append(True))
        view, window = open_window(panel)
        view.explain()
        panel.root.after(60000, lambda: None)
        panel.quit()
        assert stopped == [True]
        assert not panel.root.tk.call('after', 'info'), 'Application shutdown retained timers'
        assert not window._tclCommands, 'Dashboard Tcl commands survived destruction'
        assert panel.insights_window is None
        assert view.panel is None
        assert not errors, errors
        del view, window
        gc.collect()
    finally:
        dispose(panel)
    print('PASS: application shutdown with dashboard and explanation open clears timers and Tcl callbacks')


def main():
    scenarios = {'dashboard': dashboard, 'exports': exports, 'settings': settings,
                 'sorting': sorting, 'scopes': scopes, 'stale-anchor': stale_anchor,
                 'layout': layout, 'lifecycle': lifecycle, 'shutdown': shutdown}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=tuple(scenarios))
    parser.add_argument('--screenshot', help='Optional image of synthetic dashboard data only')
    args = parser.parse_args()
    if args.child:
        if args.child == 'dashboard':
            dashboard(args.screenshot)
        else:
            scenarios[args.child]()
        return
    failures = []
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    for name in scenarios:
        command = [sys.executable, str(Path(__file__).resolve()), '--child', name]
        if name == 'dashboard' and args.screenshot:
            command += ['--screenshot', str(Path(args.screenshot).resolve())]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding='utf8', errors='replace', env=env, timeout=40)
        except subprocess.TimeoutExpired:
            failures.append(name+' timed out')
            continue
        print(result.stdout, end='')
        if result.returncode or result.stderr:
            failures.append(f'{name}: exit {result.returncode}')
            print(result.stderr, file=sys.stderr, end='')
    if failures:
        raise SystemExit('FAIL: '+'; '.join(failures))
    print(f'PASS: all {len(scenarios)} isolated, offline dashboard scenarios')


if __name__ == '__main__':
    main()
