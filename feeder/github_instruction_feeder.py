#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, re, subprocess, sys, time, uuid
from pathlib import Path
HERE=Path(__file__).resolve().parent
CONFIG_PATH=HERE/'config.json'
def readj(p): return json.loads(Path(p).read_text('utf-8-sig'))
def atomic(p,o):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp');t.write_text(json.dumps(o,ensure_ascii=False,indent=2),encoding='utf-8');t.replace(p)
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def cfg():
 c=readj(CONFIG_PATH)
 if c.get('schema')!='WORKC_GITHUB_INSTRUCTION_FEEDER_V1': raise ValueError('INVALID_CONFIG_SCHEMA')
 return c
def root(c): return Path(c['repo_root'])
def manifest(c):
 p=root(c)/c['source_manifest'];out=[]
 if not p.exists(): return out
 for n,line in enumerate(p.read_text('utf-8-sig').splitlines(),1):
  if line.strip():
   x=json.loads(line);x['manifest_line']=n;out.append(x)
 return out
def paths(c,j):
 r=root(c);return {'ko':r/c['korean_dir']/(j+'.json'),'zh':r/c['chinese_dir']/(j+'.json'),'review':r/c['review_dir']/(j+'.json'),'receipt':r/c['receipt_dir']/(j+'.json')}
def completed(c,j):
 p=paths(c,j)
 if not p['review'].exists() or not p['receipt'].exists(): return False
 try:
  a=readj(p['review']);b=readj(p['receipt']);return a.get('job_id')==j and a.get('status')=='COMPLETE' and b.get('job_id')==j and b.get('status')=='COMPLETE'
 except Exception:return False
def active(c):
 out={};d=root(c)/c['active_dir']
 if d.exists():
  for p in d.glob('C??.json'):
   try:
    x=readj(p)
    if x.get('status')=='ACTIVE':out[x['worker']]=x
   except Exception:pass
 return out
def candidates(c):
 act={x.get('job_id') for x in active(c).values()};out=[]
 for x in manifest(c):
  j=x['job_id'];p=paths(c,j)
  if j in act or completed(c,j):continue
  if p['ko'].exists() and p['zh'].exists():out.append(x)
 out.sort(key=lambda x:(int(x.get('rows') or 0),int(x.get('documents') or 0),x['job_id']))
 return out
def validate(c,write=False):
 rows=manifest(c);ids=set();errors=[];pairs=done=0;english_indexes=0
 for x in rows:
  j=x.get('job_id')
  if not j or j in ids:errors.append({'job_id':j,'error':'MISSING_OR_DUPLICATE_JOB_ID'});continue
  ids.add(j);p=paths(c,j);miss=[k for k in ('ko','zh') if not p[k].exists()]
  if miss:errors.append({'job_id':j,'error':'MISSING_SOURCE_PAIR','missing':miss})
  else:pairs+=1
  ep=root(c)/c.get('english_index_dir','laws/en')/(j+'.json')
  if ep.exists():english_indexes+=1
  else:errors.append({'job_id':j,'error':'MISSING_ENGLISH_INDEX'})
  if completed(c,j):done+=1
 workers=[w['worker'] for w in c['workers'] if w.get('enabled')]
 report={'schema':'WORKC_GITHUB_FEEDER_VALIDATION_V1','at':now(),'repository':c['repository'],'branch':c['branch'],'enabled':c['enabled'],'mode':c['mode'],'manifest_jobs':len(rows),'paired_jobs':pairs,'english_index_jobs':english_indexes,'completed_jobs':done,'active_jobs':len(active(c)),'eligible_jobs':len(candidates(c)),'workers':workers,'errors':errors,'valid':bool(rows) and not errors,'dispatch_guard_locked':not(c.get('enabled') and c.get('mode')=='LIVE')}
 if write:atomic(root(c)/'status'/'feeder-validation.json',report)
 return report
