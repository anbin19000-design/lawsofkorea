"""Replace an unusable panel conversation while retaining its Git assignment."""
import json
import re
import time
from pathlib import Path
import event_supplier as s
import panel_faults as f
import verified_draft
from supervisor import atomic

HOME = f.HOME
THREAD = re.compile(r'https://chatgpt\.com/c/([a-zA-Z0-9-]+)\Z')
BLOCKED = {'OFFLINE','RATE_LIMITED','REPORTED_HOLD','APPROVAL_REQUIRED','LOGIN_REQUIRED'}


def normalized(text):
    return ' '.join(str(text or '').split())


def journal(config, worker):
    return Path(config['handoff_run']) / ('handoff-' + worker + '.json')


def check_controls(config):
    s.require_unstopped(config)
    s.check(config.get('enabled') is True, 'SUPPLIER_DISABLED')
    for path in [Path(config['handoff_run']) / 'SERVICE_STOP', Path(config['observer_latest']).with_name('STOP')]:
        s.check(not path.exists(), 'STOP_PRESENT')


def observed(config, item, reader):
    obs = s.panel_observation(s.panel_command(config, 'panel.observe', panelId=item['panel_id']))
    s.check(obs.get('success') and (THREAD.fullmatch(obs.get('url','')) or obs.get('url') == HOME), 'PANEL_UNAVAILABLE')
    obs.update(reader.read(item, obs))
    return obs


def verify_claim(snap, config, item, old_url):
    worker = item['worker']
    active = snap.read('status/active/'+worker+'.json') or {}
    s.check(active.get('worker') == worker and active.get('panel_id') == item['panel_id']
            and active.get('status') == 'ACTIVE' and active.get('pc') == config['pc'], 'ACTIVE_ASSIGNMENT_REQUIRED')
    s.binding_matches(snap, {**item, 'conversation_id': THREAD.fullmatch(old_url)[1]}, config)
    job = s.manifest(snap, config)[active['job_id']]
    s.check(not re.search('HOLD|BLOCK|DENIED|NO_RETRY',str(job.get('prior_queue_status','')),re.I)
            and not any(p.startswith('holds/') and (active['job_id'] in p or worker in p) for p in snap.paths), 'PROTECTED_ASSIGNMENT')
    docs, _, blobs = s.source(snap, job)
    s.check(blobs == active.get('source_blobs'), 'SOURCE_CHANGED')
    return active, job, docs


def instruction(config, active, original, done, marker):
    prefix = active['marker']+' '+marker+'\n'
    if done:
        return (prefix + '저장소: '+config['repository']+'\n브랜치: '+config['branch']+'\n법령가족: '+active['title']+
                '\nJob ID: '+active['job_id']+'\n이전 대화에서 전체 검수 결과와 완료 영수증을 검증했습니다. 같은 법령을 다시 검수하지 마십시오. '+
                active['worker']+' READY라고 답한 뒤 공급기의 다음 배정을 받으십시오.')
    return (prefix + original.partition('\n')[2]+'\n기존 대화에서 이 배정을 승계합니다. 공식 GitHub에서 기존 검수와 영수증을 먼저 확인하고 저장된 범위를 보존하여 미완료 부분부터 계속하십시오.')


def save(path, record, phase):
    record.update(phase=phase, updated_at=s.iso())
    atomic(path, record)

def route_reason(config, row):
    if config.get('assignment_mode') != 'Chat':
        return f.reason(row)
    if not row.get('observation_ok') or not row.get('dom_verified') or row.get('draft_present'):
        return None
    ready = row.get('state') in ('READY','IDLE','COMPLETION_REPORTED')
    if row.get('url') == HOME and ready and row.get('mode') == 'Chat':
        return 'USE_OPEN_CHAT'
    if config.get('use_open_panel_only'):
        return None
    if ready and row.get('mode') == 'Work':
        return 'NEXT_IN_CHAT'
    return f.reason(row)


def finish_draining(snapshot,config):
    updates = {}
    paths = [p for p in snapshot.paths if p.startswith('status/draining/') and p.endswith('.json')]
    if not paths:
        return updates
    jobs = s.manifest(snapshot,config)
    for path in paths:
        old = snapshot.read(path)
        try:
            done = s.completed(snapshot,jobs[old['job_id']],old)
        except s.SupplyError as exc:
            if old.get('validation_error') != str(exc):
                updates[path] = {**old,'validation_error':str(exc)}
            continue
        if done:
            old.update(status='COMPLETE',completed_at=s.iso())
            aid=old['assignment_id']
            delivery='status/delivery/'+aid+'.json'
            updates.update({path:None,'status/completed/'+aid+'.json':old,'status/claims/'+aid+'.json':old,
                            delivery:{**snapshot.read(delivery,{}),'state':'COMPLETED','completed_at':old['completed_at']}})
    return updates


