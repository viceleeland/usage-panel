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
        self.result = read_json(DATA/'usage-cache.json')
        for data in self.result.values():
            if isinstance(data,dict):
                data['ok']=False
                data['note']='上次缓存 · 正在刷新…'
        self.settings = read_json(DATA/'settings.json')
        self.scores = self.settings.get('scores', DEFAULT_SCORES.copy())
        self.radar_mode = tk.StringVar(value=self.settings.get('radar_mode','综合智能'))
        self.topmost = tk.BooleanVar(value=self.settings.get('topmost', False))
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
        tk.Frame(self.body, bg=LINE, height=1).pack(fill='x', pady=7)

    def button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg=BG, fg=FG, activebackground='#DDE8D9',
            relief='flat', bd=0, cursor='hand2', padx=7, pady=4, font=(FONT,9))

    def draw(self):
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
                self.label(self.body, data.get('note', '正在读取本机登录状态…'), 8, MUTED,
                    wraplength=480, justify='left').pack(anchor='w', pady=(4,5))
                if not data.get('ok') and data.get('updated'):
                    self.label(self.body, f'上次成功 {dt.datetime.fromtimestamp(data["updated"]):%m/%d %H:%M} · 当前为旧数据', 8, '#B08129').pack(anchor='w')
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
        self.line()
        self.label(self.body, 'SUMMARY', 10, bold=True, mono=True).pack(anchor='w')
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
        self.button(footer,'连接说明',self.help).pack(side='left')
        tk.Checkbutton(footer,text='置顶',variable=self.topmost,command=self.toggle_topmost,bg=BG,fg=MUTED,
            activebackground=BG,font=(FONT,9),selectcolor=BG,bd=0).pack(side='left',padx=8)
        self.button(footer,'隐藏',self.hide).pack(side='right')
        self.button(footer,'退出',self.quit).pack(side='right')
        timestamps=[v.get('updated',0) for v in self.result.values() if isinstance(v,dict)]
        last=max(timestamps,default=0)
        stamp=dt.datetime.fromtimestamp(last).strftime('%H:%M:%S') if last else '等待首次读取'
        self.label(self.body,f'更新于 {stamp}  ·  每 5 分钟刷新  ·  重置时间为本地时间',8,MUTED).pack(anchor='w',pady=(5,0))
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
                        field={'codex':'cards','deepseek':'balances','radar':'tables'}.get(key)
                        if not new.get('ok') and field and old.get(field):
                            new={**old,**new,field:old[field]}
                            value[key]=new
                    self.result=value
                    self.busy=False
                    self.save_cache()
                    self.draw()
                    if self.args.smoke:
                        self.show()
                        self.root.attributes('-topmost',True)
                        self.root.after(2000,lambda:self.finish_smoke(True))
                elif kind=='error':
                    self.busy=False
                    self.draw()
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

    def save_settings(self):
        DATA.mkdir(exist_ok=True)
        target=DATA/'settings.tmp'
        target.write_text(json.dumps({'radar_mode':self.radar_mode.get(), 'topmost':self.topmost.get()},ensure_ascii=False,indent=2),encoding='utf-8')
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
        dialog.title('连接说明')
        dialog.configure(bg=BG,padx=20,pady=18)
        text=('Codex\n自动使用本机 Codex CLI 的登录读取官方额度。\n\n'
              'DeepSeek API / Claude Code\n使用 Claude Code 已配置的 DeepSeek API 密钥读取余额。\n'
              '只访问 DeepSeek 官方余额接口，不发起模型对话。\n\n'
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
        self.show()
        self.root.update()
        if self.args.screenshot:
            box=(self.root.winfo_rootx(),self.root.winfo_rooty(),self.root.winfo_rootx()+self.root.winfo_width(),self.root.winfo_rooty()+self.root.winfo_height())
            ImageGrab.grab(bbox=box).save(self.args.screenshot)
        if self.args.report:
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
                'width':self.root.winfo_width(),'height':self.root.winfo_height()},indent=2),encoding='utf-8')
        self.quit()

def main():
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception: pass
    parser=argparse.ArgumentParser()
    parser.add_argument('--hidden',action='store_true')
    parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--screenshot')
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
