"""Read-only usage adapters. Tokens never leave their owning provider."""
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}

def codex_binary():
    override = os.environ.get('USAGE_PANEL_CODEX_EXE')
    if override and pathlib.Path(override).is_file():
        return override
    found = shutil.which('codex.exe')
    if found:
        return found
    npm = pathlib.Path(os.environ.get('APPDATA', ''))/'npm/node_modules/@openai'
    for path in npm.glob('codex*/**/bin/codex.exe'):
        return str(path)
    raise RuntimeError('未找到 Codex CLI，请先安装并登录 Codex。')

class CodexRPC:
    def __init__(self):
        self.proc = subprocess.Popen([codex_binary(), 'app-server', '--stdio'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=HIDDEN)
        self.messages = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass
        self.messages.put({'closed': True})

    def send(self, message):
        self.proc.stdin.write(json.dumps(message)+'\n')
        self.proc.stdin.flush()

    def call(self, method, params=None, timeout=25):
        self.next_id += 1
        request_id = self.next_id
        self.send({'id': request_id, 'method': method, 'params': params or {}})
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(.01, deadline-time.monotonic()))
            if message.get('closed'):
                raise RuntimeError('Codex 连接已关闭。')
            if message.get('id') == request_id:
                if 'error' in message:
                    raise RuntimeError('Codex 暂时无法读取额度，请确认已登录。')
                return message.get('result', {})
        raise TimeoutError()

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

def normalize_window(raw, label=None):
    if not isinstance(raw, dict):
        return None
    used = raw.get('usedPercent', raw.get('utilization'))
    if not isinstance(used, (float, int)) or isinstance(used, bool):
        return None
    duration = raw.get('windowDurationMins')
    if label is None:
        label = {300: '5 小时', 10080: '每周'}.get(duration, f'{duration} 分钟' if duration else '额度')
    reset = raw.get('resetsAt', raw.get('resets_at'))
    if isinstance(reset, str):
        try:
            reset = dt.datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp()
        except ValueError:
            reset = None
    return {'label': label, 'used': max(0, min(100, used)), 'reset': reset}

def codex_usage():
    rpc = None
    try:
        rpc = CodexRPC()
        rpc.call('initialize', {'clientInfo': {'name': 'usage_panel', 'title': 'Usage Panel', 'version': '1.2.0'}})
        rpc.send({'method': 'initialized'})
        result = rpc.call('account/rateLimits/read')
        buckets = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits')}
        cards = []
        for key, bucket in buckets.items():
            if not bucket or key != 'codex':
                continue
            windows = [normalize_window(bucket.get(k)) for k in ('primary', 'secondary')]
            windows = sorted([w for w in windows if w], key=lambda w: w['label'] != '每周')
            cards.append({'name': 'Codex' if key == 'codex' else (bucket.get('limitName') or key),
                'plan': bucket.get('planType') or '', 'windows': windows})
        if not cards:
            raise RuntimeError('账户未返回额度数据。')
        return {'ok': True, 'cards': cards, 'reset_credits': normalize_credits(result.get('rateLimitResetCredits')),
                'updated': time.time(), 'note': '官方账户额度 · 剩余百分比'}
    except Exception as exc:
        return {'ok': False, 'cards': [], 'note': str(exc) if isinstance(exc, RuntimeError) else '读取超时或连接失败，请稍后刷新。'}
    finally:
        if rpc:
            rpc.close()

def normalize_credits(raw):
    if not isinstance(raw, dict):
        return {'count': None, 'expires': []}
    count = raw.get('availableCount')
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = None
    expires = sorted(c['expiresAt'] for c in (raw.get('credits') or [])
        if isinstance(c, dict) and c.get('status') == 'available'
        and isinstance(c.get('expiresAt'), (int, float)) and not isinstance(c['expiresAt'], bool))
    return {'count': count, 'expires': expires}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def collect():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        codex = pool.submit(codex_usage)
        deepseek = pool.submit(deepseek_usage)
        radar = pool.submit(radar_scores)
        return {'codex': codex.result(), 'deepseek': deepseek.result(), 'radar': radar.result()}

