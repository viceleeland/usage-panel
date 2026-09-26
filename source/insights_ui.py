"""Monthly local usage dashboard; no network or conversation text access."""
import csv
import datetime as dt
import json
import math
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser
from usage_costs import event_cost

BG, INK, MUTED, GREEN, WHITE = '#F1F3EE', '#17362C', '#60736A', '#238C52', '#FFFFFF'
DARK, MINT = '#163B2E', '#A8ECC4'
FONT = 'Microsoft YaHei UI'
ROLES = {'main': '主 Agent', 'subagent': '子 Agent', 'unknown': '归属未知'}


def tokens(value):
    if value is None:
        return '—'
    return '<0.001m' if 0 < value < 1000 else f'{value/1_000_000:.3f}m'


def money(value):
    if value is None:
        return '—'
    return '<$0.01' if 0 < value < .01 else '$' + f'{value:,.2f}'


def cost_text(row):
    known, unknown = row.get('known_cost', 0), row.get('unpriced_tokens', 0)
    if unknown:
        return (money(known) + ' + ?') if known else '未定价'
    return money(known)


def valid_events(snapshot, now=None):
    now = dt.datetime.now().timestamp() if now is None else now
    for event in snapshot.get('events') or []:
        if not isinstance(event, dict):
            continue
        stamp = event.get('timestamp')
        if (not isinstance(stamp, (int,float)) or isinstance(stamp, bool)
                or not math.isfinite(stamp) or not 0 <= stamp <= now
                or any(not isinstance(event.get(k), int) or isinstance(event.get(k), bool)
                       or event[k] < 0 for k in ('input','output','total'))
                or event['total'] != event['input']+event['output']):
            continue
        yield event


