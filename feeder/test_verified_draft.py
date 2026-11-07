import json
import os
import shutil
import subprocess
import unittest
import verified_draft as v
import event_supplier as s

class VerifiedDraftTests(unittest.TestCase):
    def test_wrong_or_ambiguous_webview_is_rejected(self):
        event={'conversation_id':'abc'}
        good={'type':'page','url':'https://chatgpt.com/c/abc','webSocketDebuggerUrl':'ws://127.0.0.1:32723/devtools/page/x'}
        self.assertEqual(v.select_target([good],event)[0],good)
        for targets in [[],[good,good],[{**good,'webSocketDebuggerUrl':'ws://remote:32723/devtools/page/x'}]]:
            with self.assertRaises(s.SupplyError):v.select_target(targets,event)

    def test_visible_dom_full_draft_and_controls_guard_single_click(self):
        node=shutil.which('node')
        if not node:self.skipTest('node unavailable for DOM guard simulation')
        harness=r"""
let input=JSON.parse(process.argv[1]),clicks=0;
const element=(label,text)=>({innerText:text||'',isContentEditable:true,disabled:false,getClientRects:()=>[{}],checkVisibility:()=>true,getAttribute:k=>k==='aria-label'?label:null,scrollIntoView(){},getBoundingClientRect:()=>({x:0,y:0,width:20,height:20}),contains:()=>false,click(){clicks++}});
const editor=element('',input.draft),send=element('보내기'),buttons=[send];
if(input.busy)buttons.push(element('중지'));
if(input.approval)buttons.push(element('Allow once'));
if(input.ambiguous)buttons.push(element('보내기'));
global.location={href:input.url};
global.document={querySelectorAll(q){if(q.includes('data-message-author-role'))return input.received?[element('',input.marker)]:[];if(q==='[role=textbox]')return [editor];if(q==='button')return buttons;if(q.includes('[role=alert]'))return input.alert?[element('',input.alert)]:[];throw Error(q)},elementFromPoint:()=>input.obscured?{}:send};
let result=JSON.parse(eval(input.expression));process.stdout.write(JSON.stringify({result,clicks}));
"""
        text='[marker]\nexact entire instruction including final sentence'
        cases=[({},1),({'draft':text[:-8]},0),({'url':'https://chatgpt.com/c/other'},0),
               ({'busy':True},0),({'approval':True},0),({'alert':'permission denied'},0),
               ({'ambiguous':True},0),({'obscured':True},0),({'received':True},0)]
        for overrides,expected in cases:
            with self.subTest(overrides=overrides):
                data={'draft':text,'url':'https://chatgpt.com/c/abc','marker':'[marker]',**overrides,
                      'expression':v.JS.replace('__ARGS__',json.dumps({'url':'https://chatgpt.com/c/abc','text':text,'marker':'[marker]'}))}
                result=subprocess.run([node,'-e',harness,json.dumps(data)],capture_output=True,text=True,encoding='utf-8',timeout=10,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['clicks'],expected)
if __name__=='__main__':unittest.main()

