import argparse
import ctypes
import datetime as dt
import json
import pathlib
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser

import psutil
import pystray
from PIL import Image, ImageDraw, ImageGrab, ImageTk
from providers import collect, read_json
from token_usage import DailyTokens
from alerts import check_alerts
from usage_view import codex_daily_rows, retain_daily_usage

BASE = pathlib.Path(sys.executable).parent if getattr(sys, 'frozen', False) else pathlib.Path(__file__).resolve().parent.parent
DATA = BASE/'data'
BG = '#EDF0EA'
FG = '#26332C'
MUTED = '#7A877E'
LINE = '#D4DCD2'
GREEN = '#28B753'
FONT = 'Microsoft YaHei UI'
MONO = 'Consolas'
LEVELS = ['ultra', 'max', 'xhigh', 'high', 'medium', 'low']
DEFAULT_SCORES = {name: ['']*6 for name in ['Astra','Sol','Terra','Luna']}

def token_text(value):
    return '<0.001m' if 0 < value < 1000 else f'{value/1_000_000:.3f}m'

def cache_hit_text(data):
    inputs, cached = data.get('input'), data.get('cached')
    if (not data.get('available') or data.get('partial')
            or not isinstance(inputs, int) or not isinstance(cached, int)
            or inputs <= 0 or not 0 <= cached <= inputs):
        return '—'
    return f'{cached / inputs * 100:.1f}%'

def reset_text(timestamp):
    if not isinstance(timestamp, (int, float)):
        return '重置时间未提供'
    seconds = int(timestamp-time.time())
    if seconds <= 0:
        return '等待刷新额度'
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem//60
    remaining = f'{days}天{hours}时' if days else f'{hours}时{minutes}分'
    return f'{remaining}后 · {dt.datetime.fromtimestamp(timestamp):%m/%d %H:%M}'

def tray_image():
    im = Image.new('RGBA', (64,64))
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((3,3,61,61), radius=16, fill='#EAF3E8', outline='#A3B69D', width=2)
    for x, h in [(16,15),(28,27),(40,38)]:
        draw.rounded_rectangle((x,49-h,x+8,49), radius=3, fill=GREEN)
    return im