def preview(c,limit=6):
 a=active(c);free=[w for w in c['workers'] if w.get('enabled') and w['worker'] not in a];cand=candidates(c);sel=[]
 heavy=int(c['scheduler']['heavy_threshold_rows']);maxh=int(c['scheduler']['max_heavy_active']);ha=sum(int(v.get('rows') or 0)>=heavy for v in a.values())
 for w in free:
  pick=None
  for x in cand:
   if int(x.get('rows') or 0)>=heavy and ha>=maxh:continue
   pick=x;break
  if not pick:break
  cand.remove(pick)
  if int(pick.get('rows') or 0)>=heavy:ha+=1
  sel.append({'worker':w['worker'],'panel_id':w['panel_id'],**{k:pick.get(k) for k in ('job_id','title','documents','rows','prior_queue_status')}})
  if len(sel)>=limit:break
 return {'schema':'WORKC_GITHUB_FEEDER_PREVIEW_V1','at':now(),'mutated':False,'supplied':False,'assignments':sel}
def git(c,*args,check=True):
 p=subprocess.run(['git','-C',str(root(c)),*args],capture_output=True,text=True,encoding='utf-8',errors='replace')
 if check and p.returncode:raise RuntimeError('git '+' '.join(args)+' failed: '+(p.stderr.strip() or p.stdout.strip()))
 return p
def sync(c):
 git(c,'fetch','origin',c['branch'])
 if git(c,'status','--porcelain').stdout.strip():raise RuntimeError('DIRTY_GITHUB_WORKTREE')
 git(c,'pull','--ff-only','origin',c['branch'])
def commit(c,msg,items):
 git(c,'add',*items)
 if not git(c,'status','--porcelain').stdout.strip():return None
 git(c,'commit','-m',msg);git(c,'push','origin',c['branch']);return git(c,'rev-parse','HEAD').stdout.strip()
def require_live(c,confirm):
 if not c.get('enabled') or c.get('mode')!='LIVE':raise RuntimeError('FEEDER_DISABLED')
 if confirm!=c.get('dispatch_confirm_token'):raise RuntimeError('DISPATCH_CONFIRMATION_REQUIRED')
 if os.environ.get('WORKC_GITHUB_FEEDER_LIVE')!='1':raise RuntimeError('LIVE_ENVIRONMENT_LOCKED')
def prompt(c,w,j,a):
 t=(HERE/'instruction_template.txt').read_text('utf-8');jid=j['job_id'];return t.format(assignment_id=a,worker=w,job_id=jid,repository=c['repository'],branch=c['branch'],title=j.get('title') or jid,ko_path=f"{c['korean_dir']}/{jid}.json",zh_path=f"{c['chinese_dir']}/{jid}.json",review_path=f"{c['review_dir']}/{jid}.json",receipt_path=f"{c['receipt_dir']}/{jid}.json")
def transport(c):
 sys.path.insert(0,c['transport']['kiip_tool_root']);from panel_watch import LocalPanelClient,classify;return LocalPanelClient(),classify
