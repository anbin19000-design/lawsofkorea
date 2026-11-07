"""Legacy feeder tests use an isolated fixture, never the live campaign."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

class LegacyGuardTests(unittest.TestCase):
    def test_disabled_fixture_validates_and_never_dispatches(self):
        script=Path(__file__).with_name('github_instruction_feeder.py')
        if not script.exists():self.skipTest('legacy entrypoint is not part of this source bundle')
        spec=importlib.util.spec_from_file_location('isolated_legacy_feeder',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            cfg={'repo_root':str(root),'repository':'fixture/repo','branch':'main',
                 'enabled':False,'mode':'PREPARE_ONLY','source_manifest':'status/jobs.jsonl',
                 'korean_dir':'laws/ko','chinese_dir':'laws/zh','english_index_dir':'laws/en',
                 'review_dir':'review/zh','receipt_dir':'receipts','active_dir':'status/active',
                 'claim_dir':'status/claims','delivery_dir':'status/delivery',
                 'workers':[{'worker':'C%02d'%i,'panel_id':'panel-%02d'%i,'enabled':True} for i in range(1,7)],
                 'scheduler':{'heavy_threshold_rows':3000,'max_heavy_active':1}}
            jobs=[]
            for i in range(1,7):
                jid='F-'+str(i)*20
                jobs.append({'job_id':jid,'title':'Fixture','rows':2,'documents':1})
                for lang in ['ko','zh','en']:
                    p=root/'laws'/lang/(jid+'.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
            (root/'status').mkdir()
            (root/cfg['source_manifest']).write_text('\n'.join(json.dumps(j) for j in jobs))
            before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            report=module.validate(cfg)
            self.assertTrue(report['valid'])
            self.assertFalse(report['enabled'])
            self.assertEqual(report['manifest_jobs'],6)
            self.assertTrue(report['dispatch_guard_locked'])
            preview=module.preview(cfg,6)
            self.assertFalse(preview['mutated']);self.assertFalse(preview['supplied'])
            self.assertEqual(len(preview['assignments']),6)
            with mock.patch.object(module,'transport',side_effect=AssertionError('LIVE TRANSPORT MUST NOT RUN')):
                dispatch=getattr(module,'run_once',getattr(module,'dispatch_once',None))
                with self.assertRaisesRegex(RuntimeError,'FEEDER_DISABLED'):dispatch(cfg,'')
            after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
if __name__=='__main__':unittest.main()