def begin(config, store, item, row, obs, path):
    old = row['url']; reason = route_reason(config,row)
    s.check(old == obs['url'] and reason, 'OBSERVER_CHANGED')
    snap = store.refresh()
    adopt = reason=='USE_OPEN_CHAT'
    if adopt:
        s.check(old==HOME and not obs.get('lastUser') and not obs.get('lastAssistant') and
                (obs.get('worker_model') or {}).get('mode')=='Chat','OPEN_CHAT_NOT_EMPTY')
        bindings = snap.read(config['bindings_path'])
        old=next(v['url'] for v in bindings['workers'] if v['worker']==item['worker'])
    s.check(THREAD.fullmatch(old),'BOUND_URL_REQUIRED')
    detected = f.fault(obs)
    if reason in ('PERSISTENT_IDLE','USE_OPEN_CHAT','NEXT_IN_CHAT'):
        s.check(detected is None and obs.get('status') in ('IDLE','READY') and
                not obs.get('dom_stop_visible') and not obs.get('transportError'), 'IDLE_RESOLVED')
    else:
        s.check(reason == detected, 'FAULT_CHANGED')
    s.check(not normalized(obs.get('composer_text')) and not obs.get('dom_stop_visible'), 'PANEL_IN_USE')
    active, job, docs = verify_claim(snap, config, item, old)
    unavailable = (reason == 'CONVERSATION_UNAVAILABLE' and not obs.get('lastUser') and
                   active.get('delivery_status') == 'SENT_VERIFIED' and
                   active.get('conversation_hash') == s.digest(THREAD.fullmatch(old)[1]))
    s.check(adopt or unavailable or ((row.get('assignment') or {}).get('job_id') == active['job_id'] and
            (obs.get('lastUser') or '').startswith(active['marker'])), 'VISIBLE_ASSIGNMENT_MISMATCH')
    original = s.prompt(config, job, active, docs, active['source_commit'])
    key = s.digest(active['assignment_id']+' '+old)[:20]
    marker = '[LAO-HANDOFF:'+key+']'
    done = s.completed(snap, job, active)
    if reason=='NEXT_IN_CHAT':
        s.check(done,'WAIT_PREVIOUS_WORK')
    prompt = instruction(config,active,original,done,marker)
    if adopt and not done:
        prompt=(active['marker']+' '+marker+'\n저장소: '+config['repository']+'\n브랜치: '+config['branch']+
                '\n기존 워커가 이전 법령을 계속 검수합니다. 이 Chat 패널은 '+active['worker']+
                '의 후속 배정을 받습니다. 이전 법령을 시작하지 말고 '+active['worker']+' READY라고 답하십시오.')
    remote = 'status/delivery/auto-handoff-'+key+'.json'
    record = dict(schema='LAW_AUTO_HANDOFF_V1', worker=item['worker'], panel_id=item['panel_id'],
                  old_url=old, reason=reason, target=obs['dom_target_id'], assignment_id=active['assignment_id'],
                  job_id=active['job_id'], marker=marker, source_marker=active['marker'], prompt=prompt,
                  original=original, remote=remote, completed=done, created_at=s.iso())
    # A subsequent failure may copy a prior continuation rather than the initial assignment.
    previous = obs.get('lastUser') or ''
    known = [snap.read('status/delivery/recovery-'+active['assignment_id']+'.json') or {}]
    previous_marker = re.search(r'\[LAO-HANDOFF:[0-9a-f]{20}\]', previous)
    if previous_marker:
        archive = Path(config['handoff_run']) / ('finished-'+s.digest(previous_marker[0])[:20]+'.json')
        if archive.exists():
            known.append(s.json_file(archive))
    if any((v.get('prompt') and normalized(previous)==normalized(v['prompt'])) or
           (v.get('prompt_sha256') in {s.digest(previous),s.digest(previous.rstrip()+'\n')}) for v in known):
        record['carried'] = previous
    receipt = {k:v for k,v in record.items() if k not in ('prompt','original','target','carried')}
    receipt.update(status='INTENT',prompt_sha256=s.digest(prompt))
    prior = snap.read(remote)
    if prior:
        s.check(prior.get('status')=='INTENT' and prior['prompt_sha256']==receipt['prompt_sha256'], 'EXISTING_HANDOFF_CHANGED')
    else:
        store.publish(snap.sha,{remote:receipt},'Record worker handoff intent '+item['worker'])
    save(path,record,'HOME' if adopt else 'OPEN')
    return record