def run_once(c,confirm):
 require_live(c,confirm);sync(c);client,classify=transport(c);a=active(c);cand=candidates(c);results=[]
 heavy=int(c['scheduler']['heavy_threshold_rows']);maxh=int(c['scheduler']['max_heavy_active']);ha=sum(int(v.get('rows') or 0)>=heavy for v in a.values())
 for w in c['workers']:
  worker=w['worker']
  if not w.get('enabled') or worker in a:continue
  obs=client.command('panel.observe',panelId=w['panel_id']);obs=obs.get('observation',obs);state=classify(obs,worker,obs.get('url'))['state']
  if state not in ('IDLE','READY') or str(obs.get('composer_text') or '').strip():results.append({'worker':worker,'status':'WAIT_PANEL','observed_state':state});continue
  job=next((x for x in cand if not(int(x.get('rows') or 0)>=heavy and ha>=maxh)),None)
  if not job:results.append({'worker':worker,'status':'NO_ELIGIBLE_JOB'});continue
  cand.remove(job)
  if int(job.get('rows') or 0)>=heavy:ha+=1
  aid='A-'+uuid.uuid4().hex;claim={'schema':'WORKC_GITHUB_ASSIGNMENT_V1','assignment_id':aid,'worker':worker,'panel_id':w['panel_id'],'job_id':job['job_id'],'title':job.get('title'),'documents':job.get('documents'),'rows':job.get('rows'),'status':'ACTIVE','claimed_at':now(),'delivery_status':'PENDING'}
  ap=root(c)/c['active_dir']/(worker+'.json');cp=root(c)/c['claim_dir']/(aid+'.json');atomic(ap,claim);atomic(cp,claim);commit(c,f"Claim {job['job_id']} for {worker}",[str(ap.relative_to(root(c))),str(cp.relative_to(root(c)))])
  text=prompt(c,worker,job,aid);dp=root(c)/c['delivery_dir']/(aid+'.json');delivery={'schema':'WORKC_GITHUB_DELIVERY_V1','assignment_id':aid,'worker':worker,'job_id':job['job_id'],'prompt_sha256':hashlib.sha256(text.encode()).hexdigest(),'state':'SENDING_UNCERTAIN','at':now()};atomic(dp,delivery)
  try:delivery.update(state='SENT',sent_at=now(),result=client.command('panel.chat.send',panelId=w['panel_id'],text=text))
  except Exception as e:delivery.update(state='UNCERTAIN_NO_RESEND',error=str(e))
  atomic(dp,delivery);claim['delivery_status']=delivery['state'];atomic(ap,claim);atomic(cp,claim);commit(c,f'Record delivery {aid}',[str(ap.relative_to(root(c))),str(cp.relative_to(root(c))),str(dp.relative_to(root(c)))]);results.append({'worker':worker,'job_id':job['job_id'],'status':delivery['state']})
 return {'at':now(),'results':results}
def set_enabled(c,en,confirm):
 expected='ENABLE_WORKC_FEEDER_CONFIG' if en else 'DISABLE_WORKC_FEEDER_CONFIG'
 if confirm!=expected:raise RuntimeError('CONFIG_CONFIRMATION_REQUIRED')
 c['enabled']=en;c['mode']='LIVE' if en else 'PREPARE_ONLY';atomic(CONFIG_PATH,c);sp=root(c)/'status'/'feeder-state.json';s=readj(sp) if sp.exists() else {};s.update(enabled=en,mode=c['mode'],updated_at=now());atomic(sp,s);return {'enabled':en,'mode':c['mode']}
def main():
 ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='cmd',required=True);v=sub.add_parser('validate');v.add_argument('--write-report',action='store_true');p=sub.add_parser('preview');p.add_argument('--limit',type=int,default=6);e=sub.add_parser('enable');e.add_argument('--confirm',required=True);d=sub.add_parser('disable');d.add_argument('--confirm',required=True);o=sub.add_parser('run-once');o.add_argument('--confirm',default='');s=sub.add_parser('service');s.add_argument('--confirm',default='');a=ap.parse_args();c=cfg()
 if a.cmd=='validate':r=validate(c,a.write_report)
 elif a.cmd=='preview':r=preview(c,max(1,min(a.limit,6)))
 elif a.cmd=='enable':r=set_enabled(c,True,a.confirm)
 elif a.cmd=='disable':r=set_enabled(c,False,a.confirm)
 elif a.cmd=='run-once':r=run_once(c,a.confirm)
 else:
  require_live(c,a.confirm)
  while True:
   try:print(json.dumps(run_once(cfg(),a.confirm),ensure_ascii=False),flush=True)
   except Exception as e:print(json.dumps({'at':now(),'error':str(e)},ensure_ascii=False),flush=True)
   time.sleep(int(c['poll_seconds']))
  return
 print(json.dumps(r,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