def safe_cell(value):
    """Prevent spreadsheet formula evaluation of local task titles."""
    text = str(value if value is not None else '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


def export_rows(path, columns, rows):
    path = Path(path)
    if path.suffix.lower() == '.json':
        path.write_text(json.dumps([dict(zip(columns, row)) for row in rows],
                                  ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        with path.open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows([[safe_cell(value) for value in row] for row in rows])


class InsightsWindow:
    def __init__(self, panel):
        self.panel = panel
        self.window = tk.Toplevel(panel.root)
        self.window.title('Usage Panel · 月度费用与 Agent 用量')
        self.window.configure(bg=BG)
        width, height = min(1220, self.window.winfo_screenwidth()-60), min(870, self.window.winfo_screenheight()-100)
        self.window.geometry(f'{width}x{height}+30+30')
        self.window.minsize(900, 700)
        self.window.bind('<Escape>', lambda _: self.window.destroy())
        self.window.bind('<Destroy>', self.close, add='+')
        self.after_id, self.last_revision, self.report = None, None, {}
        self.query = tk.StringVar(master=self.window)
        self.model = tk.StringVar(master=self.window, value='全部模型')
        self.role = tk.StringVar(master=self.window, value='全部角色')
        self.scope = tk.StringVar(master=self.window, value='本周期')
        self.anchor = tk.StringVar(master=self.window, value=panel.settings.get('billing_anchor', ''))
        self.budget = tk.StringVar(master=self.window, value=str(panel.settings.get('monthly_budget') or ''))
        self.error = tk.StringVar(master=self.window)
        self.export_data, self.sorts, self.sort_values = {}, {}, {}
        self._build()
        self.refresh(force=True)

    def label(self, parent, text='', size=10, color=INK, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget('background'), fg=color,
                        font=(FONT, size, 'bold' if bold else 'normal'), **kw)

    def button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=WHITE, fg=INK,
                         activebackground='#DCEBDD', relief='flat', bd=0, padx=12,
                         pady=6, cursor='hand2', font=(FONT, 9))

    def _build(self):
        outer = tk.Frame(self.window, bg=BG, padx=22, pady=16)
        outer.pack(fill='both', expand=True)
        head = tk.Frame(outer, bg=DARK, padx=20, pady=14)
        head.pack(fill='x', pady=(0, 12))
        identity = tk.Frame(head, bg=DARK)
        identity.pack(side='left')
        self.label(identity, 'USAGE PANEL  /  ANALYTICS', 9, MINT).pack(anchor='w')
        self.label(identity, '用量与费用', 23, '#FFFFFF', bold=True).pack(anchor='w', pady=(4,0))
        self.button(head, '导出当前表格', self.export).pack(side='right')
        self.button(head, '刷新', lambda: self.panel.refresh_tokens()).pack(side='right', padx=8)
        self.period_label = self.label(outer, '读取本机记录…', 10, MUTED)
        self.period_label.pack(anchor='w', pady=(4, 12))
        metrics = tk.Frame(outer, bg=BG)
        metrics.pack(fill='x')
        self.metrics = {}
        for col, (key, title) in enumerate([
                ('cost', '本周期 · Token 折算'), ('forecast', '周期结束 · 预计总费用'),
                ('total', '主 / 子 Agent · Token'), ('pace', '最近 60 分钟 · 消耗速度')]):
            metrics.columnconfigure(col, weight=1, uniform='metric')
            card = tk.Frame(metrics, bg=DARK if col == 0 else WHITE, padx=16, pady=14)
            card.grid(row=0, column=col, sticky='nsew', padx=(0 if col == 0 else 8, 0))
            self.label(card, title, 10, '#CCE0D2' if col == 0 else MUTED).pack(anchor='w')
            value = self.label(card, '—', 24, MINT if col == 0 else INK, True)
            value.configure(font=('Bahnschrift', 25))
            value.pack(anchor='w', pady=(5, 3))
            detail = self.label(card, '', 9, '#CCE0D2' if col == 0 else MUTED, justify='left', wraplength=240)
            detail.pack(anchor='w')
            self.metrics[key] = (value, detail)
        self.status = self.label(outer, '', 9, MUTED, anchor='w', justify='left', wraplength=1120)
        self.status.pack(fill='x', pady=(10, 3))
        self.chart = tk.Canvas(outer, bg=BG, height=98, highlightthickness=0)
        self.chart.pack(fill='x', pady=(0, 10))
        self.chart.bind('<Configure>', lambda _: self.draw_chart())
        filters = tk.Frame(outer, bg=BG)
        filters.pack(fill='x', pady=(0, 9))
        self.label(filters, '搜索任务 / Agent', 9, MUTED).pack(side='left')
        search = ttk.Entry(filters, textvariable=self.query, width=22)
        search.pack(side='left', padx=(8, 12))
        for variable, values, width in [
                (self.scope, ['本周期', '今日', '最近 7 天'], 12),
                (self.role, ['全部角色', '主 Agent', '子 Agent', '归属未知'], 13),
                (self.model, ['全部模型'], 22)]:
            widget = ttk.Combobox(filters, textvariable=variable, values=values,
                                  state='readonly', width=width)
            widget.pack(side='left', padx=(0, 8))
            if variable is self.model:
                self.model_select = widget
            widget.bind('<<ComboboxSelected>>', lambda _: self.render_tables())
        search.bind('<KeyRelease>', lambda _: self.render_tables())
        self.label(filters, '筛选仅影响下方表格', 8, MUTED).pack(side='right')
        style = ttk.Style(self.window)
        style.theme_use('clam')
        style.configure('Usage.TNotebook', background=BG, borderwidth=0)
        style.configure('Usage.TNotebook.Tab', font=(FONT,10), padding=(18,8), background='#E1E7DE', foreground=MUTED)
        style.map('Usage.TNotebook.Tab', background=[('selected',WHITE)], foreground=[('selected',INK)])
        style.configure('Usage.Treeview', font=(FONT, 9), rowheight=29,
                        background=WHITE, fieldbackground=WHITE, foreground=INK, borderwidth=0)
        style.map('Usage.Treeview', background=[('selected','#CDE9D7')], foreground=[('selected',INK)])
        style.configure('Usage.Treeview.Heading', font=(FONT, 9, 'bold'), background='#EAF0E7', foreground=INK, padding=(6,8), relief='flat')
        self.tabs = ttk.Notebook(outer, style='Usage.TNotebook')
        self.tabs.pack(fill='both', expand=True)
        self.tasks = self.table('任务 / Agent', [
            ('role', '角色', 76), ('model', '模型 / 主子合计', 200), ('effort', '推理档位', 70),
            ('input', '输入', 78), ('cached', '缓存输入', 78), ('output', '输出', 78),
            ('total', '合计 Token', 91), ('cost', '折算 USD', 106)], tree=True)
        self.models = self.table('模型排行', [
            ('model', '模型', 210), ('effort', '推理档位', 85),
            ('input', '输入', 115), ('cached', '缓存输入', 115),
            ('output', '输出', 115), ('total', '合计 Token', 125),
            ('share', 'Token 占比', 100), ('cost', '折算 USD', 120)])
        self.days = self.table('每日明细', [
            ('date', '日期', 180), ('total', '合计 Token', 200),
            ('cost', '折算 USD', 200), ('unknown', '未定价 Token', 200)])
        self.tables = [self.tasks, self.models, self.days]
        self.table_note = self.label(outer, '', 9, MUTED)
        self.table_note.pack(anchor='w', pady=(7, 0))
        settings = tk.Frame(outer, bg=BG)
        settings.pack(fill='x', pady=(12, 0))
        self.label(settings, '续费锚点', 9, MUTED).pack(side='left')
        ttk.Entry(settings, textvariable=self.anchor, width=13).pack(side='left', padx=(8, 6))
        self.label(settings, 'YYYY-MM-DD', 8, MUTED).pack(side='left')
        self.label(settings, '月度折算预算 $', 9, MUTED).pack(side='left', padx=(20, 6))
        ttk.Entry(settings, textvariable=self.budget, width=10).pack(side='left')
        self.label(settings, '选填', 8, MUTED).pack(side='left', padx=5)
        self.button(settings, '保存', self.save).pack(side='left', padx=10)
        self.label(settings, textvariable=self.error, size=9, color='#AE6830').pack(side='left')
        self.button(settings, '定价与统计说明', self.explain).pack(side='right')
        # Reserve the settings/footer before giving the table its stretchable
        # space, so both remain reachable on shorter displays.
        self.tabs.pack_forget()
        self.table_note.pack_forget()
        settings.pack_forget()
        settings.pack(side='bottom', fill='x', pady=(12,0))
        self.table_note.pack(side='bottom', anchor='w', pady=(7,0))
        self.tabs.pack(fill='both', expand=True)
        self.settings_frame = settings

    def table(self, title, columns, tree=False):
        frame = tk.Frame(self.tabs, bg=WHITE)
        self.tabs.add(frame, text='  '+title+'  ')
        table = ttk.Treeview(frame, columns=[c[0] for c in columns],
                             show='tree headings' if tree else 'headings',
                             style='Usage.Treeview', selectmode='browse')
        table.tag_configure('task', background='#EDF3E9', foreground=INK, font=(FONT,9,'bold'))
        table.tag_configure('alternate', background='#F6F8F3')
        if tree:
            table.heading('#0', text='任务 / Agent · 点击展开')
            table.column('#0', width=245, minwidth=160)
        for key, label, width in columns:
            table.heading(key, text=label, command=lambda t=table, k=key: self.sort(t, k))
            table.column(key, width=width, minwidth=60, anchor='w' if key in ('model','role','effort','date') else 'e')
        yscroll = ttk.Scrollbar(frame, orient='vertical', command=table.yview)
        xscroll = ttk.Scrollbar(frame, orient='horizontal', command=table.xview)
        table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        table.grid(row=0, column=0, sticky='nsew')
        yscroll.grid(row=0, column=1, sticky='ns')
        xscroll.grid(row=1, column=0, sticky='ew')
        return table

    def selected_events(self):
        snapshot = getattr(self.panel, 'analytics_snapshot', {}) or {}
        if self.panel.settings.get('billing_anchor') and not self.report.get('start'):
            return []
        events = valid_events(snapshot)
        now = dt.datetime.now().astimezone()
        start = self.report.get('start')
        lower = dt.datetime.fromisoformat(start).replace(tzinfo=now.tzinfo).timestamp() if start else 0
        if self.scope.get() == '今日':
            lower = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif self.scope.get() == '最近 7 天':
            lower = (now-dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        query, model, role = self.query.get().casefold().strip(), self.model.get(), self.role.get()
        sessions = snapshot.get('sessions', {})
        selected = []
        for event in events:
            stamp = event.get('timestamp')
            if (not isinstance(stamp, (int,float)) or not math.isfinite(stamp)
                    or not lower <= stamp <= now.timestamp()
                    or any(not isinstance(event.get(k), int) or isinstance(event.get(k), bool) or event[k] < 0 for k in ('input','output','total'))
                    or event['total'] != event['input']+event['output']):
                continue
            if model != '全部模型' and (event.get('model') or '未知模型') != model:
                continue
            if role != '全部角色' and ROLES.get(event.get('role'), '归属未知') != role:
                continue
            root_id = event.get('root_id') or event['session_id']
            haystack = ' '.join(str(v or '') for v in [
                event['session_id'], root_id, event.get('agent_path'),
                sessions.get(event['session_id'], {}).get('title'),
                sessions.get(root_id, {}).get('title'), event.get('model')])
            if query and query not in haystack.casefold():
                continue
            selected.append(event)
        return selected

    @staticmethod
    def aggregate(events):
        row = {key: sum(e.get(key, 0) for e in events) for key in ('input', 'output', 'total')}
        row['cached'] = None if any(e.get('cached') is None for e in events) else sum(e['cached'] for e in events)
        row.update(known_cost=0., unpriced_tokens=0)
        for event in events:
            cost = event_cost(event)
            if cost is None:
                row['unpriced_tokens'] += event.get('total', 0)
            else:
                row['known_cost'] += cost
        return row

    def render_tables(self):
        if self.panel is None:
            return
        opened = {self.tasks.item(i, 'text') for i in self.tasks.get_children() if self.tasks.item(i, 'open')}
        positions = {table: table.yview() for table in self.tables}
        for table in self.tables:
            table.delete(*table.get_children())
        self.sort_values = {}
        events = self.selected_events()
        sessions = (getattr(self.panel, 'analytics_snapshot', {}) or {}).get('sessions', {})
        tasks, models, days = {}, {}, {}
        for event in events:
            root = event.get('root_id') or event['session_id']
            tasks.setdefault(root, []).append(event)
            models.setdefault((event.get('model') or '未知模型', event.get('effort') or '—'), []).append(event)
            day = dt.datetime.fromtimestamp(event['timestamp']).date().isoformat()
            days.setdefault(day, []).append(event)
        task_export, model_export, day_export = [], [], []
        for root, group in sorted(tasks.items(), key=lambda x: -sum(e['total'] for e in x[1])):
            row = self.aggregate(group)
            title = sessions.get(root, {}).get('title') or '任务 '+root[:8]
            main = sum(e['total'] for e in group if e.get('role') == 'main')
            sub = sum(e['total'] for e in group if e.get('role') == 'subagent')
            parent = self.tasks.insert('', 'end', text=title, open=title in opened,
                tags=('task',),
                values=('任务合计', '主 '+tokens(main)+' / 子 '+tokens(sub), '—', tokens(row['input']),
                        tokens(row['cached']), tokens(row['output']), tokens(row['total']), cost_text(row)))
            self.sort_values[(self.tasks, parent)] = {**row, 'cost': row['known_cost']}
            agents = {}
            for event in group:
                agents.setdefault((event['session_id'], event.get('model') or '未知模型',
                                   event.get('effort') or '—'), []).append(event)
            for (owner, model, effort), agent_events in sorted(agents.items(),
                    key=lambda x: (x[1][0].get('role') != 'main', -sum(e['total'] for e in x[1]))):
                item, first = self.aggregate(agent_events), agent_events[0]
                path = first.get('agent_path') or ('主 Agent' if first.get('role') == 'main' else owner[:8])
                role = ROLES.get(first.get('role'), '归属未知')
                node = self.tasks.insert(parent, 'end', text=path, tags=('alternate',) if len(task_export)%2 else (), values=(role, model, effort,
                    tokens(item['input']), tokens(item['cached']), tokens(item['output']),
                    tokens(item['total']), cost_text(item)))
                self.sort_values[(self.tasks, node)] = {**item, 'cost': item['known_cost']}
                task_export.append([root, title, owner, path, role, model, effort,
                    item['input'], item['cached'], item['output'], item['total'],
                    round(item['known_cost'], 6), item['unpriced_tokens']])
        total = sum(e['total'] for e in events)
        for (model, effort), group in sorted(models.items(), key=lambda x: -sum(e['total'] for e in x[1])):
            row = self.aggregate(group)
            share = row['total']/total*100 if total else 0
            node = self.models.insert('', 'end', tags=('alternate',) if len(model_export)%2 else (), values=(model, effort, tokens(row['input']),
                tokens(row['cached']), tokens(row['output']), tokens(row['total']), f'{share:.1f}%', cost_text(row)))
            self.sort_values[(self.models, node)] = {**row, 'cost': row['known_cost'], 'share': share}
            model_export.append([model, effort, row['input'], row['cached'], row['output'],
                row['total'], round(share, 3), round(row['known_cost'], 6), row['unpriced_tokens']])
        for day, group in sorted(days.items(), reverse=True):
            row = self.aggregate(group)
            node = self.days.insert('', 'end', tags=('alternate',) if len(day_export)%2 else (), values=(day, tokens(row['total']), cost_text(row), tokens(row['unpriced_tokens'])))
            self.sort_values[(self.days, node)] = {**row, 'cost': row['known_cost'], 'unknown': row['unpriced_tokens']}
            day_export.append([day, row['total'], round(row['known_cost'], 6), row['unpriced_tokens']])
        self.export_data = {
            self.tasks: (['task_id','task_title','agent_id','agent_path','role','model','effort',
                'input_tokens','cached_input_tokens','output_tokens','total_tokens','known_api_equivalent_usd','unpriced_tokens'], task_export),
            self.models: (['model','effort','input_tokens','cached_input_tokens','output_tokens',
                'total_tokens','token_share_percent','known_api_equivalent_usd','unpriced_tokens'], model_export),
            self.days: (['date','total_tokens','known_api_equivalent_usd','unpriced_tokens'], day_export)}
        self.table_note.configure(text=f'{len(tasks)} 个任务 · {len(task_export)} 组 Agent / 模型 · '
            f'筛选合计 {tokens(total)} · 任务行已含子 Agent，请勿重复相加。')
        for table, position in positions.items():
            if position:
                table.yview_moveto(position[0])
            if table in self.sorts:
                column, reverse = self.sorts[table]
                self.sort(table, column, reverse)

    def sort(self, table, column, reverse=None):
        if reverse is None:
            old = self.sorts.get(table, (None, False))
            reverse = not old[1] if old[0] == column else True
        self.sorts[table] = (column, reverse)
        def value(item):
            raw = self.sort_values.get((table, item), {})
            if column in raw:
                return (1, '') if raw[column] is None else (0, raw[column])
            text = str(table.set(item, column))
            try:
                return (0, float(text.replace('$','').replace(',','').replace('m','')
                                 .replace('%','').replace('<','').replace(' + ?', '')))
            except ValueError:
                return (1, text.casefold())
        for parent in [''] + (list(table.get_children()) if table is self.tasks else []):
            for index, item in enumerate(sorted(table.get_children(parent), key=value, reverse=reverse)):
                table.move(item, parent, index)

    def refresh(self, force=False):
        self.after_id = None
        if self.panel is None:
            return
        report = getattr(self.panel, 'analytics_report', {}) or {}
        snapshot = getattr(self.panel, 'analytics_snapshot', {}) or {}
        revision = (snapshot.get('updated'), self.panel.settings.get('billing_anchor'), self.panel.settings.get('monthly_budget'))
        if force or revision != self.last_revision:
            self.report, self.last_revision = report, revision
            start, end = report.get('start'), report.get('end')
            pending = bool(self.panel.settings.get('billing_anchor')) and not start
            self.period_label.configure(text=(f'本机 Codex · {start} → {end}（结束日不计入）'
                if start else '正在按新周期重新统计…' if pending else '请设置续费锚点日期，以统计一个月的额度周期。'))
            values = list(valid_events(snapshot)) if not pending else []
            total = report.get('total_tokens', 0)
            main = sum(t['main_tokens'] for t in report.get('tasks', []))
            sub = sum(t['subagent_tokens'] for t in report.get('tasks', []))
            known, elapsed = report.get('known_cost', 0), report.get('elapsed_days', 0) or 0
            daily = known/elapsed if elapsed else None
            forecast = report.get('projected_cost')
            partial_forecast = forecast is None and report.get('projected_known_cost') is not None
            display_forecast = report.get('projected_known_cost') if partial_forecast else forecast
            self.metrics['cost'][0].configure(text=cost_text(report) if start and snapshot.get('available') else '—')
            self.metrics['cost'][1].configure(text='Standard API 基础折算 · 非实付')
            self.metrics['forecast'][0].configure(text=money(display_forecast)+(' + ?' if partial_forecast else ''))
            remaining = max(0, (report.get('total_days', 0) or 0)-elapsed)
            self.metrics['forecast'][1].configure(text=('仅已知部分 · ' if partial_forecast else '')+
                f'日均 {money(daily)}\n剩余 {remaining:.1f} 天')
            self.metrics['total'][0].configure(text=tokens(total) if snapshot.get('available') and start else '—')
            self.metrics['total'][1].configure(text=f'主 {tokens(main)} / 子 {tokens(sub)}')
            recent = [e for e in values if e['timestamp'] >= dt.datetime.now().timestamp()-3600]
            rate = self.aggregate(recent)
            self.metrics['pace'][0].configure(text=cost_text(rate)+'/h' if snapshot.get('available') else '—')
            self.metrics['pace'][1].configure(text=f'{tokens(rate["total"]/60)}/min · 最近一小时均值')
            unknown = report.get('unpriced_tokens', 0)
            message = f'费率覆盖 {100*(total-unknown)/total:.1f}%' if total else '暂无可计量调用'
            if unknown:
                message += f' · {tokens(unknown)} 未定价，金额含已知部分'
            if report.get('partial'):
                message += ' · 日志读取不完整'
            if display_forecast is None and start:
                message += ' · 预测暂不可用（记录不完整或不足一天）'
            budget = self.panel.settings.get('monthly_budget')
            if budget:
                message += f' · 折算预算 {money(budget)}，已记录 {known/budget*100:.1f}%'
                if forecast is not None and forecast > budget:
                    message += '，预计超出 '+money(forecast-budget)
            if snapshot.get('updated'):
                message += ' · '+dt.datetime.fromtimestamp(snapshot['updated']).strftime('%H:%M:%S')+' 更新'
            self.status.configure(text=message)
            options = ['全部模型'] + sorted({e.get('model') or '未知模型' for e in values})
            self.model_select.configure(values=options)
            if self.model.get() not in options:
                self.model.set('全部模型')
            self.render_tables()
            self.draw_chart()
        self.after_id = self.window.after(1500, self.refresh)

    def draw_chart(self):
        self.chart.delete('all')
        days, width = self.report.get('daily') or [], max(self.chart.winfo_width(), 600)
        self.chart.create_text(0, 9, anchor='w', fill=MUTED, font=(FONT, 8),
                              text='本周期每日折算 · USD · 未定价用量另列于明细')
        if not days:
            self.chart.create_text(width/2, 54, fill=MUTED, text='等待本周期用量')
            return
        maximum = max((d.get('known_cost', 0) for d in days), default=0) or 1
        slot = (width-20)/len(days)
        for i, day in enumerate(days):
            cost, left = day.get('known_cost', 0), 10+i*slot
            height = 48*cost/maximum
            self.chart.create_rectangle(left, 76-height, left+max(3, slot-7), 76,
                                        fill=GREEN if cost else '#D5DED4', outline='')
            if cost and (len(days) <= 16 or i % 2 == 0):
                self.chart.create_text(left+slot/2-3, 70-height, text=f'{cost:.1f}', fill=INK,
                                       font=('Consolas', 8), anchor='s')
            if len(days) <= 16 or i % 3 == 0 or i == len(days)-1:
                self.chart.create_text(left+slot/2-3, 89, text=day.get('date','')[5:],
                                       fill=MUTED, font=('Consolas', 8))

    def save(self):
        previous = dict(self.panel.settings)
        try:
            anchor = dt.date.fromisoformat(self.anchor.get().strip()).isoformat()
            value = self.budget.get().strip()
            budget = float(value) if value else None
            if budget is not None and not (0 < budget < 1e9):
                raise ValueError()
            self.panel.settings.update(billing_anchor=anchor, monthly_budget=budget)
            self.panel.save_settings()
        except (ValueError, OSError):
            self.panel.settings = previous
            self.error.set('请填有效日期和正数预算；检查保存权限。')
            return
        self.error.set('已保存，正在重新统计')
        self.panel.analytics_report = {}
        self.panel.update_cost_labels()
        self.last_revision = None
        self.report = {}
        self.render_tables()
        self.panel.refresh_tokens()

    def export(self):
        table = self.tables[self.tabs.index(self.tabs.select())]
        columns, rows = self.export_data.get(table, ([], []))
        path = filedialog.asksaveasfilename(parent=self.window, title='导出当前筛选明细（含任务名称）',
            defaultextension='.csv', filetypes=[('CSV 表格','*.csv'), ('JSON 数据','*.json')],
            initialfile='usage-'+self.scope.get()+'-'+dt.date.today().isoformat()+'.csv')
        if not path:
            return
        try:
            export_rows(path, columns, rows)
            self.error.set(f'已导出 {len(rows)} 行')
        except OSError:
            self.error.set('导出失败，请检查文件权限。')

    def explain(self):
        dialog = tk.Toplevel(self.window)
        dialog.title('统计口径与价格')
        dialog.configure(bg=BG, padx=22, pady=18)
        text = (
            '金额按已核验的 Standard API 单价折算，单位 USD，不是订阅实付账单。\n'
            '价格核验日期：2026-09-26；历史用量按该费率重算。\n\n'
            '缓存输入属于输入，推理 Token 属于输出，不重复加总。\n'
            '模型与推理档位来自当次记录；ultra 不另乘计费系数。\n'
            '无法确认的模型、单次输入或价格，保留 Token 并显示未定价。\n'
            '未计入无法确认的 Fast 溢价、缓存写入差价及工具/图像费用。\n\n'
            '月度周期取配置日期的日号，不从 5 小时或 7 天额度推断。\n'
            '预计费用 = 本周期已记录折算 ÷ 已过时间 × 整个周期时长。\n'
            '不足一天或读取不完整时不预测；未定价时仅预测已知部分并加问号。\n'
            '预测只是使用节奏外推，不代表额度价值，也不保证后续消费。\n\n'
            '仅覆盖本机保留日志，可能不含其他设备、云任务和已删除日志。\n'
            '任务标题仅来自本机索引；无标题时显示任务 ID。\n'
            '任务合计含子 Agent；展开行按 Agent 和实际模型/档位拆分。\n'
            'CSV/JSON 仅导出当前筛选表格，不含聊天正文。')
        self.label(dialog, text, 10, justify='left').pack(anchor='w')
        self.button(dialog, '查看官方 API 定价',
                    lambda: webbrowser.open('https://developers.openai.com/api/docs/pricing')).pack(anchor='w', pady=(12, 0))
        self.button(dialog, '关闭', dialog.destroy).pack(anchor='e')

    def close(self, event):
        if event.widget is not self.window:
            return
        if self.after_id is not None:
            self.window.after_cancel(self.after_id)
            self.after_id = None
        if self.panel is not None:
            self.panel.insights_window = None
        self.panel = None