def eligible_now(obs, record):
    if obs['url'] != record['old_url'] or obs.get('dom_stop_visible') or normalized(obs.get('composer_text')):
        return False
    if not (obs.get('lastUser') or '').startswith(record['source_marker']) and not (
            record['reason'] == 'CONVERSATION_UNAVAILABLE' and obs.get('conversation_unavailable') and not obs.get('lastUser')):
        return False
    state=f.fault(obs)
    return state==record['reason'] or (record['reason'] in ('PERSISTENT_IDLE','NEXT_IN_CHAT') and state is None and obs.get('status') in ('IDLE','READY'))


def new_chat_control(obs):
    controls=[e for e in obs.get('recovery_controls',[]) if not e.get('disabled') and e.get('elementId')]
    for pattern in (f.ACTION,f.NEW_CHAT):
        matches=[e for e in controls if pattern.fullmatch(f.label(e))]
        if len(matches)==1:
            return matches[0]
    return None


def open_home(reader, item, obs, button):
    if button:
        return reader.click_visible(item,obs,button)
    # The normal sidebar often renders two New Chat buttons. Navigate the verified
    # WebView to the same new-chat destination used by KIIP in this case.
    target = reader.target_details[item['worker']]
    expr = r"""(()=>{const a=__ARGS__;const out=x=>JSON.stringify(x);
      const visible=e=>e.getClientRects().length&&(!e.checkVisibility||e.checkVisibility());
      if(location.href!==a.old)return out({error:'PAGE_CHANGED'});
      const editors=[...document.querySelectorAll('[role=textbox]')].filter(e=>visible(e)&&e.isContentEditable);
      if(editors.some(e=>e.innerText.trim()))return out({error:'DRAFT_PRESENT'});
      const labels=[...document.querySelectorAll('button')].filter(e=>visible(e)&&!e.disabled)
        .map(e=>(e.getAttribute('aria-label')||e.innerText||'').trim());
      if(labels.some(l=>/^(stop|stop generating|stop streaming|중지|응답 중지|허용|항상 허용|승인|로그인|allow once|always allow|approve|log in|sign in)$/i.test(l)))
        return out({error:'PANEL_IN_USE'});
      location.assign(a.home);return out({opened:true});
    })()"""
    result = verified_draft.evaluate(target,expr.replace('__ARGS__',json.dumps({'old':obs['url'],'home':HOME})))
    s.check(result.get('opened'),result.get('error','NAVIGATION_UNCONFIRMED'))


# Switch through the visible mode picker on a blank home page. One click per observation.
# Work appears as a button or menu option, depending on the deployed Work UI.
HOME_JS=r"""(()=>{const a=__ARGS__;
const out=x=>JSON.stringify(x),norm=s=>String(s||'').replace(/\s+/g,' ').trim();
const vis=e=>e.getClientRects().length&&(!e.checkVisibility||e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
if(location.href!==a.url)return out({error:'PAGE_CHANGED'});
if([...document.querySelectorAll('[data-message-author-role],[data-user-message-bubble]')].some(vis))return out({error:'HOME_NOT_EMPTY'});
const buttons=[...document.querySelectorAll('button,[role=tab],[role=menuitem],[role=option],[role=radio],[role=menuitemradio]')].filter(e=>vis(e)&&!e.disabled);
const name=e=>norm(e.getAttribute('aria-label')||e.innerText);
if(buttons.some(e=>/^(allow once|always allow|approve|log in|sign in|허용|항상 허용|승인|로그인)$/i.test(name(e))))return out({error:'PROTECTED_CONTROL'});
const ed=[...document.querySelectorAll('[role=textbox]')].filter(e=>vis(e)&&e.isContentEditable);
if(ed.length!==1)return out({error:'EDITOR_UNAVAILABLE'});
const editorLabel=ed[0].getAttribute('aria-label')||'';
const current=/Work/i.test(editorLabel)?'Work':/^(?:ChatGPT에게 물어보세요|Message ChatGPT|Ask (?:ChatGPT|anything)|메시지 입력|Send a message)/i.test(editorLabel)?'Chat':null;
const wanted=a.mode||'Work';
if(!current)return out({error:'EDITOR_MODE_UNKNOWN'});
if(current!==wanted){
 const choices=buttons.filter(e=>name(e)===wanted||norm(e.innerText)===wanted);
 const opt=choices.length===1?choices[0]:null;
 if(!opt)return out({error:'MODE_CONTROL_UNAVAILABLE'});
 opt.scrollIntoView({block:'nearest'});const r=opt.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
 if(!hit||!(hit===opt||opt.contains(hit)))return out({error:'MODE_CONTROL_OBSCURED'});
 opt.click();return out({mode_selected:true});
}
if(norm(ed[0].innerText)===norm(a.prompt))return out({typed:true});
if(norm(ed[0].innerText))return out({error:'OTHER_DRAFT_PRESENT'});
ed[0].focus();document.execCommand('insertText',false,a.prompt);
return out(norm(ed[0].innerText)===norm(a.prompt)?{typed:true}:{error:'INSERT_NOT_VERIFIED'});
})()"""


