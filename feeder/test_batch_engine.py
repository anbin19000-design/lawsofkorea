import copy
import unittest
from unittest import mock
import batch_engine as b
import event_supplier as s
from test_event_supplier import fixture, MemoryStore


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config, _, files = fixture()
        self.config.update(observer_latest='report', observer_health='health')
        self.config['workers'].append({'worker':'C02','panel_id':'panel-02','enabled':True})
        files['status/worker-bindings.json']['workers'].append({'worker':'C02','panel_id':'panel-02','conversation_id':'conv-2','verified':True})
        self.store = MemoryStore(files)
        at = s.now().timestamp()
        self.health = {'schema':'LAO_PASSIVE_OBSERVATION_V1','pc':self.config['pc'],'mode':'PASSIVE','health':'OK','boot':'test-boot','sequence':1,'observed_at':at}
        self.report = {**self.health, 'panels':[{'panel_id':'panel-0'+str(i),'observation_ok':True,'draft_present':False,'observed_at':at,'assignment':{},'state':'IDLE','native_state':'IDLE','url':'https://chatgpt.com/c/conv-'+str(i)} for i in (1,2)]}
        self.sent=[]

    def sender(self, config, event, text):
        self.assertEqual(len(self.store.published), 1)
        self.assertEqual(len(s.active_records(self.store.refresh())),2)
        self.sent.append(event['worker'])

    def execute(self, sender=None):
        with mock.patch.object(s,'json_file',side_effect=lambda p:copy.deepcopy(self.report if p=='report' else self.health)):
            return b.execute(self.config,self.store,sender or self.sender)

    def test_all_claims_are_durable_before_parallel_delivery(self):
        result=self.execute()
        self.assertCountEqual(self.sent,['C01','C02'])
        self.assertEqual(len({r['job_id'] for r in result['results']}),2)
        self.assertTrue(all(r['sent'] for r in result['results']))
        self.assertEqual(len(self.store.published),2)

    def test_one_timeout_does_not_block_other_worker_or_resend(self):
        def sender(config,event,text):
            self.sent.append(event['worker'])
            if event['worker']=='C01':raise TimeoutError()
        result=self.execute(sender)
        by={r['worker']:r for r in result['results']}
        self.assertEqual(by['C01']['status'],'UNCERTAIN_NO_RESEND')
        self.assertTrue(by['C02']['sent'])
        self.execute(sender)
        self.assertEqual(len(self.sent),2)

    def test_one_draft_does_not_block_other_idle_slot(self):
        self.report['panels'][0]['draft_present']=True
        result=self.execute(lambda c,e,t:self.sent.append(e['worker']))
        self.assertEqual(self.sent,['C02'])
        self.assertEqual(result['results'][0]['status'],'HOLD')
