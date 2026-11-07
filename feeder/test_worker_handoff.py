import unittest
from unittest.mock import Mock,patch
from pathlib import Path
from tempfile import TemporaryDirectory
import panel_faults as f
import worker_handoff as h
import event_supplier as s

class HandoffTests(unittest.TestCase):
    def test_current_turn_signals(self):
        pairs=[({'page_alerts':['Unknown error']},'UNKNOWN_ERROR'),
               ({'page_alerts':['이 대화의 최대 길이에 도달했습니다']},'CONTEXT_EXHAUSTED'),
               ({'page_alerts':['connection lost']},'NETWORK_ERROR'),
               ({'lastAssistant':'McpServerError: Connection reset'},'EXECUTOR_CONNECTION_ERROR'),
               ({'thought_stopped':True},'THOUGHT_STOPPED'),
               ({'lastAssistant':'JSONDecodeError'},'INPUT_ERROR'),
               ({'lastAssistant':'UnicodeEncodeError'},'ENCODING_ERROR'),
               ({'conversation_unavailable':True},'CONVERSATION_UNAVAILABLE'),
               ({'page_alerts':['Unknown error','HTTP 403']},'REPORTED_HOLD'),
               ({'page_alerts':['usage limit'],'thought_stopped':True},'RATE_LIMITED'),
               ({'lastAssistant':'UnicodeDecodeError','dom_stop_visible':True},None)]
        for case,expected in pairs:
            with self.subTest(case=case):self.assertEqual(f.fault(case),expected)

    def test_verified_idle_threshold_and_early_recovery(self):
        row=dict(observation_ok=True,dom_verified=True,state='IDLE',unchanged_seconds=119)
        self.assertIsNone(f.reason(row))
        self.assertEqual(f.reason(dict(row,unchanged_seconds=120)),'PERSISTENT_IDLE')
        self.assertIsNone(f.reason(dict(row,unchanged_seconds=300,draft_present=True)))
        self.assertIsNone(f.reason(dict(row,state='UNKNOWN_ERROR',state_seconds=80,dom_verified=False)))
        self.assertEqual(f.reason(dict(row,state='UNKNOWN_ERROR',recovery_button=True)),'UNKNOWN_ERROR')

    def test_uncertain_click_is_observed_without_replay(self):
        with TemporaryDirectory() as td:
            path=Path(td)/'C01.json'
            rec={'old_url':'https://chatgpt.com/c/old','target':'tab','phase':'CLICK_INTENT','prompt':'own'}
            h.atomic(path,rec)
            obs={'url':h.HOME,'dom_target_id':'tab','composer_text':'own'}
            with patch.object(h,'check_controls'),patch.object(f,'reader'),patch.object(h,'observed',return_value=obs),patch.object(h.verified_draft,'evaluate') as click:
                self.assertEqual(h.advance({},Mock(),{'worker':'C01'},{},path),'AWAITING_DELIVERY_EVIDENCE')
                click.assert_not_called()

    def test_completed_family_handoff_only_requests_ready(self):
        a=dict(marker='[assignment]',title='법률',job_id='F-abc',worker='C03')
        result=h.instruction({'repository':'a/b','branch':'main'},a,'[assignment]\nFULL LAW REVIEW',True,'[handoff]')
        self.assertNotIn('FULL LAW REVIEW',result)
        self.assertIn('C03 READY',result)
        with self.assertRaisesRegex(s.SupplyError,'RECEIPT_NOT_VISIBLE'):
            h.bind({},Mock(),{},dict(old_url='https://chatgpt.com/c/old',marker='[handoff]',source_marker='[assignment]'),
                   dict(url='https://chatgpt.com/c/new',lastUser='[assignment] wrong'))

    def test_unavailable_bound_conversation_can_have_no_messages(self):
        rec=dict(old_url='https://chatgpt.com/c/old',source_marker='[assignment]',reason='CONVERSATION_UNAVAILABLE')
        obs=dict(url=rec['old_url'],conversation_unavailable=True)
        self.assertTrue(h.eligible_now(obs,rec))
        self.assertFalse(h.eligible_now(dict(obs,lastUser='manual message'),rec))
        self.assertFalse(h.eligible_now(dict(obs,composer_text='unsaved'),rec))

    def test_ambiguous_sidebar_entries_use_verified_home_navigation(self):
        btn=dict(tag='button',ariaLabel='새 채팅',elementId='panel-03-e1')
        obs=dict(url='https://chatgpt.com/c/old',recovery_controls=[btn,dict(btn,elementId='panel-03-e2')])
        reader=Mock(target_details={'C03':{'id':'tab'}})
        self.assertIsNone(h.new_chat_control(obs))
        with patch.object(h.verified_draft,'evaluate',return_value={'opened':True}) as evaluate:
            h.open_home(reader,{'worker':'C03'},obs,None)
            self.assertIn(obs['url'],evaluate.call_args.args[1])
            reader.click_visible.assert_not_called()
