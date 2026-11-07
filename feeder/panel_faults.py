"""Read the registered KIIP WebView signals and identify recoverable Lao Panel failures."""
import re
import sys
from pathlib import Path

HOME = 'https://chatgpt.com/'
FAULTS = {'CONTEXT_EXHAUSTED', 'CONVERSATION_UNAVAILABLE', 'UNKNOWN_ERROR',
          'NETWORK_ERROR', 'EXECUTOR_CONNECTION_ERROR', 'THOUGHT_STOPPED',
          'ENCODING_ERROR', 'INPUT_ERROR'}
ACTION = re.compile(r'^(새 대화 시작|start (?:a )?new chat)$', re.I)
NEW_CHAT = re.compile(r'^(새 채팅|new chat)$', re.I)
LIMIT = re.compile(r'too many requests|rate.?limit|usage.?limit|사용량.{0,12}(?:한도|초과)|사용 한도|(?:HTTP(?: error)?|status|응답 코드)\s*[:=]?\s*429\b', re.I)
DENIED = re.compile(r'permission denied|access denied|forbidden|unauthorized|(?:HTTP|status)(?: error)?\s*[:=]?\s*40[13]\b|blocked by safety|automatic approval.{0,30}(?:rejected|denied)|(?:권한|안전 검사|승인 심사).{0,18}(?:거부|거절|차단)(?!.{0,8}(?:없|아닌|아님))|\b(?:NO_RETRY|SOURCE_HOLD|SAFETY_HOLD|RETIRED_HOLD)\b', re.I)
NETWORK = re.compile(r'연결(?:이|을)?\s*(?:해제|끊|유실)|네트워크\s*(?:오류|연결이 끊)|disconnected|connection\s+(?:lost|closed|interrupted)|network\s+error|ERR_(?:NETWORK|INTERNET|CONNECTION)', re.I)


def reader():
    support = Path(Path(__file__).anchor) / 'YOLLA_WORKSPACE/law_review18_environment_20261105/zh_fastpass_20261106/kiip_support'
    if str(support) not in sys.path:
        sys.path.insert(0, str(support))
    from panel_dom import PanelDomReader
    return PanelDomReader()


def label(element):
    return str(element.get('ariaLabel') or element.get('text') or '').strip()


def read_after_timeout(panel):
    """Trust a fresh list URL only after confirming the panel stamp in that WebView."""
    r = reader()
    item = {'panel_id': panel['panelId'], 'worker': panel['panelId']}
    dom = r.read(item, {'url': panel['url']})
    target = r.target_details[item['worker']]
    controls = r._evaluate(target, r"""JSON.stringify([...document.querySelectorAll('button[data-yolla-panel-element-id]')]
      .filter(e=>e.getClientRects().length&&(!e.checkVisibility||e.checkVisibility())&&!e.disabled)
      .map(e=>({elementId:e.getAttribute('data-yolla-panel-element-id'),tag:'button',
        ariaLabel:(e.getAttribute('aria-label')||e.innerText||'').trim().slice(0,90)})))""")
    prefix = item['panel_id']+'-e'
    if not controls or not all(str(x['elementId']).startswith(prefix) for x in controls):
        raise ValueError('DOM_PANEL_STAMP_MISMATCH')
    if not dom.get('page_loaded'):
        raise ValueError('PAGE_NOT_READY')
    state = 'WORKING' if dom['dom_stop_visible'] else 'UNKNOWN'
    return dict(dom, success=True, readyState='complete', status=state, url=panel['url'],
                elements=controls, assistantCount=dom.get('dom_assistant_count'))


def fault(obs):
    """Only the current turn and visible controls can authorize a replacement."""
    controls = [label(e) for e in obs.get('elements', []) + obs.get('recovery_controls', [])
                if e.get('tag') == 'button' and not e.get('disabled')]
    alerts = '\n'.join(obs.get('page_alerts') or [])
    reply = obs.get('lastAssistant') or ''
    if any(re.fullmatch(r'(?:Allow once|Always allow|Allow always|Approve|Confirm|한 번만? 허용|항상 허용|허용|승인|권한 허용)', l, re.I) for l in controls):
        return 'APPROVAL_REQUIRED'
    if any(re.fullmatch(r'로그인|log in|sign in', l, re.I) for l in controls):
        return 'LOGIN_REQUIRED'
    if LIMIT.search(alerts) or re.search(r'^\s*429\s*$', alerts, re.M):
        return 'RATE_LIMITED'
    if DENIED.search(alerts):
        return 'REPORTED_HOLD'
    if obs.get('dom_stop_visible'):
        return None
    if DENIED.search(reply) or re.search(r'source (?:DB revision|photograph) changed|conflicting/corrupt submission|existing result differs', reply, re.I):
        return 'REPORTED_HOLD'
    if LIMIT.search(reply):
        return 'RATE_LIMITED'
    if obs.get('page_online') is False:
        return 'OFFLINE'
    if re.search(r'maximum.{0,30}length|대화.{0,16}최대 길이', alerts, re.I):
        return 'CONTEXT_EXHAUSTED'
    if obs.get('conversation_unavailable'):
        return 'CONVERSATION_UNAVAILABLE'
    if re.search(r'unknown error|something went wrong|알 수 없는 오류|resume stream unavailable', alerts, re.I) or any(ACTION.fullmatch(l) for l in controls):
        return 'UNKNOWN_ERROR'
    if NETWORK.search(alerts) or any(NETWORK.match(l.strip()) for l in (obs.get('page_text') or '').splitlines()):
        return 'NETWORK_ERROR'
    if obs.get('status') == 'WORKING':
        return None
    if obs.get('thought_stopped'):
        return 'THOUGHT_STOPPED'
    for pattern, state in [(r'McpServerError:\s*(?:Connection (?:failed|reset|timed out)|Timed out)', 'EXECUTOR_CONNECTION_ERROR'),
                           (r'Unicode(?:En|De)codeError|surrogates not allowed', 'ENCODING_ERROR'),
                           (r'JSONDecodeError|ValueError|validation error', 'INPUT_ERROR'),
                           (r'something went wrong|unknown error|알 수 없는 오류', 'UNKNOWN_ERROR')]:
        if re.search(pattern, reply, re.I):
            return state
    return None


def reason(row):
    if not row.get('observation_ok') or row.get('draft_present') or not row.get('dom_verified'):
        return None
    state = row.get('state')
    if state in FAULTS:
        grace = 0 if state in ('CONTEXT_EXHAUSTED', 'CONVERSATION_UNAVAILABLE') or row.get('recovery_button') else 60
        if (row.get('state_seconds') or 0) >= grace:
            return state
    if state == 'IDLE' and (row.get('unchanged_seconds') or 0) >= 120:
        return 'PERSISTENT_IDLE'
    return None
