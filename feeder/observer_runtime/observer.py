"""Passive Lao Panel observer. Only panel.list/panel.observe are permitted.

No sender, recovery, task assignment, DB writes, navigation or browser mutations.
"""
import argparse
import collections
import concurrent.futures
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from urllib.request import Request, build_opener, ProxyHandler

VERSION = '2026.11.07-passive.10'
SCHEMA = 'LAO_PASSIVE_OBSERVATION_V1'
ROOT = Path(__file__).resolve().parent
ALLOWED_COMMANDS = frozenset({'panel.list', 'panel.observe'})
PROTECTED = re.compile(r'blocked by safety|automatic approval.{0,40}(?:rejected|denied)|안전.{0,20}(?:차단|거절|거부)|권한.{0,20}(?:거절|거부)', re.I)
ERROR = re.compile(r'network error|connection (?:lost|error)|resume stream unavailable|something went wrong|연결.{0,10}(?:오류|끊)|네트워크.{0,10}오류', re.I)
LIMIT = re.compile(r'rate limit|usage limit|too many requests|사용량.{0,12}(?:한도|초과)|사용 한도', re.I)
EXPIRED = re.compile(r'conversation (?:not found|unavailable)|maximum.{0,10}length|대화.{0,15}(?:찾을 수 없|만료|최대 길이)', re.I)
STORAGE_BLOCKED = re.compile(r'(?:GitHub|브랜치|main|branch|ref).{0,40}(?:저장|반영|갱신|update|write).{0,20}(?:실패|오류|미완료|failed|error)|(?:저장|반영)\s*(?:실패|미완료)|main\s*미반영', re.I)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(text, encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def command(endpoint, name, params=None, timeout=7):
    if name not in ALLOWED_COMMANDS:
        raise ValueError('PASSIVE_COMMAND_ALLOWLIST')
    if endpoint != 'http://127.0.0.1:32722':
        raise ValueError('LOCAL_REGISTERED_PANEL_ENDPOINT_ONLY')
    payload = {'command': name, 'params': params or {}}
    req = Request(endpoint + '/command', data=json.dumps(payload).encode(),
                  headers={'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
        data = json.load(response)
    if not data.get('success'):
        raise RuntimeError(str(data.get('error') or data.get('detail') or 'PANEL_RESPONSE_FAILED')[:250])
    return data


def assignment(text):
    result = {'worker': None, 'job_id': None, 'family_title': None, 'marker': None,
              'repository': None, 'branch': None}
    marker = re.search(r'\[((?:WORKC|NOTEX|LAW|KIIP)[^\]\r\n]{1,220})\]', text)
    if marker:
        result['marker'] = marker.group(1)
        worker = re.search(r'(?:^|:)([CN]\d{2})(?=:|$)', marker.group(1))
        if worker:
            result['worker'] = worker.group(1)
    job = re.search(r'(?:Job ID\s*:\s*|:)(F-[a-f0-9]{12,40})', text, re.I)
    if job:
        result['job_id'] = job.group(1)
    title = re.search(r'법령가족\s*:\s*([^\r\n]+?)(?=\s*(?:Job\s*ID|전체\s*문서)\s*:|\r?\n|$)', text)
    if title:
        result['family_title'] = title.group(1).strip()[:200]
    for key, pattern in [('repository',r'저장소\s*:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)'),
                         ('branch',r'브랜치\s*:\s*([^\r\n]+?)(?=\s*(?:원문\s*기준\s*커밋|법령가족|Job\s*ID)\s*:|\r?\n|$)')]:
        match = re.search(pattern,text)
        if match:
            result[key] = match.group(1).strip()
    return result


def protected_report(text):
    for match in PROTECTED.finditer(text):
        tail = text[match.end():match.end()+35]
        if re.match(r'\s*(?:이?\s*아닌|이?\s*아님|(?:은|는|이|가)?\s*없|(?:은|는)?\s*발생하지)', tail):
            continue
        if re.match(r'\s*(?:이나|또는)\s*expected\s*SHA\s*충돌\s*메시지.{0,6}없', tail, re.I):
            continue
        return True
    return bool(re.search(r'\b(?:SAFETY_HOLD|RETIRED_HOLD|SOURCE_HOLD|NO_RETRY)\b', text))


def classify(obs, current_assignment=None):
    """UI state and assistant claims remain different evidence classes."""
    if not obs or obs.get('success') is False:
        return 'UNKNOWN', ['OBSERVATION_FAILED']
    if obs.get('readyState') != 'complete':
        return 'LOADING', ['DOCUMENT_NOT_READY']
    if obs.get('page_online') is False:
        return 'OFFLINE', ['PAGE_OFFLINE']
    elements = obs.get('elements') or []
    controls = [str(x.get('ariaLabel') or x.get('text') or '').strip()
                for x in elements if x.get('tag') == 'button' and not x.get('disabled')]
    approval = any(re.fullmatch(r'(?:Allow(?: once| always)?|Approve|Confirm|허용|한 번 허용|승인|권한 허용)', x, re.I) for x in controls)
    if approval:
        return 'APPROVAL_REQUIRED', ['LIVE_APPROVAL_CONTROL']
    login = any(re.fullmatch(r'(?:Log in|Sign in|로그인)', x, re.I) for x in controls)
    if login:
        return 'LOGIN_REQUIRED', ['LIVE_LOGIN_CONTROL']
    if obs.get('transportError'):
        return 'TRANSPORT_ERROR', ['LIVE_TRANSPORT_ERROR']
    ui = str(obs.get('status') or 'UNKNOWN').upper()
    if ui == 'WORKING' or any(re.search(r'^(?:Stop generating|Stop streaming|생성 중지|응답 중지)$', x, re.I) for x in controls):
        return 'WORKING', []
    drafts = [str(x.get('text') or '') for x in elements if x.get('role') == 'textbox']
    if any(x.strip() for x in drafts) or 'send-ready' in (obs.get('evidence') or []):
        return 'DRAFT_PRESENT', ['COMPOSER_NOT_EMPTY']
    if any(re.fullmatch(r'(?:새 대화 시작|Start a new chat|Start new chat)', x, re.I) for x in controls):
        return 'CONVERSATION_RECOVERY_REQUIRED', ['LIVE_NEW_CONVERSATION_RECOVERY_CONTROL']
    alerts = ' '.join(str(x) for x in (obs.get('page_alerts') or []))
    for pattern, state in [(LIMIT, 'RATE_LIMITED'), (EXPIRED, 'CONTEXT_UNAVAILABLE'), (ERROR, 'UI_ERROR')]:
        if pattern.search(alerts):
            return state, ['LIVE_PAGE_ALERT']
    assistant = str(obs.get('lastAssistant') or '')
    if protected_report(assistant):
        return 'REPORTED_HOLD', ['ASSISTANT_REPORTED_SAFETY_OR_PERMISSION']
    if ui in {'ERROR', 'NETWORK_ERROR', 'DISCONNECTED', 'UNKNOWN', 'WAITING_APPROVAL'}:
        return ui, ['NATIVE_PANEL_STATE']
    if ui in {'IDLE', 'READY', 'DONE', 'COMPLETE', 'COMPLETED'}:
        current = current_assignment or assignment(str(obs.get('lastUser') or ''))
        same_task = any(current.get(k) and current[k] in assistant for k in ('job_id','family_title'))
        active_hold = re.search(r'(?:최종\s*상태|현재\s*상태|운영상\s*상태|status)\s*[:：]?\s*HOLD|\b[CN]\d{2}\s*(?:[—–:-]\s*)?HOLD\b|HOLD\s*(?:유지|중)|저장\s*미완료|반영\s*실패', assistant, re.I)
        if same_task and active_hold:
            if STORAGE_BLOCKED.search(assistant):
                return 'STORAGE_BLOCKED', ['CURRENT_ASSIGNMENT_STORAGE_FAILURE', 'REPAIR_REQUIRES_ERROR_DIAGNOSIS']
            return 'REPORTED_HOLD', ['CURRENT_ASSIGNMENT_REPORTED_HOLD']
        complete = re.search(r'WHOLE FAMILY COMPLETE|전체.{0,15}검수.{0,10}완료|법령가족\s*전체\s*완료|(?:최종\s*)?(?:확인|상태|status)\s*[:：]?\s*COMPLETE|\b[CN]\d{2}\s*(?:[—–:-]\s*)?COMPLETE\b|완료\s*영수증\s*JSON\s*[—–:-]\s*COMPLETE', assistant, re.I)
        if same_task and complete:
            return 'COMPLETION_REPORTED', ['ASSISTANT_CLAIM_NOT_RECEIPT_VERIFICATION']
        if complete and current.get('job_id') and not same_task:
            return 'IDLE', ['PREVIOUS_COMPLETION_NOT_CURRENT_ASSIGNMENT']
        if ERROR.search(assistant):
            return 'REPORTED_ERROR', ['ASSISTANT_REPORTED_ERROR']
        return 'IDLE', []
    return 'UNKNOWN', ['UNRECOGNIZED_NATIVE_STATE']


def summarize(panel, obs, previous, now):
    url = str(obs.get('url') or '')
    scope = assignment(str(obs.get('lastUser') or ''))
    prior_scope = (previous or {}).get('assignment') or {}
    if (previous or {}).get('url') == url and scope.get('marker') and scope['marker'] == prior_scope.get('marker'):
        scope = {k: v or prior_scope.get(k) for k, v in scope.items()}
    state, reasons = classify(obs, scope)
    if url != panel.get('url'):
        state, reasons = 'CONVERSATION_CHANGED', ['LIST_OBSERVE_URL_MISMATCH']
    signature = fingerprint([url, obs.get('lastUser'), obs.get('lastAssistant'), obs.get('assistantCount'), obs.get('status')])
    old = previous or {}
    continuity = (old.get('observation_ok') and old.get('url') == url
                  and (old.get('assignment') or {}).get('marker') == scope.get('marker')
                  and 0 <= now - old.get('observed_at', 0) < 60)
    unchanged_since = old.get('unchanged_since', now) if continuity and old.get('signature') == signature else now
    state_since = old.get('state_since', now) if continuity and old.get('state') == state else now
    unchanged = max(0, now - unchanged_since)
    if unchanged >= 600 and state == 'WORKING':
        reasons.append('UI_UNCHANGED_10_MIN_NOT_PROOF_OF_STALL')
    elif unchanged >= 300 and state == 'IDLE':
        reasons.append('IDLE_OBSERVED_5_MIN')
    commits = list(dict.fromkeys(re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])', str(obs.get('lastAssistant') or ''))))
    return dict(panel_id=panel['panelId'], panel_name=panel.get('name'), url=url,
                title=obs.get('title'), native_state=obs.get('status'), state=state,
                reasons=reasons, observed_at=now, observation_ok=state not in {'UNKNOWN', 'DISCONNECTED', 'CONVERSATION_CHANGED', 'LOADING', 'OFFLINE', 'TRANSPORT_ERROR'},
                busy_evidence=obs.get('evidence') or [],
                transport_error=str(obs.get('transportError') or '')[:300] or None,
                assistant_count=obs.get('assistantCount'), assignment=scope,
                reported_commits=commits[-3:], receipt_verification='NOT_PERFORMED',
                recovery_marker=(re.search(r'\[(?:LAW-RECOVERY-V1|OBSERVER-RECOVERY):[^\]\r\n]+\]', str(obs.get('lastUser') or '')) or [None])[0],
                signature=signature, unchanged_since=unchanged_since, unchanged_seconds=round(unchanged, 1),
                state_since=state_since, state_seconds=round(max(0, now-state_since), 1),
                assistant_excerpt=str(obs.get('lastAssistant') or '')[-900:],
                draft_present=bool('send-ready' in (obs.get('evidence') or []) or any(str(x.get('text') or '').strip() for x in obs.get('elements', []) if x.get('role') == 'textbox')))


def observe_one(panel, cfg, previous):
    started = time.monotonic()
    try:
        result = command(cfg['endpoint'], 'panel.observe', {'panelId': panel['panelId'], 'maxElements': 1000, 'maxMessages': 1}, cfg['timeout_seconds'])
        obs = result.get('observation')
        if not isinstance(obs, dict):
            raise RuntimeError('MISSING_OBSERVATION')
        row = summarize(panel, obs, previous, time.time())
    except Exception as exc:
        row = dict(panel_id=panel['panelId'], panel_name=panel.get('name'), url=panel.get('url'),
                   state='UNKNOWN', observation_ok=False, observed_at=time.time(),
                   reasons=['OBSERVATION_FAILED'], error=str(exc)[:300])
    row['latency_ms'] = round((time.monotonic()-started)*1000)
    return row


def control_snapshot(cfg):
    result = []
    for raw in cfg.get('workflow_control_files', []):
        p = Path(raw)
        try:
            exists = p.is_file()
            row = {'path': str(p), 'exists': exists}
            if exists:
                row['modified_at'] = p.stat().st_mtime
                row['value'] = p.read_text(encoding='utf-8-sig')[:1000]
            result.append(row)
        except OSError as exc:
            result.append({'path': str(p), 'read_error': str(exc)[:200]})
    return result


def append_bounded(path, value, cap=8*1024*1024):
    path = Path(path)
    line = json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'
    size = path.stat().st_size if path.exists() else 0
    if size + len(line.encode()) > cap:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(line)
    return True


def cycle(cfg, run, boot, sequence):
    start = time.time()
    old = read_json(run/'latest.json', {})
    previous = {r['panel_id']: r for r in old.get('panels', [])}
    list_error = None
    try:
        panels = command(cfg['endpoint'], 'panel.list', timeout=cfg['timeout_seconds'])['panels']
        ids = [p['panelId'] for p in panels]
        if len(ids) != len(set(ids)):
            raise ValueError('DUPLICATE_PANEL_IDS')
        panels = [p for p in panels if p['panelId'] in cfg['expected_panels']]
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg['concurrency']) as pool:
            rows = list(pool.map(lambda p: observe_one(p, cfg, previous.get(p['panelId'])), panels))
    except Exception as exc:
        list_error = str(exc)[:300]
        rows = [dict(panel_id=p, state='DISCONNECTED', observation_ok=False,
                     reasons=['PANEL_LIST_FAILED'], error=list_error, observed_at=time.time())
                for p in cfg['expected_panels']]
    seen = {r['panel_id'] for r in rows}
    for p in cfg['expected_panels']:
        if p not in seen:
            rows.append(dict(panel_id=p, state='UNAVAILABLE', observation_ok=False,
                             reasons=['EXPECTED_PANEL_MISSING'], observed_at=time.time()))
    rows.sort(key=lambda r: r['panel_id'])
    at = time.time()
    valid = sum(r.get('observation_ok', False) for r in rows)
    report = dict(schema=SCHEMA, version=VERSION, pc=cfg['pc'], mode='PASSIVE',
                  pid=os.getpid(), boot=boot, sequence=sequence, observed_at=at,
                  interval_seconds=cfg['interval_seconds'], scan_started_at=start,
                  scan_duration_seconds=round(at-start, 3), expected_panels=len(cfg['expected_panels']),
                  observed_panels=len(rows), valid_panels=valid,
                  working_panels=sum(r['state']=='WORKING' for r in rows),
                  idle_panels=sum(r['state']=='IDLE' for r in rows),
                  completion_reported_panels=sum(r['state']=='COMPLETION_REPORTED' for r in rows),
                  blocked_panels=sum(r['state'] in {'STORAGE_BLOCKED','REPORTED_HOLD','APPROVAL_REQUIRED','LOGIN_REQUIRED'} for r in rows),
                  health='OK' if valid == len(rows) == len(cfg['expected_panels']) else 'DEGRADED',
                  counts=dict(collections.Counter(r['state'] for r in rows)),
                  panels=rows, workflow_controls=control_snapshot(cfg),
                  list_error=list_error, automatic_actions_enabled=False,
                  receipt_verification='NOT_PERFORMED', commands_allowed=sorted(ALLOWED_COMMANDS))
    events = []
    for row in rows:
        before = previous.get(row['panel_id'], {})
        if [before.get(k) for k in ('state','signature')] != [row.get(k) for k in ('state','signature')]:
            events.append(dict(at=at, pc=cfg['pc'], panel_id=row['panel_id'], previous_state=before.get('state'),
                               state=row['state'], assignment=row.get('assignment'), reasons=row.get('reasons'),
                               url=row.get('url'), signature=row.get('signature')))
    day = time.strftime('%Y-%m-%d')
    report['history_written'] = append_bounded(run/'history'/f'{day}.jsonl',
        {k:v for k,v in report.items() if k not in {'workflow_controls','panels'}} | {'panels': [
            {k:v for k,v in r.items() if k not in {'assistant_excerpt','title'}} for r in rows]})
    for event in events:
        append_bounded(run/'events'/f'{day}.jsonl', event)
    atomic_json(run/'latest.json', report)
    atomic_json(run/'health.json', {k:v for k,v in report.items() if k not in {'panels','workflow_controls'}})
    atomic_text(run/'dashboard.html', render(report))
    return report


def render(report):
    esc = lambda x: html.escape(str(x if x is not None else ''))
    names = {'WORKING':'작업 중', 'IDLE':'유휴', 'COMPLETION_REPORTED':'완료 보고 · 저장 미검증',
             'UNKNOWN':'관측 실패', 'DISCONNECTED':'연결 끊김', 'DRAFT_PRESENT':'입력 초안 있음',
             'REPORTED_HOLD':'보류 보고', 'STORAGE_BLOCKED':'저장 실패 · 원인 확인 필요', 'APPROVAL_REQUIRED':'승인 대기', 'LOGIN_REQUIRED':'로그인 필요',
             'LOADING':'로딩 중', 'UNAVAILABLE':'패널 없음'}
    rows = []
    for r in report.get('panels', []):
        a = r.get('assignment') or {}
        label = a.get('worker') or r.get('panel_name') or r['panel_id']
        state = r['state']; color = 'green' if state=='WORKING' else 'blue' if state in {'IDLE','COMPLETION_REPORTED'} else 'orange'
        rows.append('<tr><td><b>'+esc(label)+'</b><small>'+esc(r['panel_id'])+'</small></td><td><span class="'+color+'">'+esc(names.get(state,state))+'</span></td><td>'+esc(a.get('family_title') or r.get('title') or '—')+'<small>'+esc(a.get('job_id') or '')+'</small></td><td>'+str(round(r.get('unchanged_seconds',0)))+'초</td><td>'+esc(r.get('latency_ms',''))+' ms</td><td>'+esc(', '.join(r.get('reasons') or []) or '—')+'</td></tr>')
    controls = ''.join('<li>'+esc(Path(c['path']).name)+': '+esc((c.get('value') or '없음').strip())+'</li>' for c in report.get('workflow_controls',[]) if c.get('exists'))
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta http-equiv="refresh" content="10"><meta name="viewport" content="width=device-width"><title>Lao Panel 관측기</title><style>
body{margin:0;background:#f3f6fa;color:#16253b;font:15px system-ui,sans-serif}main{max-width:1400px;margin:36px auto;padding:0 26px}h1{font-size:28px;margin-bottom:8px}.sub,small{color:#596b80}small{display:block;margin-top:6px;font-size:12px}.cards{display:flex;gap:16px;margin:25px 0}.card{background:white;padding:18px 24px;border:1px solid #dce3ee;border-radius:12px;min-width:140px}.number{font-size:30px;font-weight:700}table{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden}th,td{text-align:left;padding:16px;border-bottom:1px solid #e2e8f0;vertical-align:top}th{background:#e8eef6;font-size:13px}td:last-child{font-size:12px;max-width:260px;overflow-wrap:anywhere}.green,.blue,.orange{padding:6px 9px;display:inline-block;border-radius:6px;font-size:13px;background:#e1f3ea;color:#196146}.blue{background:#e3edfc;color:#285a9a}.orange{background:#fff0d8;color:#8d5100}.note{line-height:1.7;margin:22px 0}.stale{background:#ffe1df;color:#8c221b;padding:14px;border-radius:8px}details{margin-top:24px}li{white-space:pre-wrap;overflow-wrap:anywhere}</style><main><div class="sub">LAO PANEL · PASSIVE OBSERVER</div><h1>'''+esc(report['pc'])+''' 패널 관측</h1><p class="sub">자동 공급·전송·패널 변경 없이 현재 화면을 관측합니다.</p><div id="fresh"></div><div class="cards"><div class="card">실제 작업 중<div class="number">'''+str(report.get('working_panels',0))+'/'+str(report['expected_panels'])+'''</div></div><div class="card">완료 후 대기<div class="number">'''+str(report.get('completion_reported_panels',0))+'''</div></div><div class="card">관측 성공<div class="number">'''+str(report['valid_panels'])+'/'+str(report['expected_panels'])+'''</div></div><div class="card">관측 주기<div class="number">'''+str(report['interval_seconds'])+'''초</div></div><div class="card">한 회 관측<div class="number">'''+str(report['scan_duration_seconds'])+'''초</div></div><div class="card">누적 회차<div class="number">'''+str(report['sequence'])+'''</div></div></div><table><thead><tr><th>패널</th><th>관측 상태</th><th>현재 작업</th><th>화면 무변화</th><th>응답 시간</th><th>근거</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table><p class="note">완료 보고는 대화에 표시된 주장입니다. GitHub 영수증·커밋 또는 운영 DB 반영을 확인한 판정이 아닙니다.<br>화면 무변화 시간은 관측 시작 이후의 값이며, 작업 정지나 서버 작업 시간을 뜻하지 않습니다.</p><details><summary>기존 작업 제어 상태 · 읽기만 수행</summary><ul>'''+controls+'''</ul></details><small>'''+esc(VERSION)+''' · latest.json / health.json / events · 읽기 전용 상태 계약</small></main><script>
const at='''+str(report['observed_at'])+''';function fresh(){const age=Math.max(0,Date.now()/1000-at);const e=document.getElementById('fresh');e.className=age>30?'stale':'sub';e.textContent=age>30?'관측 갱신 중단 · '+Math.floor(age)+'초 전 자료':'최근 관측 '+new Date(at*1000).toLocaleString('ko-KR')+' · '+Math.floor(age)+'초 전';}fresh();setInterval(fresh,1000);</script></html>'''


def lock_file(path):
    stream = open(path, 'a+b')
    stream.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def process_running(run):
    probe = lock_file(run/'observer.lock')
    if probe is None:
        return True
    probe.close()
    return False


def ensure_started(root, resume=False):
    """A scheduler may restore a crashed observer, never override an explicit STOP."""
    run = root/'run'
    run.mkdir(parents=True, exist_ok=True)
    if (run/'STOP').exists() and not resume:
        return {'stopped_by_control': True, 'started': False}
    if process_running(run):
        return {'already_running': True}
    if resume:
        (run/'STOP').unlink(missing_ok=True)
    executable = Path(sys.executable)
    if os.name == 'nt' and executable.with_name('pythonw.exe').is_file():
        executable = executable.with_name('pythonw.exe')
    flags = subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0
    with (run/'process.log').open('ab') as log:
        p = subprocess.Popen([str(executable),str(root/'observer.py'),'run','--root',str(root)],cwd=root,
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags,start_new_session=os.name!='nt')
    result = {'started_pid':p.pid,'root':str(root),'at':time.time(),'reason':'EXPLICIT_START' if resume else 'MISSING_PROCESS'}
    atomic_json(run/'last_start.json',result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['once','run','start','ensure','stop','status'], default='status', nargs='?')
    parser.add_argument('--root', default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve(); run = root/'run'; run.mkdir(parents=True, exist_ok=True)
    cfg = read_json(root/'config.json')
    if not cfg or cfg.get('mode') != 'PASSIVE':
        raise SystemExit('PASSIVE_CONFIG_REQUIRED')
    cfg['interval_seconds'] = max(10, min(60, int(cfg.get('interval_seconds',15))))
    cfg['concurrency'] = max(1, min(3,int(cfg.get('concurrency',3))))
    cfg['timeout_seconds'] = max(2, min(10,int(cfg.get('timeout_seconds',7))))
    if args.action == 'stop':
        atomic_text(run/'STOP','USER_STOP '+str(time.time()))
        print(json.dumps({'stop_requested':True})); return
    if args.action == 'status':
        data = read_json(run/'health.json', {})
        data['age_seconds'] = round(time.time()-data.get('observed_at',0),1)
        data['process_running'] = process_running(run)
        data['fresh'] = data['age_seconds'] <= max(30,cfg['interval_seconds']*3) and data['process_running'] and not (run/'STOP').exists()
        if not data['fresh']:
            data['health'] = 'STALE_OR_STOPPED'
        print(json.dumps(data,ensure_ascii=True)); return
    if args.action in {'start','ensure'}:
        print(json.dumps(ensure_started(root,resume=args.action=='start'),ensure_ascii=True)); return
    lock = lock_file(run/'observer.lock')
    if lock is None:
        print(json.dumps({'already_running':True})); return
    boot = uuid.uuid4().hex
    sequence = 0
    try:
        while not (run/'STOP').exists():
            sequence += 1; started = time.monotonic()
            try:
                data = cycle(cfg,run,boot,sequence)
                atomic_json(run/'process.json',{'pid':os.getpid(),'boot':boot,'heartbeat_at':time.time(),'version':VERSION})
                if args.action=='once':
                    print(json.dumps({k:v for k,v in data.items() if k not in {'panels','workflow_controls'}},ensure_ascii=True)); break
            except Exception as exc:
                atomic_json(run/'last_error.json',{'at':time.time(),'error':str(exc)[:500]})
                if args.action=='once': raise
            while time.monotonic()-started < cfg['interval_seconds']:
                if (run/'STOP').exists(): return
                time.sleep(.5)
    finally:
        lock.close()


if __name__ == '__main__':
    main()