def bind(config, store, item, record, obs):
    new_url=obs['url']
    s.check(THREAD.fullmatch(new_url) and new_url!=record['old_url'] and
            (obs.get('lastUser') or '').startswith(record['source_marker']+' '+record['marker']), 'RECEIPT_NOT_VISIBLE')
    if config.get('assignment_mode')=='Chat':
        s.check((obs.get('worker_model') or {}).get('mode')=='Chat','CHAT_MODE_REQUIRED')
    for _ in range(3):
        snap=store.refresh()
        receipt=snap.read(record['remote'])
        if receipt.get('new_url')==new_url and receipt.get('status')=='REBOUND':
            return
        active, _, _ = verify_claim(snap,config,item,record['old_url'])
        s.check(active['assignment_id']==record['assignment_id'], 'ASSIGNMENT_CHANGED')
        bindings=snap.read(config['bindings_path'])
        binding=next(w for w in bindings['workers'] if w['worker']==item['worker'])
        cid=THREAD.fullmatch(new_url)[1]
        binding.update(conversation_id=cid,url=new_url,replaced_url=record['old_url'],verified=True,verified_at=s.iso(),
                       handoff_assignment_id=active['assignment_id'],status=obs.get('status'),title=obs.get('title'))
        bindings['updated_at']=s.iso()
        if record['reason']!='USE_OPEN_CHAT' or record['completed']:
            active.update(conversation_hash=s.digest(cid),delivery_status='SENT_VERIFIED')
        receipt.update(status='REBOUND',new_url=new_url,verified_at=s.iso(),
                       completed=record['completed'],prompt_sha256=s.digest(record['prompt']))
        changes={config['bindings_path']:bindings,'status/active/'+item['worker']+'.json':active,
                 'status/claims/'+active['assignment_id']+'.json':active,record['remote']:receipt}
        if record['reason']=='USE_OPEN_CHAT' and not record['completed']:
            old={**active,'status':'FINISHING_IN_PREVIOUS_CONVERSATION','previous_url':record['old_url']}
            changes.update({'status/active/'+item['worker']+'.json':None,
                            'status/claims/'+active['assignment_id']+'.json':old,
                            'status/draining/'+active['assignment_id']+'.json':old})
        try:
            store.publish(snap.sha,changes,'Bind replacement worker '+item['worker'],rebind=True)
            return
        except s.Conflict:
            continue
    raise s.Conflict('BINDING_CHANGED_RECHECK')


