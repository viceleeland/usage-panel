"""Read-only usage adapters. Tokens never leave their owning provider."""
import concurrent.futures
import datetime as dt
import json
import math
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

def normalize_daily_usage(raw):
    """Keep the service's calendar dates and totals without filling missing days."""
    rows = raw.get('dailyUsageBuckets') if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {'ok': False, 'buckets': [], 'note': '官方每日统计暂不可用。'}
    buckets = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date, tokens = row.get('startDate'), row.get('tokens')
        if not isinstance(date, str) or not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            continue
        try:
            if dt.date.fromisoformat(date).isoformat() != date:
                continue
        except ValueError:
            continue
        buckets[date] = tokens
    return {'ok': True, 'buckets': [{'date': date, 'total': buckets[date]} for date in sorted(buckets)],
            'note': '官方账户日统计 · 按接口日期显示' if buckets else '官方尚未返回可用每日数据。'}

def codex_usage():
    rpc = None
    try:
        rpc = CodexRPC()
        rpc.call('initialize', {'clientInfo': {'name': 'usage_panel', 'title': 'Usage Panel', 'version': '1.3.2'}})
        rpc.send({'method': 'initialized'})
        result = {}
        cards = []
        note = '官方账户额度 · 剩余百分比'
        try:
            result = rpc.call('account/rateLimits/read')
            buckets = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits')}
            for key, bucket in buckets.items():
                if not bucket or key != 'codex':
                    continue
                windows = [normalize_window(bucket.get(k)) for k in ('primary', 'secondary')]
                windows = sorted([w for w in windows if w], key=lambda w: w['label'] != '每周')
                cards.append({'name': 'Codex', 'plan': bucket.get('planType') or '', 'windows': windows})
            if not cards:
                raise RuntimeError('账户未返回额度数据。')
        except Exception as exc:
            cards = []
            note = str(exc) if isinstance(exc, RuntimeError) else '额度读取超时或连接失败，请稍后刷新。'
        # These account endpoints have independent availability. A quota error
        # must not prevent the service's authoritative daily totals being read.
        try:
            daily_usage = normalize_daily_usage(rpc.call('account/usage/read'))
        except Exception:
            daily_usage = {'ok': False, 'buckets': [], 'note': '官方每日统计读取失败，请稍后刷新。'}
        daily_usage['updated'] = time.time()
        data = {'ok': bool(cards), 'cards': cards, 'daily_usage': daily_usage, 'note': note}
        if cards:
            data['reset_credits'] = normalize_credits(result.get('rateLimitResetCredits'))
            data['updated'] = time.time()
        return data
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
RADAR_MODELS={
    'gpt-6-astra':'GPT-6 Astra', 'gpt-6.1-sol':'GPT-6.1 Sol',
    'gpt-6-sol':'GPT-6 Sol', 'gpt-6-luna':'GPT-6 Luna',
    'gpt-5.6-sol':'GPT-5.6 Sol', 'gpt-5.6-terra':'GPT-5.6 Terra',
    'gpt-5.6-luna':'GPT-5.6 Luna', 'gpt-5.5':'GPT-5.5',
}
# Mirror the homepage's hasReliableGpt6Score, not a generic GPT-6 prefix rule:
# Astra still needs both dimensions. Verified at https://codexradar.com/ 2026-10-02.
RADAR_SAMPLE_GATED_MODELS={'gpt-6.1-sol','gpt-6-sol','gpt-6-luna'}

def _radar_number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        number=float(value)
        return number if math.isfinite(number) else None
    except (TypeError,ValueError,OverflowError):
        return None

def _radar_visual_summary(payload):
    """The visual endpoint can return its compact summary or its raw task table."""
    if not isinstance(payload,dict) or payload.get('schema')!=1 or payload.get('benchmark_id')!='pompeii-adjacency':
        raise ValueError('Unsupported visual benchmark')
    if payload.get('type')=='visual_spatial_reasoning_summary':
        return payload
    tasks,combos,cells=payload.get('tasks'),payload.get('combos'),payload.get('cells')
    if (payload.get('scoring_mode')!='continuous-macro' or not isinstance(tasks,list) or not tasks
            or not isinstance(combos,list) or not combos or not isinstance(cells,dict)):
        raise ValueError('Unsupported visual table')
    points=[]
    latest=None
    for combo in combos:
        if not isinstance(combo,dict):
            continue
        model,effort=combo.get('model'),combo.get('effort')
        if not isinstance(model,str) or model not in RADAR_MODELS or effort not in RADAR_LEVELS:
            continue
        scores=[]
        for task in tasks:
            if not isinstance(task,dict) or task.get('id') is None:
                continue
            cell=cells.get(f'{task["id"]}|{model}|{effort}')
            runners=cell.get('ran_by') if isinstance(cell,dict) else None
            if not isinstance(runners,list) or not runners or not isinstance(runners[0],dict):
                continue
            # The website uses the latest runner only, not every historical run.
            runner=runners[0]
            score=_radar_number(runner.get('score'))
            if score is not None:
                scores.append(max(0,min(1,score)))
            try:
                graded=dt.datetime.fromisoformat(runner['graded_at'].replace('Z','+00:00'))
                if graded.tzinfo is not None and (latest is None or graded>latest):
                    latest=graded
            except (KeyError,TypeError,ValueError,AttributeError):
                pass
        if scores:
            points.append({'model':model,'effort':effort,'iq':sum(scores)/len(scores)*150,
                           'valid_tasks':len(scores)})
    return {'schema':1,'type':'visual_spatial_reasoning_summary','benchmark_id':'pompeii-adjacency',
            'points':points,'source_updated_at':latest.isoformat() if latest else None}

