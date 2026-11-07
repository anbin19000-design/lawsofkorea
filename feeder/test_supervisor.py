import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import event_supplier as s
import supplier_batch as b
import supervisor as v
import test_recovery_engine as recovery_tests

class ContinuationTests(unittest.TestCase):
    setUp = recovery_tests.RecoveryTests.setUp
    execute = recovery_tests.RecoveryTests.execute
    sender = recovery_tests.RecoveryTests.sender
    def verify_first(self):
        self.row.update(assistant_count=4, assistant_excerpt='기존 저장 완료. 남은 부분을 이어갈 수 있습니다.')
        self.execute()
        record=self.store.files['status/delivery/recovery-'+self.active['assignment_id']+'.json']
        self.row.update(state='WORKING',native_state='WORKING',recovery_marker=record['recovery_marker'])
        self.execute()
        record=self.store.files['status/delivery/recovery-'+self.active['assignment_id']+'.json']
        record['created_at']=(s.now()-dt.timedelta(seconds=100)).isoformat()
        self.row.update(state='IDLE',native_state='IDLE',assistant_count=5,state_seconds=25)
        return record

    def test_new_verified_benign_pause_can_continue_once(self):
        self.verify_first(); self.execute(); self.execute()
        self.assertEqual(len(self.sent),2)
        record=self.store.files['status/delivery/recovery-'+self.active['assignment_id']+'.json']
        self.assertEqual(record['episode'],2)
        self.assertEqual(len(record['previous_episodes']),1)

    def test_no_replay_without_new_assistant_response(self):
        self.verify_first();self.row['assistant_count']=4;self.execute()
        self.assertEqual(len(self.sent),1)

    def test_error_or_permission_pause_not_replayed(self):
        for text in ['저장 오류가 발생했습니다','권한 거절','NO_RETRY']:
            self.setUp();self.verify_first();self.row['assistant_excerpt']=text;self.execute()
            self.assertEqual(len(self.sent),1)

    def test_continuation_cap(self):
        record=self.verify_first();record['episode']=3;self.execute()
        self.assertEqual(len(self.sent),1)

class SupervisorTests(unittest.TestCase):
    def test_stop_is_preserved_without_start(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(v.subprocess,'Popen') as start:
            root=Path(d);(root/'run').mkdir();(root/'run'/'SERVICE_STOP').touch()
            self.assertEqual(v.ensure_service(root,{'enabled':True})['status'],'STOPPED_BY_CONTROL')
            start.assert_not_called()

    def test_held_service_lock_never_launches_duplicate(self):
        with tempfile.TemporaryDirectory() as d,mock.patch.object(v.subprocess,'Popen') as start:
            root=Path(d);(root/'run').mkdir()
            with b.batch_lock(root/'run'/'supplier-service.lock'):
                result=v.ensure_service(root,{'enabled':True})
            self.assertEqual(result['status'],'STALE_PROCESS_REQUIRES_REVIEW')
            start.assert_not_called()

    def test_start_cooldown_prevents_racing_launches(self):
        with tempfile.TemporaryDirectory() as d,mock.patch.object(v.subprocess,'Popen') as start:
            start.return_value.pid=42;root=Path(d)
            self.assertTrue(v.ensure_service(root,{'enabled':True})['started'])
            self.assertEqual(v.ensure_service(root,{'enabled':True})['status'],'START_COOLDOWN')
            self.assertEqual(start.call_count,1)

    def test_dashboard_reports_missing_evidence_and_escapes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);v.atomic(root/'supplier-config.json',{'pc':'<X>','workers':[{'worker':'C01','panel_id':'panel-01','enabled':True}]})
            result=v.refresh_dashboard(root)
            self.assertEqual(result['rows'][0]['state'],'STALE')
            self.assertIsNone(result['observer_age_seconds'])
            self.assertIn('&lt;X&gt;',(root/'run'/'supervision.html').read_text())

    def test_snapshot_transition_reads_only_coherent_pair(self):
        config={'observer_latest':'r','observer_health':'h'}
        with mock.patch.object(s,'json_file',side_effect=[{'boot':'b','sequence':2},{'boot':'b','sequence':1},{'boot':'b','sequence':2},{'boot':'b','sequence':2}]),mock.patch.object(s.time,'sleep'):
            self.assertEqual(s.read_observation(config)[0]['sequence'],2)
        with mock.patch.object(s,'json_file',side_effect=lambda p:{'boot':'b','sequence':1 if p=='r' else 2}),mock.patch.object(s.time,'sleep'):
            with self.assertRaisesRegex(s.SupplyError,'SNAPSHOT_IN_FLIGHT'):s.read_observation(config)

    def test_failure_written_to_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'supplier-config.json').write_text('{"enabled":false}')
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(b.main(root),2)
            log=json.loads((root/'run'/'supplier-history.jsonl').read_text())
            self.assertEqual(log['error'],'SUPPLIER_DISABLED')

if __name__=='__main__':unittest.main()

