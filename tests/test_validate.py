import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.validate import ROOT, TEMPLATE, load_record, validate_record


class ReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.record = load_record(TEMPLATE)

    def errors(self):
        return validate_record(self.record, allow_example=True)

    def unit(self):
        return self.record["documents"][0]["units"][0]

    def mark_reviewed(self):
        unit = self.unit()
        unit.update(
            status="reviewed",
            reviewed_by="test-reviewer",
            reviewed_at="2026-11-07T01:00:00+09:00",
            reviewed_source_sha256=hashlib.sha256(unit["source_ko"].encode("utf-8")).hexdigest(),
        )
        self.record["review"]["checkpoint_receipt"] = "TEST_CHECKPOINT_NOT_REAL"

    def test_example_allowed_only_for_template(self):
        self.assertEqual(self.errors(), [])
        self.assertTrue(validate_record(self.record))

    def test_complete_requires_all_units_and_catalog_and_receipts(self):
        self.record["review"]["status"] = "complete"
        errors = "\n".join(self.errors())
        self.assertIn("every unit reviewed", errors)
        self.assertIn("confirmed catalog", errors)
        self.assertIn("checkpoint and finish", errors)

    def test_valid_complete_record(self):
        self.mark_reviewed()
        self.record["catalog"]["confirmed"] = True
        self.record["review"].update(status="complete", finish_receipt="TEST_FINISH_NOT_REAL")
        self.assertEqual(self.errors(), [])

    def test_source_edit_invalidates_review_hash(self):
        self.mark_reviewed()
        self.unit()["source_ko"] += " 원문 변경"
        self.assertIn("hash mismatch", "\n".join(self.errors()))

    def test_missing_checkpoint_fails(self):
        self.mark_reviewed()
        self.record["review"]["checkpoint_receipt"] = ""
        self.assertIn("checkpoint receipt", "\n".join(self.errors()))

    def test_catalog_omission_fails(self):
        self.record["catalog"]["confirmed"] = True
        self.record["catalog"]["expected_document_ids"].append("MISSING_ANNEX")
        self.assertIn("does not match", "\n".join(self.errors()))

    def test_duplicate_ids_fail(self):
        self.record["documents"].append(copy.deepcopy(self.record["documents"][0]))
        self.record["documents"][0]["units"].append(copy.deepcopy(self.unit()))
        errors = "\n".join(self.errors())
        self.assertIn("duplicate document", errors)
        self.assertIn("duplicate unit", errors)

    def test_blocked_requires_note(self):
        self.unit().update(status="blocked", note="")
        self.assertIn("blocked units require", "\n".join(self.errors()))

    def test_invalid_time_and_empty_translation_fail(self):
        self.mark_reviewed()
        self.unit().update(reviewed_at="2026-11-07T01:00:00", translation="")
        errors = "\n".join(self.errors())
        self.assertIn("timezone", errors)
        self.assertIn("must not be empty", errors)

    def test_pending_cannot_retain_review_metadata(self):
        self.mark_reviewed()
        self.unit()["status"] = "pending"
        self.assertIn("must not carry review metadata", "\n".join(self.errors()))

    def test_example_url_cannot_be_promoted_to_real_data(self):
        self.record["is_example"] = False
        self.assertIn("replace the example source", "\n".join(validate_record(self.record)))

    def test_malformed_types_return_errors(self):
        self.assertTrue(validate_record([]))
        self.record["documents"] = [None, {"units": [None]}]
        self.record["catalog"]["expected_document_ids"] = [{}]
        self.assertTrue(self.errors())

    def test_duplicate_json_keys_and_constants_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in ('{"x": 1, "x": 2}', '{"x": NaN}'):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_record(path)

    def test_cli_exits_nonzero_for_invalid_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(self.record), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "validate.py"), str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("examples cannot be stored", result.stdout)


if __name__ == "__main__":
    unittest.main()