def _radar_points(payload,software=False):
    if software:
        if not isinstance(payload,dict) or payload.get('benchmark_id')!='deep-swe':
            raise ValueError('Unsupported software benchmark')
        if payload.get('schema')==3 and payload.get('mode')=='equal_latest_3':
            sample_field='total'
        elif payload.get('schema')==2 and payload.get('mode')=='weighted_latest_3':
            sample_field='weighted_total'
        else:
            raise ValueError('Unsupported software scoring mode')
    else:
        payload=_radar_visual_summary(payload)
        sample_field='valid_tasks'
    if not isinstance(payload.get('points'),list):
        raise ValueError('Missing radar points')
    points={}
    for point in payload['points']:
        if not isinstance(point,dict):
            continue
        model,effort=point.get('model'),point.get('effort')
        if not isinstance(model,str) or model not in RADAR_MODELS or effort not in RADAR_LEVELS:
            continue
        iq=_radar_number(point.get('iq'))
        samples=_radar_number(point.get(sample_field))
        if iq is None or not 0<=iq<=150 or (software and (samples is None or samples<=0)):
            continue
        samples=max(0,samples or 0)
        if model in RADAR_SAMPLE_GATED_MODELS and samples<30:
            continue
        points[model,effort]={'iq':iq,'samples':samples}
    return points

def radar_tables(software,visual):
    a,b=_radar_points(software,True),_radar_points(visual)
    tables={mode:{name:['']*6 for name in RADAR_MODELS.values()} for mode in ['综合智能','软件工程','视觉空间']}
    for key in a.keys()|b.keys():
        model,effort=key
        name=RADAR_MODELS[model]
        index=RADAR_LEVELS.index(effort)
        # The site renders Math.round(iq): positive half ties round up, not to even.
        if key in a: tables['软件工程'][name][index]=math.floor(a[key]['iq']+.5)
        if key in b: tables['视觉空间'][name][index]=math.floor(b[key]['iq']+.5)
        if key in a and key in b:
            x,y=a[key],b[key]
            nx,ny=max(1,x['samples']),max(1,y['samples'])
            tables['综合智能'][name][index]=math.floor((x['iq']*nx+y['iq']*ny)/(nx+ny)+.5)
        elif key in a and model in RADAR_SAMPLE_GATED_MODELS:
            tables['综合智能'][name][index]=math.floor(a[key]['iq']+.5)
    return tables

def radar_scores():
    try:
        def fetch(path):
            request=urllib.request.Request('https://codexradar.com'+path,headers={'User-Agent':'UsagePanel/1.0','Accept':'application/json'})
            with urllib.request.urlopen(request,timeout=18) as r:
                status=r.headers.get('X-Codex-Cache','')
                if not status or status.startswith('STALE') or status=='ERROR':
                    raise ValueError('Radar cache is not current')
                return json.load(r)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(fetch,'/api/intelligence-efficiency-metrics')
            second=pool.submit(fetch,'/api/visual-spatial-reasoning')
            software,visual=first.result(),second.result()
        visual=_radar_visual_summary(visual)
        tables=radar_tables(software,visual)
        if not any(v!='' for row in tables['软件工程'].values() for v in row):
            raise ValueError('No valid radar results')
        return {'ok':True,'tables':tables,'updated':time.time(),
            'software_updated':software.get('source_updated_at'),'visual_updated':visual.get('source_updated_at'),
            'note':'Codex Radar 社区评测 · 沿用网站样本门槛、加权与整数显示'}
    except Exception:
        return {'ok':False,'note':'Codex Radar 暂时不可用，请稍后刷新。'}

if __name__ == '__main__':
    print(json.dumps(collect(), ensure_ascii=True))