def advance(config,store,item,row,path):
    check_controls(config)
    r=f.reader()
    obs=observed(config,item,r)
    rec=s.json_file(path) if path.exists() else None
    if not rec:
        s.check(-5<=time.time()-row.get('observed_at',0)<=config.get('observer_stale_seconds',60), 'STALE_HANDOFF_OBSERVATION')
        rec=begin(config,store,item,row,obs,path)
    s.check(obs['dom_target_id']==rec['target'], 'PANEL_WEBVIEW_CHANGED')
    state=f.fault(obs)
    s.check(state not in BLOCKED, state or 'PROTECTED')
    if obs['url'] not in (rec['old_url'],HOME):
        bind(config,store,item,rec,obs)
        rec['new_url']=obs['url']
        save(path,rec,'REBOUND')
        path.replace(path.with_name('finished-'+s.digest(rec['marker'])[:20]+'.json'))
        return 'REBOUND'
    if obs['url']==rec['old_url']:
        s.check(not config.get('use_open_panel_only'),'KEEP_CURRENT_PANEL')
        if not eligible_now(obs,rec):
            healed = (state is None and (obs.get('dom_stop_visible') or
                      (obs.get('status') in ('READY','IDLE') and not normalized(obs.get('composer_text')))))
            if rec['phase'] in ('OPEN','OPENING') and healed:
                save(path,rec,'RESUMED')
                path.replace(path.with_name('resumed-'+s.digest(rec['marker'])[:18]+'.json'))
                return 'RESUMED'
            raise s.SupplyError('OLD_PANEL_CHANGED')
        s.check(rec['phase'] in ('OPEN','OPENING'), 'UNEXPECTED_OLD_CONVERSATION')
        button=new_chat_control(obs)
        # A retry while the identical old failure and URL remain visible cannot open a second conversation.
        save(path,rec,'OPENING');check_controls(config)
        open_home(r,item,obs,button)
        return 'OPENING'
    s.check(rec['phase'] in ('OPENING','HOME','TYPED','CLICK_INTENT'), 'UNEXPECTED_HOME')
    s.check(not obs.get('lastUser') and not obs.get('lastAssistant') and not obs.get('dom_stop_visible'), 'HOME_IN_USE')
    draft=normalized(obs.get('composer_text'))
    if draft and draft!=normalized(rec['prompt']):
        expected = next((v for v in (rec['original'],rec.get('carried')) if v and draft==normalized(v)),None)
        s.check(rec['phase'] in ('OPENING','HOME') and expected, 'OTHER_DRAFT_PRESERVED')
        rec['archived_draft']=obs['composer_text'];save(path,rec,'HOME')
        r.clear_known_draft(item,obs,expected)
        return 'CLEARED_CARRIED_ASSIGNMENT'
    if rec['phase']=='CLICK_INTENT':
        return 'AWAITING_DELIVERY_EVIDENCE'
    check_controls(config)
    target=r.target_details[item['worker']]
    snap=store.refresh()
    active, job, _=verify_claim(snap,config,item,rec['old_url'])
    s.check(active['assignment_id']==rec['assignment_id'],'ASSIGNMENT_CHANGED')
    if rec['reason']!='USE_OPEN_CHAT' and not rec['completed'] and s.completed(snap,job,active):
        if draft:
            r.clear_known_draft(item,obs,rec['prompt'])
        rec['completed']=True
        rec['prompt']=instruction(config,active,rec['original'],True,rec['marker'])
        save(path,rec,'HOME')
        return 'COMPLETION_VERIFIED_BEFORE_DISPATCH'
    check_controls(config)
    if rec['phase']!='TYPED' or draft!=normalized(rec['prompt']):
        save(path,rec,'HOME')
        args=json.dumps(dict(url=HOME,prompt=rec['prompt'],mode=config.get('assignment_mode','Work')),ensure_ascii=True)
        result=verified_draft.evaluate(target,HOME_JS.replace('__ARGS__',args))
        s.check(not result.get('error'),result.get('error','HOME_PREPARATION_FAILED'))
        if result.get('typed'):
            save(path,rec,'TYPED')
        return 'DRAFT_READY' if result.get('typed') else 'SELECTING_REQUIRED_MODE'
    save(path,rec,'CLICK_INTENT')
    args=json.dumps(dict(url=HOME,text=rec['prompt'],marker=rec['prompt'].splitlines()[0],
                         chatOnly=config.get('assignment_mode')=='Chat'),ensure_ascii=True)
    result=verified_draft.evaluate(target,verified_draft.JS.replace('__ARGS__',args))
    if result.get('error'):
        # Every explicit JS error is emitted before the click. Transport exceptions keep CLICK_INTENT.
        save(path,rec,'TYPED')
        raise s.SupplyError(result['error'])
    s.check(result.get('clicked') or result.get('already_received'),'CLICK_UNCERTAIN')
    return 'AWAITING_DELIVERY_EVIDENCE'


def run(config,store):
    report,health=s.read_observation(config)
    s.check(report['pc']==health['pc']==config['pc'] and report['health'] in ('OK','DEGRADED')
            and not report.get('list_error') and not any(c.get('exists') and 'STOP' in str(c.get('path','')).upper()
            for c in report.get('workflow_controls',[])), 'HANDOFF_OBSERVER_NOT_READY')
    rows={r['panel_id']:r for r in report['panels']}
    result={}
    for item in config['workers']:
        worker=item['worker'];path=journal(config,worker);row=rows.get(item['panel_id'],{})
        if not item.get('enabled') or (not path.exists() and not route_reason(config,row)):
            continue
        try:
            with store.lock():
                result[worker]=advance(config,store,item,row,path)
        except Exception as exc:
            result[worker]='WAIT: '+str(exc)[:200]
    return result
