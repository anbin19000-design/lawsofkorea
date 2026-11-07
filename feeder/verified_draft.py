"""Complete an already typed supplier draft using current visible DOM evidence."""
import json
from urllib.request import urlopen
from urllib.parse import urlparse
import event_supplier as s

JS = r"""(()=>{const a=__ARGS__;
const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
const visible=e=>e.getClientRects().length&&(!e.checkVisibility||e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
if(location.href!==a.url)return JSON.stringify({error:'CONVERSATION_CHANGED'});
const users=[...document.querySelectorAll('[data-message-author-role="user"],[data-user-message-bubble],.bg-user-message')].filter(visible);
if((users.at(-1)?.innerText||'').startsWith(a.marker))return JSON.stringify({already_received:true});
const editors=[...document.querySelectorAll('[role=textbox]')].filter(e=>visible(e)&&e.isContentEditable);
if(editors.length!==1||norm(editors[0].innerText)!==norm(a.text))return JSON.stringify({error:'FULL_DRAFT_MISMATCH'});
const buttons=[...document.querySelectorAll('button')].filter(e=>visible(e)&&!e.disabled);
const label=e=>(e.getAttribute('aria-label')||e.innerText||'').trim();
if(buttons.some(e=>/^(중지|응답 중지|stop|stop generating|stop streaming|Allow once|Allow always|Approve|Confirm|허용|승인|권한 허용|로그인|Log in|Sign in|새 대화 시작|Start a new chat)$/i.test(label(e))))
 return JSON.stringify({error:'LIVE_CONTROL_BLOCKS_SEND'});
const alerts=[...document.querySelectorAll('[role=alert],.text-chatgpt-recovery')].filter(visible).map(e=>e.innerText||'').join('\n');
if(/denied|forbidden|unauthorized|permission|approval|rate.?limit|usage.?limit|한도|거절|권한|승인|Unknown error|NO_RETRY|SAFETY_HOLD/i.test(alerts))
 return JSON.stringify({error:'LIVE_ALERT_BLOCKS_SEND'});
const send=buttons.filter(e=>/^(보내기|Send|Send prompt|Send message)$/i.test(label(e)));
if(send.length!==1)return JSON.stringify({error:'SEND_BUTTON_NOT_UNIQUE'});
const button=send[0];button.scrollIntoView({block:'nearest'});
const rect=button.getBoundingClientRect(),hit=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
if(!hit||!(hit===button||button.contains(hit)))return JSON.stringify({error:'SEND_BUTTON_OBSCURED'});
button.click();return JSON.stringify({clicked:true,label:label(button)});
})()"""

def evaluate(target, expression):
    import websocket
    ws=websocket.create_connection(target['webSocketDebuggerUrl'],timeout=5,suppress_origin=True)
    try:
        ws.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True}}))
        while True:
            result=json.loads(ws.recv())
            if result.get('id')==1:break
    finally:ws.close()
    if result.get('error') or result.get('result',{}).get('exceptionDetails'):
        raise s.SupplyError('VISIBLE_DRAFT_CHECK_FAILED')
    return json.loads(result['result']['result']['value'])

def select_target(targets, event):
    url='https://chatgpt.com/c/'+event['conversation_id']
    candidates=[t for t in targets if t.get('type')=='page' and t.get('url')==url]
    s.check(len(candidates)==1,'DRAFT_TARGET_NOT_UNIQUE')
    target=candidates[0];parsed=urlparse(target.get('webSocketDebuggerUrl',''))
    s.check(parsed.scheme=='ws' and parsed.hostname in ('127.0.0.1','localhost') and parsed.port==32723,'UNREGISTERED_DRAFT_TARGET')
    return target,url

def finish_own_draft(config,event,text):
    s.require_unstopped(config)
    with urlopen('http://127.0.0.1:32723/json/list',timeout=5) as stream:targets=json.load(stream)
    target,url=select_target(targets,event)
    result=evaluate(target,JS.replace('__ARGS__',json.dumps({'url':url,'text':text,'marker':text.splitlines()[0]},ensure_ascii=True)))
    s.check(not result.get('error'),result.get('error','DRAFT_SEND_FAILED'))
    s.check(result.get('clicked') or result.get('already_received'),'NO_VERIFIED_DRAFT_ACTION')
    return result