def deepseek_usage():
    config_dir = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR', str(pathlib.Path.home()/'.claude')))
    env = read_json(config_dir/'settings.json').get('env', {})
    host = urllib.parse.urlparse(env.get('ANTHROPIC_BASE_URL','')).hostname
    token = os.environ.get('DEEPSEEK_API_KEY')
    if not token and host == 'api.deepseek.com':
        token = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')
    if not token:
        return {'ok':False,'note':'未找到 DeepSeek API 配置。'}
    req = urllib.request.Request('https://api.deepseek.com/user/balance', headers={
        'Authorization':'Bearer '+token, 'Accept':'application/json', 'User-Agent':'UsagePanel/1.0'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=18) as response:
            data=json.load(response)
        balances=[{k: item.get(k) for k in ('currency','total_balance','granted_balance','topped_up_balance')}
                  for item in data.get('balance_infos',[]) if item.get('total_balance') is not None]
        if not balances: raise ValueError('Missing balance')
        return {'ok':True,'available':data.get('is_available'),'balances':balances,
                'model':env.get('ANTHROPIC_MODEL',''),'updated':time.time(),
                'note':'余额来自 DeepSeek 官方 API · 按量付费'}
    except urllib.error.HTTPError as exc:
        return {'ok':False,'note':f'DeepSeek 余额读取失败（HTTP {exc.code}），请检查 API 配置。'}
    except Exception:
        return {'ok':False,'note':'DeepSeek 暂时无法连接，请稍后刷新。'}

RADAR_LEVELS=['ultra','max','xhigh','high','medium','low']
RADAR_MODELS={'gpt-6-astra':'Astra','gpt-5.6-sol':'Sol','gpt-5.6-terra':'Terra','gpt-5.6-luna':'Luna'}

def radar_tables(software,visual):
    def points(payload):
        return {(p.get('model'),p.get('effort')):p for p in payload.get('points',[])
            if p.get('model') in RADAR_MODELS and p.get('effort') in RADAR_LEVELS
            and isinstance(p.get('iq'),(int,float)) and 0<=p['iq']<=150}
    a,b=points(software),points(visual)
    tables={mode:{name:['']*6 for name in RADAR_MODELS.values()} for mode in ['综合智能','软件工程','视觉空间']}
    for key in a.keys()|b.keys():
        model,effort=key
        name=RADAR_MODELS[model]
        index=RADAR_LEVELS.index(effort)
        if key in a: tables['软件工程'][name][index]=round(a[key]['iq'],1)
        if key in b: tables['视觉空间'][name][index]=round(b[key]['iq'],1)
        if key in a and key in b:
            x,y=a[key],b[key]
            nx=x.get('valid_tasks',x.get('total',0)) or 0
            ny=y.get('valid_tasks',y.get('total',0)) or 0
            if nx>0 and ny>0:
                tables['综合智能'][name][index]=round((x['iq']*nx+y['iq']*ny)/(nx+ny),1)
    return tables

def radar_scores():
    try:
        def fetch(path):
            request=urllib.request.Request('https://codexradar.com'+path,headers={'User-Agent':'UsagePanel/1.0','Accept':'application/json'})
            with urllib.request.urlopen(request,timeout=18) as r:
                return json.load(r)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(fetch,'/api/intelligence-efficiency-metrics')
            second=pool.submit(fetch,'/api/visual-spatial-reasoning')
            software,visual=first.result(),second.result()
        tables=radar_tables(software,visual)
        if not any(v!='' for row in tables['软件工程'].values() for v in row):
            raise ValueError('No valid radar results')
        return {'ok':True,'tables':tables,'updated':time.time(),
            'software_updated':software.get('source_updated_at'),'visual_updated':visual.get('source_updated_at'),
            'note':'Codex Radar 社区评测 · 综合分按有效题量加权'}
    except Exception:
        return {'ok':False,'note':'Codex Radar 暂时不可用，请稍后刷新。'}

if __name__ == '__main__':
    print(json.dumps(collect(), ensure_ascii=True))