class UsagePanel:
    def __init__(self, args):
        self.args = args
        self.root = tk.Tk()
        self.root.tk.call('tk','scaling',1.30)
        self.root.title('Usage Panel · AI 用量')
        self.window_icon=ImageTk.PhotoImage(tray_image())
        self.root.iconphoto(True,self.window_icon)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol('WM_DELETE_WINDOW', self.hide)
        self.root.bind('<Escape>', lambda e: self.hide())
        self.root.bind('<Control-r>', lambda e: self.refresh())
        self.messages = queue.Queue()
        self.busy = False
        self.closed = False
        self.token_reader = DailyTokens()
        self.token_busy = False
        self.tokens = {}
        self.token_labels = {}
        self.result = read_json(DATA/'usage-cache.json')
        for data in self.result.values():
            if isinstance(data,dict):
                data['ok']=False
                data['note']='上次缓存 · 正在刷新…'
                if isinstance(data.get('daily_usage'), dict):
                    data['daily_usage']['ok'] = False
        self.settings = read_json(DATA/'settings.json')
        self.scores = self.settings.get('scores', DEFAULT_SCORES.copy())
        self.radar_mode = tk.StringVar(value=self.settings.get('radar_mode','综合智能'))
        self.topmost = tk.BooleanVar(value=self.settings.get('topmost', False))
        self.alerts_enabled = tk.BooleanVar(value=self.settings.get('alerts_enabled', True))
        self.alert_state = read_json(DATA/'alert-state.json')
        self.alert_status = '额度每 5 分钟检查一次'
        self.trend_window = None
        self.root.attributes('-topmost', self.topmost.get())
        self.body = tk.Frame(self.root, bg=BG, padx=22, pady=14)
        self.body.pack(fill='both', expand=True)
        self.tray = pystray.Icon('UsagePanel', tray_image(), 'AI 用量 · 正在读取', pystray.Menu(
            pystray.MenuItem('显示面板', lambda *_: self.messages.put(('show', None)), default=True),
            pystray.MenuItem('刷新用量', lambda *_: self.messages.put(('refresh', None))),
            pystray.MenuItem('退出', lambda *_: self.messages.put(('quit', None)))))
        self.tray.run_detached()
        self.draw()
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.geometry(f'+{max(0, self.root.winfo_screenwidth()-width-32)}+{max(0,self.root.winfo_screenheight()-height-90)}')
        self.root.after(100, self.poll)
        self.root.after(100, self.refresh)
        self.root.after(150, self.refresh_tokens)
        self.root.after(300000, self.auto_refresh)
        self.root.after(30000, self.tick)
        if args.smoke:
            self.root.after(55000, lambda: self.finish_smoke(False))
        if args.hidden:
            self.root.withdraw()

    def label(self, parent, text, size=10, color=FG, bold=False, mono=False, **kwargs):
        return tk.Label(parent, text=text, bg=BG, fg=color,
            font=(MONO if mono else FONT, size, 'bold' if bold else 'normal'), **kwargs)

    def line(self):
        tk.Frame(self.body, bg=LINE, height=1).pack(fill='x', pady=5)

    def button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=BG, fg=FG, activebackground='#DDE8D9',
            relief='flat', bd=0, cursor='hand2', padx=7, pady=4, font=(FONT,9))

    def draw(self):
        self.token_labels = {}
        for child in self.body.winfo_children():
            child.destroy()
        header = tk.Frame(self.body, bg=BG)
        header.pack(fill='x')
        self.label(header, 'USAGE PANEL', 13, bold=True, mono=True).pack(side='left')
        self.label(header, '●  自动更新', 9, GREEN).pack(side='right')
        self.label(self.body, 'AI 用量，一眼看清', 9, MUTED).pack(anchor='w', pady=(3, 2))
        self.line()
        tooltip = []
        for key, fallback in [('codex','Codex')]:
            data = self.result.get(key, {})
            cards = data.get('cards') or [{'name':fallback, 'plan':'', 'windows':[]}]
            for card in cards:
                head = tk.Frame(self.body, bg=BG)
                head.pack(fill='x', pady=(0, 5))
                self.label(head, card['name'], 11, bold=True, mono=True).pack(side='left')
                if card.get('plan'):
                    self.label(head, card['plan'].upper(), 8, MUTED, mono=True).pack(side='left', padx=8)
                self.label(head, '剩余额度', 8, MUTED).pack(side='right')
                if card['windows']:
                    for window in card['windows']:
                        row = tk.Frame(self.body, bg=BG)
                        row.pack(fill='x', pady=3)
                        self.label(row, window['label'], 9, MUTED, width=8, anchor='w').pack(side='left')
                        remaining = 100-window['used']
                        expired = isinstance(window.get('reset'), (int,float)) and window['reset'] <= time.time()
                        color = MUTED if expired or not data.get('ok') else GREEN if remaining>25 else '#C89427' if remaining>10 else '#D56658'
                        bar = tk.Canvas(row, width=142, height=10, bg=BG, highlightthickness=0)
                        bar.pack(side='left', padx=(2,10))
                        bar.create_rectangle(0,0,142,10, fill='#D8DFD5', outline='')
                        if not expired:
                            bar.create_rectangle(0,0,142*remaining/100,10, fill=color, outline='')
                        value = '—' if expired else f'{remaining:g}%'
                        self.label(row, value, 10, color, mono=True, width=5, anchor='e').pack(side='left')
                        self.label(row, reset_text(window.get('reset')), 8, MUTED, anchor='w').pack(side='left', padx=(10,0))
                        tooltip.append(f'{card["name"]} {window["label"]} {value}')
                else:
                    self.label(self.body, '—  暂无额度数据', 10, MUTED).pack(anchor='w')
                daily = data.get('daily_usage') or {}
                latest = max(daily.get('buckets') or [], key=lambda b: b['date'], default=None)
                note = data.get('note', '正在读取本机登录状态…')
                if latest and data.get('ok'):
                    note = f'官方日用量 {latest["date"][5:]}  {token_text(latest["total"])}'
                    if not daily.get('ok'):
                        note += ' · 旧数据'
                self.label(self.body, note, 8, MUTED,
                    wraplength=480, justify='left').pack(anchor='w', pady=(4,5))
                if not data.get('ok') and data.get('updated'):
                    self.label(self.body, f'上次成功 {dt.datetime.fromtimestamp(data["updated"]):%m/%d %H:%M} · 当前为旧数据', 8, '#B08129').pack(anchor='w')
            credits = data.get('reset_credits') or {}
            count = credits.get('count')
            expiry = credits.get('expires') or []
            text = f'重置卡  {count} 张可用' if count is not None else '重置卡  — 未提供'
            if expiry:
                text += f'  ·  最近到期 {dt.datetime.fromtimestamp(expiry[0]):%m/%d %H:%M}'
            if not data.get('ok'):
                text += '（旧数据）' if count is not None else ''
            self.label(self.body, text, 9, MUTED).pack(anchor='w', pady=(0, 3))
            self.token_row('codex')
            self.line()
        deep=self.result.get('deepseek',{})
        head=tk.Frame(self.body,bg=BG)
        head.pack(fill='x')
        self.label(head,'DeepSeek API',11,bold=True,mono=True).pack(side='left')
        self.label(head,'via Claude Code',9,MUTED,mono=True).pack(side='right')
        for balance in deep.get('balances',[]):
            currency=balance.get('currency','')
            symbol={'CNY':'¥','USD':'$'}.get(currency,currency+' ')
            self.label(self.body,f'余额  {symbol}{balance["total_balance"]}',18,GREEN if deep.get('ok') else MUTED,bold=True,mono=True).pack(anchor='w',pady=(7,4))
        if not deep.get('balances'):
            self.label(self.body,'—  正在读取余额' if self.busy else '—  暂无余额数据',10,MUTED).pack(anchor='w',pady=6)
        if deep.get('model'):
            self.label(self.body,deep['model'],9,MUTED,mono=True).pack(anchor='w')
        self.label(self.body,deep.get('note','读取本机 DeepSeek API 配置…'),8,MUTED,wraplength=480,justify='left').pack(anchor='w',pady=(3,0))
        if not deep.get('ok') and deep.get('updated'):
            self.label(self.body,f'旧数据 · {dt.datetime.fromtimestamp(deep["updated"]):%m/%d %H:%M}',8,'#B08129').pack(anchor='w')
        self.token_row('deepseek')
        self.line()
        summary_header = tk.Frame(self.body, bg=BG)
        summary_header.pack(fill='x')
        self.label(summary_header, 'SUMMARY', 10, bold=True, mono=True).pack(side='left')
        self.button(summary_header, '7 天趋势 ↗', self.show_trends).pack(side='right')
        summary = tk.Frame(self.body,bg=BG)
        summary.pack(fill='x', pady=(6,0))
        mem = psutil.virtual_memory()
        self.label(summary, '内存',9,MUTED).pack(side='left')
        self.label(summary, f'{mem.used/2**30:.1f} / {mem.total/2**30:.1f} GB  ({mem.percent:g}%)',10,
            '#BC882B' if mem.percent>70 else FG,mono=True).pack(side='left',padx=14)
        names = []
        for proc in psutil.process_iter(['name']):
            name = (proc.info.get('name') or '').lower()
            if name in ('codex.exe', 'claude.exe'):
                names.append(name)
        self.label(self.body, f'进程  Codex {names.count("codex.exe")}  ·  Claude {names.count("claude.exe")}（不等于活动任务数）',8,MUTED).pack(anchor='w',pady=(5,0))
        self.line()
        iqhead=tk.Frame(self.body,bg=BG)
        iqhead.pack(fill='x')
        self.label(iqhead,'IQ / 模型评分',10,bold=True,mono=True).pack(side='left')
        mode=tk.OptionMenu(iqhead,self.radar_mode,'综合智能','软件工程','视觉空间',command=lambda _:self.change_mode())
        mode.configure(bg=BG,fg=MUTED,highlightthickness=0,relief='flat',font=(FONT,9))
        mode.pack(side='right')
        radar=self.result.get('radar',{})
        self.scores=radar.get('tables',{}).get(self.radar_mode.get(),{name:['']*6 for name in DEFAULT_SCORES})
        table=tk.Frame(self.body,bg=BG)
        table.pack(fill='x',pady=(4,0))
        for col, text in enumerate(['模型']+LEVELS):
            self.label(table,text,9,MUTED,mono=True,width=7,anchor='w' if col==0 else 'center').grid(row=0,column=col,pady=2)
        for row,(name, values) in enumerate(self.scores.items(),1):
            self.label(table,name,10,FG,bold=name=='Astra',mono=True,anchor='w').grid(row=row,column=0,sticky='w',pady=2)
            for col in range(6):
                value = values[col] if col<len(values) else ''
                self.label(table,str(value) if value!='' else '—',10,FG if value!='' else MUTED,mono=True).grid(row=row,column=col+1)
        self.label(self.body,radar.get('note','正在读取 Codex Radar…'),8,MUTED).pack(anchor='w',pady=(5,0))
        self.label(self.body,'社区基准分，非人的智商；— 表示该档位暂无数据',8,MUTED).pack(anchor='w')
        stamp=radar.get('software_updated') if self.radar_mode.get()!='视觉空间' else radar.get('visual_updated')
        if stamp:
            try: stamp=dt.datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone().strftime('%m/%d %H:%M')
            except ValueError: stamp='未提供'
            if self.radar_mode.get()=='综合智能' and radar.get('visual_updated'):
                try: visual_stamp=dt.datetime.fromisoformat(radar['visual_updated'].replace('Z','+00:00')).astimezone().strftime('%m/%d %H:%M')
                except ValueError: visual_stamp='未提供'
                stamp=f'软工 {stamp} · 视觉 {visual_stamp}'
            self.label(self.body,('旧数据 · ' if not radar.get('ok') else '')+'源更新 '+stamp,8,MUTED).pack(anchor='w')
        self.line()
        footer=tk.Frame(self.body,bg=BG)
        footer.pack(fill='x')
        self.refresh_button=self.button(footer,'刷新中…' if self.busy else '↻ 刷新',self.refresh)
        self.refresh_button.pack(side='left')
        self.button(footer,'设置 / 说明',self.help).pack(side='left')
        tk.Checkbutton(footer,text='置顶',variable=self.topmost,command=self.toggle_topmost,bg=BG,fg=MUTED,
            activebackground=BG,font=(FONT,9),selectcolor=BG,bd=0).pack(side='left',padx=8)
        self.button(footer,'隐藏',self.hide).pack(side='right')
        self.button(footer,'退出',self.quit).pack(side='right')
        timestamps=[v.get('updated',0) for v in self.result.values() if isinstance(v,dict)]
        last=max(timestamps,default=0)
        stamp=dt.datetime.fromtimestamp(last).strftime('%H:%M:%S') if last else '等待首次读取'
        self.label(self.body,f'账户更新 {stamp} · 每 5 分钟 · token 每 10 秒',8,MUTED).pack(anchor='w',pady=(5,0))
        self.tray.title=('AI 用量 · '+' | '.join(tooltip))[:127] if tooltip else 'AI 用量 · 尚未接入'
        self.root.update_idletasks()
        if self.root.winfo_y()+self.root.winfo_reqheight()>self.root.winfo_screenheight()-55:
            self.root.geometry(f'+{max(0,self.root.winfo_x())}+{max(0,self.root.winfo_screenheight()-self.root.winfo_reqheight()-65)}')

    def refresh(self):
        if self.busy or self.closed:
            return
        self.busy=True
        self.refresh_button.configure(text='刷新中…',state='disabled')
        def worker():
            try:
                result=collect()
                self.messages.put(('result',result))
            except Exception:
                self.messages.put(('error',None))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            while True:
                kind,value=self.messages.get_nowait()
                if kind=='result':
                    for key,new in value.items():
                        old=self.result.get(key,{})
                        if key == 'codex':
                            new['daily_usage'] = retain_daily_usage(old.get('daily_usage'),
                                new.get('daily_usage'), provider_ok=new.get('ok', False))
                        field={'codex':'cards','deepseek':'balances','radar':'tables'}.get(key)
                        if not new.get('ok') and field and old.get(field):
                            new={**old,**new,field:old[field]}
                            value[key]=new
                    self.result=value
                    self.busy=False
                    self.save_cache()
                    if not self.args.smoke:
                        self.process_alerts()
                    self.draw()
                    if self.args.smoke:
                        self.show()
                        self.root.attributes('-topmost',True)
                        self.root.after(2000,lambda:self.finish_smoke(True))
                elif kind=='error':
                    self.busy=False
                    self.draw()
                elif kind=='tokens':
                    self.tokens=value
                    self.token_busy=False
                    self.update_token_labels()
                    self.root.after(10000, self.refresh_tokens)
                elif kind=='show': self.show()
                elif kind=='refresh': self.refresh()
                elif kind=='quit': self.quit(); return
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100,self.poll)

    def save_cache(self):
        try:
            DATA.mkdir(exist_ok=True)
            target=DATA/'usage-cache.tmp'
            target.write_text(json.dumps(self.result,ensure_ascii=False,indent=2),encoding='utf-8')
            target.replace(DATA/'usage-cache.json')
        except OSError:
            pass

    def token_row(self, kind):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill='x')
        self.token_labels[kind] = self.label(row, '', 10, mono=True)
        self.token_labels[kind].pack(side='left')
        self.button(row, '明细', self.usage_details).pack(side='right')
        self.update_token_labels()

    def update_token_labels(self):
        fresh = self.tokens.get('date') == dt.datetime.now().date().isoformat()
        for kind, label in self.token_labels.items():
            data = self.tokens.get(kind, {})
            official_today = self.codex_rows()[-1] if kind == 'codex' else None
            if official_today and official_today['source'] == 'official':
                stale = '（旧）' if not official_today['ok'] else ''
                rate = cache_hit_text(data) if fresh else '—'
                text = f'官方今日 {token_text(official_today["total"])}{stale} · 本机缓存命中 {rate}'
            elif not fresh or not data:
                text = '今日 — · 正在读取本机记录'
            elif not data.get('available'):
                text = '今日 — · 未找到本机记录'
            elif data.get('partial'):
                text = f'今日 {token_text(data["total"])} · 缓存命中 — · 不完整'
            else:
                prefix = '今日暂估' if kind == 'codex' else '本机今日'
                text = f'{prefix} {token_text(data["total"])} · 本机缓存命中 {cache_hit_text(data)}'
            label.configure(text=text)

    def codex_rows(self):
        return codex_daily_rows(self.result.get('codex', {}).get('daily_usage'), self.tokens,
                                dt.datetime.now().date().isoformat())

    def refresh_tokens(self):
        if self.closed or self.token_busy:
            return
        self.token_busy = True
        def worker():
            try:
                result = self.token_reader.read()
            except Exception:
                result = {'date': dt.datetime.now().date().isoformat(),
                          'codex': {'available': False}, 'deepseek': {'available': False}}
            self.messages.put(('tokens', result))
        threading.Thread(target=worker, daemon=True).start()

    def usage_details(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('今日用量与重置卡')
        dialog.configure(bg=BG, padx=20, pady=16)
        content = tk.StringVar()
        self.label(dialog, '', 10, justify='left', textvariable=content).pack(anchor='w')
        def update():
            if not dialog.winfo_exists():
                return
            account = self.result.get('codex', {})
            daily = account.get('daily_usage') or {}
            latest = max(daily.get('buckets') or [], key=lambda b: b['date'], default=None)
            lines = ['Codex · 官方账户统计']
            if latest:
                lines += [f'最近已报 {latest["date"]} · {token_text(latest["total"])}'+('（旧数据）' if not daily.get('ok') else '')]
            else:
                lines += ['官方日统计暂未提供。']
            if self.codex_rows()[-1]['source'] != 'official':
                lines += ['今天官方尚未返回；面板暂用本机记录估算。']
            lines += ['', f'本机今日明细 · {dt.datetime.now():%Y-%m-%d} · 本地时区']
            for key, title in [('codex', 'Codex'), ('deepseek', 'DeepSeek / Claude Code')]:
                data = self.tokens.get(key, {})
                lines.append(title)
                if data.get('available') and self.tokens.get('date') == dt.datetime.now().date().isoformat():
                    lines += [f'输入 {token_text(data["input"])}  ·  输出 {token_text(data["output"])}',
                              f'其中缓存命中 {token_text(data["cached"])}（{cache_hit_text(data)}） · 合计 {token_text(data["total"])}',
                              '部分记录读取失败，当前为不完整统计。' if data.get('partial') else '']
                else:
                    lines += ['尚无可用统计。', '']
            credits = account.get('reset_credits') or {}
            count = credits.get('count')
            lines += [f'重置卡 · {count} 张可用' if count is not None else '重置卡 · 未提供']
            if not account.get('ok'):
                lines += ['账户数据尚未更新，以下可能为旧数据。']
            lines += [f'到期 {dt.datetime.fromtimestamp(stamp):%Y-%m-%d %H:%M}' for stamp in credits.get('expires', [])]
            lines += ['', 'm = 百万 token；卡片仅展示，到期明细可能不完整。',
                      '官方日统计每 5 分钟读取，日期与数值沿用官方，可能有延迟。',
                      '本机明细每 10 秒更新，仅含本机保留日志，不等同于官方总量。',
                      '本机缓存命中率 = 缓存命中 ÷ 输入（输入已含缓存）。',
                      '无输入或记录不完整显示 —；token 数量不等同于账单费用。']
            if self.tokens.get('updated'):
                lines += [f'本机记录读取于 {dt.datetime.fromtimestamp(self.tokens["updated"]):%H:%M:%S}']
            content.set('\n'.join(lines))
            self.root.after(1000, update)
        update()
        self.button(dialog, '关闭', dialog.destroy).pack(anchor='e', pady=(8,0))
        return dialog

    def show_trends(self):
        if self.trend_window is not None and self.trend_window.winfo_exists():
            self.trend_window.lift()
            return self.trend_window
        dialog = self.trend_window = tk.Toplevel(self.root)
        dialog.title('近 7 天 · Token 趋势')
        dialog.configure(bg=BG, padx=20, pady=16)
        dialog.resizable(False, False)
        self.label(dialog, '近 7 天用量', 14, bold=True).pack(anchor='w')
        self.label(dialog, '含今天 · m = 百万 token · 两张图分别缩放', 9, MUTED).pack(anchor='w', pady=(4,12))
        charts = {}
        for key, title in [('codex', 'Codex'), ('deepseek', 'DeepSeek / Claude Code')]:
            title_var = tk.StringVar(value=title)
            self.label(dialog, '', 10, bold=True, textvariable=title_var).pack(anchor='w')
            canvas = tk.Canvas(dialog, width=504, height=160, bg=BG, highlightthickness=0)
            canvas.pack(pady=(4,14))
            charts[key] = (title, title_var, canvas)
        status = tk.StringVar()
        self.label(dialog, '', 8, MUTED, textvariable=status, justify='left').pack(anchor='w')
        self.label(dialog, 'Codex 沿用官方日期和总量；≈ / 橙色为今日本机暂估，不计入官方合计。\nDeepSeek 仍按本机本地日期统计；— 为未提供，* 为不完整，旧为缓存。',
                   8, MUTED, justify='left').pack(anchor='w', pady=(6,0))
        self.button(dialog, '关闭', dialog.destroy).pack(anchor='e', pady=(6,0))

        def redraw():
            if not dialog.winfo_exists():
                return
            history = self.tokens.get('history') or []
            if self.tokens.get('date') != dt.datetime.now().date().isoformat():
                history = []
            for key, (title, title_var, canvas) in charts.items():
                canvas.delete('all')
                if not history and key != 'codex':
                    title_var.set(title)
                    canvas.create_text(252, 70, text='正在读取本机 7 天记录…', fill=MUTED, font=(FONT,10))
                    continue
                if key == 'codex':
                    rows = [(v['date'], dict(v, available=v['total'] is not None)) for v in self.codex_rows()]
                else:
                    rows = [(day['date'], day.get(key, {})) for day in history]
                known = [value for _, value in rows if value.get('available')]
                complete = all(v.get('ok') for _, v in rows)
                suffix = '' if complete else '（不完整）'
                if key == 'codex':
                    reported = [v for v in known if v['source'] == 'official']
                    stale = '（旧数据）' if any(not v['ok'] for v in reported) else ''
                    title_var.set(f'Codex · 官方已报 {token_text(sum(v["total"] for v in reported))}{stale}' if reported else 'Codex · 官方日统计暂未提供')
                else:
                    title_var.set(f'{title} · 本机 7 天 {token_text(sum(v.get("total", 0) for v in known))}{suffix}' if known else title+' · 未找到本机记录')
                maximum = max([v.get('total', 0) for v in known]+[1])
                baseline, plot_height, left, step = 128, 94, 58, 62
                canvas.create_line(26, baseline, 494, baseline, fill=LINE)
                color = GREEN if key == 'codex' else '#2C9B87'
                for i, (date, value) in enumerate(rows):
                    x = left + i*step
                    amount = value.get('total') or 0
                    available = value.get('available')
                    estimated = value.get('source') == 'local'
                    stale = value.get('source') == 'official' and not value.get('ok')
                    height = max(2, amount/maximum*plot_height) if amount > 0 else 0
                    if available and height:
                        canvas.create_rectangle(x-17, baseline-height, x+17, baseline,
                            fill=MUTED if value.get('partial') or stale else '#C89427' if estimated else color, outline='')
                    text = token_text(amount) if available else '—'
                    if estimated:
                        text = '≈' + text
                    if stale:
                        text += '旧'
                    if value.get('partial'):
                        text += '*'
                    canvas.create_text(x, baseline-height-11, text=text, fill=FG if available else MUTED,
                                       font=(MONO,8))
                    canvas.create_text(x, baseline+18, text=date[5:].replace('-', '/'), fill=MUTED, font=(MONO,9))
            stamp = self.tokens.get('updated')
            official_stamp = (self.result.get('codex', {}).get('daily_usage') or {}).get('updated')
            official_time = dt.datetime.fromtimestamp(official_stamp).strftime('%H:%M:%S') if official_stamp else '未提供'
            local_time = dt.datetime.fromtimestamp(stamp).strftime('%H:%M:%S') if stamp else '未读取'
            status.set(f'官方 {official_time} · 每 5 分钟  /  本机 {local_time} · 每 10 秒')
            self.root.after(10000, redraw)
        redraw()
        return dialog

    def process_alerts(self):
        if not self.alerts_enabled.get():
            return
        notices, state = check_alerts(self.result, self.alert_state)
        if notices:
            try:
                self.tray.notify('\n'.join(n['message'] for n in notices), 'Usage Panel · 低额度提醒')
                self.alert_status = f'最近提醒 {dt.datetime.now():%m/%d %H:%M}'
            except (OSError, NotImplementedError):
                self.alert_status = '系统通知未发送，请检查 Windows 通知设置'
                return
        if state != self.alert_state:
            self.alert_state = state
            try:
                DATA.mkdir(exist_ok=True)
                target = DATA/'alert-state.tmp'
                target.write_text(json.dumps(state), encoding='utf-8')
                target.replace(DATA/'alert-state.json')
            except OSError:
                self.alert_status = '提醒记录未保存，重启后可能再次提醒'

    def toggle_alerts(self):
        try:
            self.save_settings()
        except OSError:
            pass
        self.process_alerts()

    def save_settings(self):
        DATA.mkdir(exist_ok=True)
        target=DATA/'settings.tmp'
        target.write_text(json.dumps({'radar_mode':self.radar_mode.get(), 'topmost':self.topmost.get(),
            'alerts_enabled':self.alerts_enabled.get()},ensure_ascii=False,indent=2),encoding='utf-8')
        target.replace(DATA/'settings.json')

    def toggle_topmost(self):
        self.root.attributes('-topmost',self.topmost.get())
        try: self.save_settings()
        except OSError: pass

    def change_mode(self):
        try: self.save_settings()
        except OSError: pass
        self.draw()

    def help(self):
        dialog=tk.Toplevel(self.root)
        dialog.title('设置 / 连接说明')
        dialog.configure(bg=BG,padx=20,pady=18)
        tk.Checkbutton(dialog,text='低额度提醒',variable=self.alerts_enabled,command=self.toggle_alerts,
            bg=BG,fg=FG,activebackground=BG,font=(FONT,10),selectcolor=BG,bd=0).pack(anchor='w')
        self.label(dialog,'Codex 剩余 ≤10% · DeepSeek 余额 ≤¥10 / $1\n同一轮低状态只提醒一次，恢复后可再次提醒。\n'+self.alert_status,
            9,MUTED,justify='left').pack(anchor='w',pady=(3,12))
        text=('Codex\n自动使用本机 Codex CLI 的登录读取官方额度。\n\n'
              'DeepSeek API / Claude Code\n使用 Claude Code 已配置的 DeepSeek API 密钥读取余额。\n'
              '只访问 DeepSeek 官方余额接口，不发起模型对话。\n'
              'Codex 日统计优先官方；今天尚未提供时显示本机暂估。\n'
              '官方每 5 分钟读取，本机 token 与缓存命中率每 10 秒更新。\n\n'
              '数据说明\n进度条显示剩余比例；未返回的窗口不显示。\n'
              '读取失败时保留旧值并标注，过期值不当作新额度。\n'
              'IQ 来自 Codex Radar，支持综合、软件工程、视觉空间。\n综合分按两项的有效题量加权，并非人的智商。\n\n'
              '关闭窗口会留在系统托盘；右键托盘图标可以退出。')
        self.label(dialog,text,10,justify='left').pack(anchor='w')
        self.button(dialog,'打开 DeepSeek 控制台',lambda:webbrowser.open('https://platform.deepseek.com/')).pack(anchor='w',pady=(12,0))
        self.button(dialog,'打开 Codex Radar',lambda:webbrowser.open('https://codexradar.com/')).pack(anchor='w')
        self.button(dialog,'关闭',dialog.destroy).pack(anchor='e')

    def hide(self): self.root.withdraw()
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
    def auto_refresh(self):
        self.refresh()
        self.root.after(300000,self.auto_refresh)
    def tick(self):
        if not self.busy and self.root.state()!='withdrawn': self.draw()
        self.root.after(30000,self.tick)
    def quit(self):
        if self.closed: return
        self.closed=True
        self.tray.stop()
        self.root.destroy()
    def finish_smoke(self,success):
        if self.closed: return
        if self.args.trend_screenshot and not getattr(self, '_smoke_trend_captured', False):
            if not getattr(self, '_smoke_trend_prepared', False):
                trend = self.show_trends()
                trend.attributes('-topmost', True)
                trend.geometry('+100+80')
                self._smoke_trend_prepared = True
            else:
                trend = self.trend_window
                box = (trend.winfo_rootx(), trend.winfo_rooty(), trend.winfo_rootx()+trend.winfo_width(), trend.winfo_rooty()+trend.winfo_height())
                ImageGrab.grab(bbox=box).save(self.args.trend_screenshot)
                trend.destroy()
                self._smoke_trend_captured = True
            self.root.after(700, lambda: self.finish_smoke(success))
            return
        self.show()
        self.root.update()
        if self.args.screenshot:
            box=(self.root.winfo_rootx(),self.root.winfo_rooty(),self.root.winfo_rootx()+self.root.winfo_width(),self.root.winfo_rooty()+self.root.winfo_height())
            ImageGrab.grab(bbox=box).save(self.args.screenshot)
        if self.args.report:
            details = self.usage_details()
            details.update()
            details_ok = details.winfo_exists() and details.winfo_height() > 100
            details.destroy()
            self.hide()
            hidden_ok=self.root.state()=='withdrawn'
            self.show()
            self.root.update()
            shown_ok=self.root.state()=='normal'
            previous=self.radar_mode.get()
            self.radar_mode.set('软件工程')
            self.draw()
            switched_ok=self.scores==self.result.get('radar',{}).get('tables',{}).get('软件工程')
            self.radar_mode.set(previous)
            pathlib.Path(self.args.report).write_text(json.dumps({'completed':success,'codex_ok':self.result.get('codex',{}).get('ok'),
                'deepseek_ok':self.result.get('deepseek',{}).get('ok'),'radar_ok':self.result.get('radar',{}).get('ok'),'tray_visible':self.tray.visible,
                'hide_show_ok':hidden_ok and shown_ok,'score_switch_ok':switched_ok,
                'tokens_loaded': bool(self.tokens.get('updated')), 'reset_credits_loaded': self.result.get('codex', {}).get('reset_credits', {}).get('count') is not None,
                'details_ok': bool(details_ok),
                'history_days': len(self.tokens.get('history', [])),
                'official_daily_ok': (self.result.get('codex', {}).get('daily_usage') or {}).get('ok'),
                'official_history_days': sum(v['source'] == 'official' for v in self.codex_rows()),
                'local_estimate_days': sum(v['source'] == 'local' for v in self.codex_rows()),
                'cache_rate_visible': all('缓存命中' in label.cget('text') for label in self.token_labels.values()),
                'width':self.root.winfo_width(),'height':self.root.winfo_height()},indent=2),encoding='utf-8')
        self.quit()

def main():
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception: pass
    parser=argparse.ArgumentParser()
    parser.add_argument('--hidden',action='store_true')
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--screenshot')
    parser.add_argument('--trend-screenshot')
    parser.add_argument('--report')
    args=parser.parse_args()
    # Prevent duplicate tray instances; the OS releases this handle on exit.
    kernel=ctypes.windll.kernel32
    kernel.CreateMutexW.restype=ctypes.c_void_p
    handle=kernel.CreateMutexW(None,False,'Local\\UsagePanel_Desktop_1')
    if kernel.GetLastError()==183:
        ctypes.windll.user32.MessageBoxW(None,'用量面板已经在运行，请点击系统托盘里的绿色图标。','Usage Panel',0)
        return
    UsagePanel(args).root.mainloop()
    kernel.CloseHandle(ctypes.c_void_p(handle))

if __name__=='__main__': main()
