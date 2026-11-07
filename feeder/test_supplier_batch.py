import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import supplier_batch as b


class BatchTests(unittest.TestCase):
    def test_second_batch_cannot_acquire_running_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            with b.batch_lock(path) as first:
                with b.batch_lock(path) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)
            with b.batch_lock(path) as after_exit:
                self.assertTrue(after_exit)

    def test_batch_dispatches_all_workers_and_saves_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'supplier-config.json').write_text('{"enabled":true}')
            result = {'results': [{'worker': 'C01', 'sent': True}], 'preview': False}
            with mock.patch.object(b.supplier, 'GitStore'), mock.patch.object(b.batch_engine, 'execute', return_value=result) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(b.main(root), 0)
                run.assert_called_once()
            saved = json.loads((root / 'run' / 'supplier-latest.json').read_text())
            self.assertEqual(saved['status'], 'COMPLETED')
            self.assertEqual(saved['interval_seconds'], 10)
            self.assertEqual(saved['results'], result['results'])
