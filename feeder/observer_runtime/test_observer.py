import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import observer as o


def obs(**kw):
    return dict(success=True, readyState='complete', status='IDLE', url='https://chatgpt.com/c/a',
                elements=[], lastUser='', lastAssistant='', assistantCount=1, **kw)


class Tests(unittest.TestCase):
    def test_work_mode_collapsed_lines_keep_assignment_fields(self):
        x='[LAW-SUPPLY-V2:S2-x:N06:F-84c73b537cc9c65fe56e]저장소: owner/repo브랜치: law-review-notex원문 기준 커밋: abc법령가족: 국회사무처법Job ID: F-84c73b537cc9c65fe56e전체 문서: 1개'
        a=o.assignment(x)
        self.assertEqual(a['repository'],'owner/repo')
        self.assertEqual(a['branch'],'law-review-notex')
        self.assertEqual(a['family_title'],'국회사무처법')
        self.assertEqual(a['job_id'],'F-84c73b537cc9c65fe56e')

    def test_conversation_error_action_is_not_ordinary_new_chat(self):
        x=obs();x['elements']=[{'tag':'button','ariaLabel':'새 채팅','disabled':False}]
        self.assertEqual(o.classify(x)[0],'IDLE')
        x['elements'].append({'tag':'button','text':'새 대화 시작','disabled':False})
        self.assertEqual(o.classify(x)[0],'CONVERSATION_RECOVERY_REQUIRED')
        x['status']='WORKING'
        self.assertEqual(o.classify(x)[0],'WORKING')

    def test_transport_failure_never_looks_ready(self):
        row=o.summarize({'panelId':'panel-01','url':'https://chatgpt.com/c/a'},obs(transportError='disconnected'),None,100)
        self.assertEqual(row['state'],'TRANSPORT_ERROR')
        self.assertFalse(row['observation_ok'])

    def test_new_assignment_resets_idle_timer(self):
        x=obs();p={'panelId':'panel-01','url':x['url']}
        x['lastUser']='[LAW-SUPPLY-V2:S2-a:C01:F-aaaaaaaaaaaaaaaaaaaa]'
        a=o.summarize(p,x,None,100)
        x['lastUser']='[LAW-SUPPLY-V2:S2-b:C01:F-bbbbbbbbbbbbbbbbbbbb]'
        result=o.summarize(p,x,a,130)
        self.assertEqual(result['state_seconds'],0)

    def test_read_only_command_enforced_before_network(self):
        for name in ['panel.chat.send','panel.navigate','panel.click','Runtime.evaluate']:
            with self.assertRaises(ValueError): o.command('http://127.0.0.1:32722',name)

    def test_remote_endpoint_rejected(self):
        with self.assertRaises(ValueError): o.command('http://remote:32722','panel.list')

    def test_no_observation_is_not_idle(self):
        self.assertEqual(o.classify({})[0],'UNKNOWN')

    def test_draft_and_live_approval(self):
        x=obs();x['elements']=[{'role':'textbox','text':'manual draft'}]
        self.assertEqual(o.classify(x)[0],'DRAFT_PRESENT')
        x['elements']+=[{'tag':'button','text':'Allow once','disabled':False}]
        self.assertEqual(o.classify(x)[0],'APPROVAL_REQUIRED')

    def test_truncated_elements_cannot_hide_unsent_draft(self):
        x=obs();x['evidence']=['composer-enabled','conversation-url','send-ready']
        self.assertEqual(o.classify(x)[0],'DRAFT_PRESENT')
        row=o.summarize({'panelId':'panel-01','url':x['url']},x,None,100)
        self.assertTrue(row['draft_present'])

    def test_complete_is_unverified_claim(self):
        x=obs();x['lastUser']='[WORKC-WHOLE-LAW-FAMILY:WF-a:C01:F-21030edf0805f490c297]'
        x['lastAssistant']='C01 F-21030edf0805f490c297 WHOLE FAMILY COMPLETE'
        state, evidence=o.classify(x)
        self.assertEqual(state,'COMPLETION_REPORTED')
        self.assertIn('ASSISTANT_CLAIM_NOT_RECEIPT_VERIFICATION',evidence)

    def test_new_assignment_cannot_reuse_old_completion(self):
        x=obs();x['lastUser']='[WORKC-WHOLE-LAW-FAMILY:WF-a:C01:F-21030edf0805f490c297]'
        x['lastAssistant']='F-aaaaaaaaaaaaaaaaaaaa WHOLE FAMILY COMPLETE'
        self.assertEqual(o.classify(x),('IDLE',['PREVIOUS_COMPLETION_NOT_CURRENT_ASSIGNMENT']))

    def test_live_korean_complete_wording(self):
        x=obs();x['lastUser']='법령가족: 해양레저관광진흥법'
        x['lastAssistant']='완료했습니다. 법령가족: 해양레저관광진흥법 상태: COMPLETE'
        self.assertEqual(o.classify(x)[0],'COMPLETION_REPORTED')

    def test_working_does_not_use_old_error_text(self):
        x=obs();x['status']='WORKING';x['lastAssistant']='blocked by safety checks'
        self.assertEqual(o.classify(x)[0],'WORKING')

    def test_live_worker_and_family_completion_variants(self):
        x=obs();x['lastUser']='[LAW-SUPPLY-V2:S2-a:N03:F-1325ea37a1ec070d3c86]\n법령가족: 화염병 사용 등의 처벌에 관한 법률'
        for text in ['N03 COMPLETE · 커밋 및 재조회 검증 성공', '법령가족 전체 완료.', '완료 영수증 JSON — COMPLETE']:
            x['lastAssistant']='Job ID: F-1325ea37a1ec070d3c86 '+text
            self.assertEqual(o.classify(x)[0],'COMPLETION_REPORTED')
        x['lastAssistant']='F-aaaaaaaaaaaaaaaaaaaa N03 COMPLETE'
        self.assertEqual(o.classify(x)[0],'IDLE')

    def test_scheduler_preserves_explicit_stop(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(o.subprocess,'Popen') as start:
            root=Path(folder);(root/'run').mkdir();(root/'run'/'STOP').write_text('USER_STOP')
            self.assertTrue(o.ensure_started(root)['stopped_by_control'])
            start.assert_not_called()

    def test_current_storage_failure_is_not_idle_or_complete(self):
        x=obs();x['lastUser']='법령가족: 혼인신고특례법'
        x['lastAssistant']='C06 — 혼인신고특례법 전체 검수 완료. 최종 상태: HOLD — GitHub main 반영 실패. COMPLETE로 보고하지 않습니다.'
        self.assertEqual(o.classify(x)[0],'STORAGE_BLOCKED')

    def test_old_storage_failure_does_not_block_new_job(self):
        x=obs();x['lastUser']='Job ID: F-aaaaaaaaaaaaaaaaaaaa'
        x['lastAssistant']='F-bbbbbbbbbbbbbbbbbbbb HOLD GitHub 브랜치 반영 실패'
        self.assertEqual(o.classify(x)[0],'IDLE')

    def test_permission_hold_remains_protected_before_storage_error(self):
        x=obs();x['lastUser']='법령가족: 혼인신고특례법'
        x['lastAssistant']='혼인신고특례법 HOLD GitHub 저장 실패, 권한 거절'
        self.assertEqual(o.classify(x)[0],'REPORTED_HOLD')

    def test_same_assignment_followup_preserves_scope(self):
        p={'panelId':'panel-06','url':'https://chatgpt.com/c/a'}
        x=obs();x['lastUser']='[LAW-SUPPLY-V2:S2-a:C06:F-aaaaaaaaaaaaaaaaaaaa]\n법령가족: 혼인신고특례법'
        a=o.summarize(p,x,None,100)
        x['lastUser']='[LAW-SUPPLY-V2:S2-a:C06:F-aaaaaaaaaaaaaaaaaaaa]\n저장 오류 원인만 확인하십시오.'
        x['lastAssistant']='혼인신고특례법 HOLD GitHub main 반영 실패'
        b=o.summarize(p,x,a,110)
        self.assertEqual(b['state'],'STORAGE_BLOCKED')
        self.assertEqual(b['assignment']['family_title'],'혼인신고특례법')

    def test_recovery_success_with_negated_permission_report(self):
        x=obs();x['lastUser']='법령가족: 검증법'
        x['lastAssistant']='검증법 복구 완료. 기존 실패 원인은 권한·안전 거절이 아닌 GithubGraphQLAPIError / UNKNOWN입니다. 최종 상태 COMPLETE. HOLD에서 COMPLETE로 보고합니다.'
        self.assertEqual(o.classify(x)[0],'COMPLETION_REPORTED')

    def test_explicit_protected_hold_is_not_generic_storage_recovery(self):
        x=obs();x['lastUser']='법령가족: 검증법'
        x['lastAssistant']='검증법 SOURCE_HOLD. GitHub 저장 실패'
        self.assertEqual(o.classify(x)[0],'REPORTED_HOLD')

    def test_c06_graphql_failure_without_denial_can_be_diagnosed(self):
        x=obs();x['lastUser']='Job ID: F-c922697fcf8366444a46'
        x['lastAssistant']='Job ID: F-c922697fcf8366444a46 최종 상태: HOLD — GitHub 브랜치 갱신 실패. GithubGraphQLAPIError / UNKNOWN. 명시적인 401/403 권한 거절이나 expected SHA 충돌 메시지는 없었습니다.'
        self.assertEqual(o.classify(x)[0],'STORAGE_BLOCKED')

    def test_process_lock_is_actual_liveness_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.assertFalse(o.process_running(root))
            lock=o.lock_file(root/'observer.lock')
            try:
                self.assertTrue(o.process_running(root))
            finally:
                lock.close()
            self.assertFalse(o.process_running(root))

    def test_navigation_race_detected(self):
        row=o.summarize({'panelId':'panel-01','url':'https://chatgpt.com/c/other'},obs(),None,100)
        self.assertEqual(row['state'],'CONVERSATION_CHANGED')
        self.assertFalse(row['observation_ok'])

    def test_gap_resets_continuity(self):
        p={'panelId':'panel-01','url':'https://chatgpt.com/c/a'}
        a=o.summarize(p,obs(),None,100)
        b=o.summarize(p,obs(),a,115)
        c=o.summarize(p,obs(),b,300)
        self.assertEqual(b['unchanged_seconds'],15)
        self.assertEqual(c['unchanged_seconds'],0)

    def test_assignment_uses_marker_not_panel_number(self):
        a=o.assignment('[WORKC-WHOLE-LAW-FAMILY:WF-abc:C01:F-21030edf0805f490c297]\n법령가족: 극지활동 진흥법')
        self.assertEqual(a['worker'],'C01')
        self.assertEqual(a['job_id'],'F-21030edf0805f490c297')

    def test_unavailable_expected_panel_is_visible(self):
        cfg={'pc':'TEST','endpoint':'http://127.0.0.1:32722','interval_seconds':15,'concurrency':3,
             'timeout_seconds':2,'expected_panels':['panel-01','panel-02']}
        with tempfile.TemporaryDirectory() as folder,patch.object(o,'command',return_value={'panels':[]}):
            r=o.cycle(cfg,Path(folder),'boot',1)
        self.assertEqual(r['health'],'DEGRADED')
        self.assertEqual(r['counts'],{'UNAVAILABLE':2})

    def test_list_failure_not_healthy(self):
        cfg={'pc':'TEST','endpoint':'http://127.0.0.1:32722','interval_seconds':15,'concurrency':3,
             'timeout_seconds':2,'expected_panels':['panel-01']}
        with tempfile.TemporaryDirectory() as folder,patch.object(o,'command',side_effect=TimeoutError('timeout')):
            r=o.cycle(cfg,Path(folder),'boot',1)
        self.assertEqual(r['counts'],{'DISCONNECTED':1})
        self.assertEqual(r['valid_panels'],0)

    def test_dashboard_escapes_html_and_stale_timer(self):
        report={'pc':'<script>','valid_panels':0,'expected_panels':6,'interval_seconds':15,
                'scan_duration_seconds':1,'sequence':1,'observed_at':1,'panels':[]}
        doc=o.render(report)
        self.assertIn('&lt;script&gt;',doc)
        self.assertIn('관측 갱신 중단',doc)


if __name__=='__main__': unittest.main()

