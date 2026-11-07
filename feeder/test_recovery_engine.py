import copy
import unittest
from unittest import mock
import batch_engine as b
import event_supplier as s
import recovery_engine as r
from test_event_supplier import fixture, MemoryStore


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config, event, files = fixture()
        self.config.update(observer_latest='report', observer_health='health')
        self.store = MemoryStore(files)
        first = s.plan(self.store.refresh(), self.config, event)
        self.store.publish(self.store.sha, first['changes'], 'claim')
        self.active = self.store.files['status/active/C01.json']
        self.active['delivery_status'] = 'SENT_VERIFIED'
        at = s.now().timestamp()
        self.row = {'panel_id':'panel-01','url':'https://chatgpt.com/c/conv-1',
                    'observation_ok':True,'draft_present':False,'observed_at':at,
                    'native_state':'IDLE','state':'STORAGE_BLOCKED','state_seconds':30,
                    'assignment':{'worker':'C01','job_id':self.active['job_id'], 'marker':self.active['marker']}}
        self.health = {'schema':'LAO_PASSIVE_OBSERVATION_V1','pc':self.config['pc'],'mode':'PASSIVE',
                       'health':'OK','boot':'boot','sequence':2,'observed_at':at}
        self.report = {**self.health,'expected_panels':1,'panels':[self.row]}
        self.sent=[]

    def sender(self,c,e,t):
        self.assertIn(r.path_for(self.active), self.store.files)
        self.assertIn('LAW-RECOVERY-V1:', t.splitlines()[0])
        self.sent.append(t)

    def execute(self,sender=None):
        with mock.patch.object(s,'json_file',side_effect=lambda p:copy.deepcopy(self.report if p=='report' else self.health)):
            return b.execute(self.config,self.store,sender or self.sender)

    def test_once_per_assignment_with_observed_working_ack(self):
        self.execute();self.execute()
        self.assertEqual(len(self.sent),1)
        record=self.store.files[r.path_for(self.active)]
        self.row.update(state='WORKING',native_state='WORKING',recovery_marker=record['recovery_marker'])
        self.execute()
        self.assertEqual(self.store.files[r.path_for(self.active)]['status'],'RECOVERY_WORKING_VERIFIED')
        self.assertEqual(self.active['delivery_status'],'SENT_VERIFIED')

    def test_ambiguous_delivery_not_replayed_after_next_cycle(self):
        def fail(c,e,t):
            self.sent.append(t);raise TimeoutError()
        self.execute(fail);self.execute(fail)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.store.files[r.path_for(self.active)]['status'],'RECOVERY_UNCERTAIN_NO_RESEND')

    def test_protected_hold_and_draft_never_resume(self):
        self.row['state']='REPORTED_HOLD';self.execute();self.assertEqual(self.sent,[])
        self.row.update(state='IDLE',draft_present=True);self.execute();self.assertEqual(self.sent,[])

    def test_single_idle_observation_not_enough(self):
        self.row['state_seconds']=0
        result=self.execute()
        self.assertEqual(result['results'][0]['status'],'WAIT_STABLE_INCOMPLETE')
        self.assertEqual(self.sent,[])

    def test_source_change_or_registry_hold_blocks_recovery(self):
        p='laws/en/'+self.active['job_id']+'.json'
        self.store.files[p]={'changed':True};self.execute();self.assertEqual(self.sent,[])
        self.setUp();self.store.files['holds/'+self.active['job_id']+'.json']={'status':'NO_RETRY'}
        self.execute();self.assertEqual(self.sent,[])

    def test_degraded_other_panel_does_not_block_verified_worker(self):
        self.health['health']=self.report['health']='DEGRADED'
        self.report['expected_panels']=2
        self.report['panels'].append({'panel_id':'panel-02','observation_ok':False,'state':'UNKNOWN'})
        self.execute();self.assertEqual(len(self.sent),1)

    def test_stale_or_failed_list_blocks_all_recovery(self):
        self.health['observed_at']-=61;self.execute();self.assertEqual(self.sent,[])
        self.setUp();self.health['health']=self.report['health']='DEGRADED'
        self.report['list_error']='timeout';self.execute();self.assertEqual(self.sent,[])

    def test_manual_recovery_preserved(self):
        self.row['recovery_marker']='[OBSERVER-RECOVERY:manual]'
        self.execute();self.assertEqual(self.sent,[])

    def test_stale_recovery_ack_cannot_claim_working(self):
        self.execute();record=self.store.files[r.path_for(self.active)]
        self.row.update(recovery_marker=record['recovery_marker'],state='WORKING',native_state='WORKING')
        self.row['observed_at']-=61
        self.assertEqual(r.acknowledge(self.store.refresh(),self.active,self.row),{})


if __name__=='__main__':unittest.main()
