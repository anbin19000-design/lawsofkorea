"""One-shot liveness supervisor for the existing observer and supplier only.

No allocation, transmission, browser changes or forced process termination.
The scheduled task calls ensure once a minute using pythonw.exe.
"""
import argparse
import datetime as dt
import html
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default


def atomic(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=True, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def age(value, clock=None):
    clock = time.time() if clock is None else clock
    try:
        at = value if isinstance(value, (int, float)) else dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return max(0, clock-at)
    except (ValueError, TypeError, AttributeError):
        return float('inf')


def lock_held(path):
    from supplier_batch import batch_lock
    with batch_lock(path) as acquired:
        return not acquired


def controls_stopped(root, config):
    paths = [root/'run'/'SERVICE_STOP', *[Path(p) for p in config.get('stop_paths', [])]]
    return [str(p) for p in paths if p.exists()]


def ensure_service(root, config):
    run = root/'run'; run.mkdir(exist_ok=True)
    stopped = controls_stopped(root, config)
    if stopped or config.get('enabled') is not True:
        return {'status': 'STOPPED_BY_CONTROL', 'controls': stopped, 'started': False}
    if lock_held(run/'supplier-service.lock'):
        heartbeat = read(run/'supplier-service.json')
        seconds = age(heartbeat.get('heartbeat_at', heartbeat.get('started_at')))
        return {'status': 'RUNNING' if seconds <= 180 else 'STALE_PROCESS_REQUIRES_REVIEW',
                'pid': heartbeat.get('pid'), 'age_seconds': round(seconds, 1) if seconds != float('inf') else None,
                'started': False}
    prior = read(run/'last-service-start.json')
    if age(prior.get('at')) < 55:
        return {'status': 'START_COOLDOWN', 'started': False}
    executable = Path(sys.executable)
    if os.name == 'nt' and executable.with_name('pythonw.exe').is_file():
        executable = executable.with_name('pythonw.exe')
    log_path = run/'service-process.log'
    if log_path.exists() and log_path.stat().st_size > 2_000_000:
        log_path.replace(run/'service-process.previous.log')
    with log_path.open('ab') as log:
        child = subprocess.Popen([str(executable), str(root/'supplier_batch.py'), '--service'],
            cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')
    result = {'status': 'STARTED_AWAITING_HEARTBEAT', 'pid': child.pid, 'at': time.time(), 'started': True}
    atomic(run/'last-service-start.json', result)
    return result


def ensure_observer(config):
    root = Path(config['observer_latest']).parent.parent
    # Only the already registered, local observer entrypoint is loaded.
    spec = importlib.util.spec_from_file_location('lao_registered_observer', root/'observer.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    if controls_stopped(Path(__file__).resolve().parent, config):
        return {'status': 'WORKFLOW_STOP_PRESERVED', 'started': False}
    result = module.ensure_started(root, resume=False)
    health = read(root/'run'/'health.json')
    seconds = age(health.get('observed_at'))
    result['age_seconds'] = round(seconds, 1) if seconds != float('inf') else None
    if result.get('already_running') and seconds > 60:
        result['status'] = 'STALE_OBSERVER_REQUIRES_REVIEW'
    return result


def overview(root, clock=None):
    config = read(root/'supplier-config.json')
    report = read(config.get('observer_latest', root/'missing'))
    batch = read(root/'run'/'supplier-latest.json')
    service = read(root/'run'/'supplier-service.json')
    results = {r['worker']: r for r in batch.get('results', []) if r.get('worker')}
    panels = {p['panel_id']: p for p in report.get('panels', []) if p.get('panel_id')}
    rows = []
    for binding in config.get('workers', []):
        if not binding.get('enabled'): continue
        p = panels.get(binding['panel_id'], {})
        r = results.get(binding['worker'], {})
        seconds = age(p.get('observed_at'), clock)
        state = p.get('state', 'UNAVAILABLE') if seconds <= 60 else 'STALE'
        alerts = list(p.get('reasons') or [])
        progress = (batch.get('handoffs') or {}).get(binding['worker'])
        if progress:
            alerts.append(progress)
        if batch.get('handoff_error'):
            alerts.append(batch['handoff_error'])
        if state in ('IDLE', 'COMPLETION_REPORTED') and p.get('state_seconds', 0) >= 60:
            alerts.append('IDLE_NEEDS_SUPPLIER_ATTENTION')
        if r.get('status') in ('UNCERTAIN_NO_RESEND', 'RECOVERY_UNCERTAIN_NO_RESEND', 'RECOVERY_PAUSED_AGAIN_REQUIRES_REVIEW'):
            alerts.append(r['status'])
        if state in ('WORKING',) and p.get('unchanged_seconds', 0) >= 600:
            alerts.append('LONG_RUNNING_REQUIRES_PROGRESS_CHECK')
        rows.append({'worker': binding['worker'], 'panel_id': binding['panel_id'], 'state': state,
                     'job_id': (p.get('assignment') or {}).get('job_id'),
                     'family': (p.get('assignment') or {}).get('family_title'), 'url': p.get('url'),
                     'supplier_status': r.get('status', batch.get('status', 'UNKNOWN')),
                     'supplier_error': r.get('error', batch.get('error')),
                     'idle_seconds': p.get('state_seconds', 0) if state != 'WORKING' else 0,
                     'unchanged_seconds': p.get('unchanged_seconds'), 'alerts': list(dict.fromkeys(alerts))})
    return {'schema': 'LAW_CONTINUOUS_SUPERVISION_V1', 'pc': config.get('pc'), 'at': time.time(),
            'observer_version': report.get('version'), 'observer_age_seconds': age(report.get('observed_at'), clock),
            'supplier_version': batch.get('version'), 'supplier_age_seconds': age(service.get('heartbeat_at', service.get('started_at')), clock),
            'batch_status': batch.get('status'), 'batch_elapsed_seconds': batch.get('elapsed_seconds'),
            'rows': rows, 'stopped_controls': controls_stopped(root, config)}


def refresh_dashboard(root):
    data = overview(root); esc = lambda v: html.escape(str(v if v is not None else '—'))
    rows = ''.join('<tr><td>'+esc(r['worker'])+'</td><td>'+esc(r['state'])+'</td><td>'+esc(r['family'])+
                   '<small>'+esc(r['job_id'])+'</small></td><td>'+esc(r['supplier_status'])+
                   '<small>'+esc(r['supplier_error'])+'</small></td><td>'+esc(', '.join(r['alerts']))+'</td></tr>' for r in data['rows'])
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>라오패널 작업 감독</title><style>body{font:15px system-ui;background:#f1f5f9;color:#123;margin:30px}table{border-collapse:collapse;background:white;width:100%}td,th{padding:14px;border-bottom:1px solid #ddd;text-align:left}small{display:block;color:#567;font-size:12px;margin-top:5px}.alert{background:#fee;padding:12px}h1{font-size:25px}</style>
<h1>'''+esc(data['pc'])+''' · 라오패널 작업 감독</h1><p id="fresh"></p><p>관측 10초 · 공급 10초 · 프로세스 점검 1분</p>
<p>관측 버전 '''+esc(data['observer_version'])+''' · 공급기 '''+esc(data['supplier_version'])+''' · 최근 처리 '''+esc(data['batch_elapsed_seconds'])+'''초</p>
<table><tr><th>워커</th><th>화면 상태</th><th>담당 법률</th><th>공급·복구 결과</th><th>확인 필요</th></tr>'''+rows+'''</table>
<p>WORKING은 화면의 생성 중 신호입니다. 완료 보고와 GitHub 저장 검증은 구분합니다. 승인·권한·사용자 중지는 자동 해제하지 않습니다.</p>
<script>const at='''+str(data['at'])+''';function tick(){let s=Math.max(0,Date.now()/1000-at),e=document.getElementById('fresh');e.textContent='최근 갱신 '+Math.floor(s)+'초 전';e.className=s>40?'alert':'';}tick();setInterval(tick,1000)</script></html>'''
    # JSON stays finite for strict consumers even when evidence is missing.
    for k in ('observer_age_seconds', 'supplier_age_seconds'):
        if data[k] == float('inf'): data[k] = None
    atomic(root/'run'/'supervision.json', data)
    atomic(root/'run'/'supervision.html', page)
    return data


def ensure(root):
    from supplier_batch import batch_lock
    run = root/'run'; run.mkdir(exist_ok=True)
    config = read(root/'supplier-config.json')
    if config.get('pc', '').upper() != socket.gethostname().upper():
        raise ValueError('WRONG_EXECUTION_HOST')
    with batch_lock(run/'supervisor.lock') as acquired:
        if not acquired: return {'status': 'SUPERVISOR_ALREADY_RUNNING'}
        result = {'at': time.time(), 'pc': config['pc'], 'observer': ensure_observer(config),
                  'supplier': ensure_service(root, config)}
        atomic(run/'supervisor-latest.json', result)
        refresh_dashboard(root)
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['ensure', 'status', 'dashboard'])
    args = parser.parse_args(); root = Path(__file__).resolve().parent
    if args.action == 'ensure': result = ensure(root)
    else:
        result = refresh_dashboard(root)
        if args.action == 'dashboard': os.startfile(str(root/'run'/'supervision.html'))
    if sys.stdout: print(json.dumps(result, ensure_ascii=True))
