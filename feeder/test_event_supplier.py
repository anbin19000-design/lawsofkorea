import contextlib
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

import event_supplier as s

J1, J2 = "F-" + "1" * 20, "F-" + "2" * 20


def fixture():
    config = {"pc": socket.gethostname(), "repository": "owner/repo", "branch": "main",
              "enabled": True, "source_manifest": "status/whole-law-family-assignment.jsonl",
              "workers": [{"worker": "C01", "panel_id": "panel-01", "enabled": True}],
              "heavy_threshold_rows": 3000, "max_heavy_active": 1}
    event = {"schema": s.EVENT_SCHEMA, "event_id": "test-001", "kind": "READY", "pc": config["pc"],
             "worker": "C01", "panel_id": "panel-01", "conversation_id": "conv-1",
             "observed_at": s.iso(), "status": "IDLE", "composer_empty": True,
             "expected_assignment_id": None}
    jobs = []
    files = {"status/worker-bindings.json": {"pc": config["pc"], "repository": config["repository"],
             "workers": [{"worker": "C01", "panel_id": "panel-01", "conversation_id": "conv-1", "verified": True}]}}
    for jid in (J1, J2):
        jobs.append({"job_id": jid, "family_id": jid, "title": "검증법", "rows": 2, "documents": 2,
                     "assignment_unit": "WHOLE_LAW_FAMILY", "includes_all_linked_documents": True,
                     "prior_queue_status": "QUEUED"})
        files["laws/ko/" + jid + ".json"] = {"job_id": jid, "documents": [
            {"key": "B1", "kind": "법률", "title": "검증법", "law_id": "law1"},
            {"key": "A1", "kind": "별표", "title": "검증표", "law_id": "annex1"}]}
        files["laws/zh/" + jid + ".json"] = {"job_id": jid, "rows": [
            {"id": "B1-zh-00001", "doc": "B1", "law_id": "law1", "source_hash": "h1"},
            {"id": "A1-zh-00001", "doc": "A1", "law_id": "annex1", "source_hash": "h2"}]}
        files["laws/en/" + jid + ".json"] = {"job_id": jid}
    files[config["source_manifest"]] = "\n".join(json.dumps(j) for j in jobs)
    return config, event, files


class MemorySnapshot:
    def __init__(self, files, sha):
        self.files = copy.deepcopy(files)
        self.sha = sha
        self.paths = set(files)

    def text(self, path):
        value = self.files[path]
        return value if isinstance(value, str) else s.encoded(value)

    def read(self, path, default=None):
        return copy.deepcopy(self.files.get(path, default))

    def blob(self, path):
        return hashlib.sha1(self.text(path).encode()).hexdigest()


class MemoryStore:
    def __init__(self, files):
        self.files = copy.deepcopy(files)
        self.sha = "head0"
        self.published = []
        self.before_publish = None
        self.before_refresh = None

    @contextlib.contextmanager
    def lock(self):
        yield

    def refresh(self):
        if self.before_refresh:
            self.before_refresh(self)
        return MemorySnapshot(self.files, self.sha)

    def publish(self, base, changes, message):
        if self.before_publish:
            callback, self.before_publish = self.before_publish, None
            callback(self)
        if base != self.sha:
            raise s.Conflict("conflict")
        for path, value in changes.items():
            if value is None:
                self.files.pop(path, None)
            else:
                self.files[path] = copy.deepcopy(value)
        self.published.append(copy.deepcopy(changes))
        self.sha = "head" + str(len(self.published))
        return self.sha


class SupplierTests(unittest.TestCase):
    def setUp(self):
        self.config, self.event, files = fixture()
        self.store = MemoryStore(files)
        self.sent = []

    def sender(self, config, event, text):
        self.assertTrue(self.store.published)
        active = self.store.files["status/active/C01.json"]
        self.assertEqual(active["delivery_status"], "INTENT_RECORDED_NO_RESEND")
        self.sent.append(text)

    def run_supply(self, sender=None):
        return s.run(self.config, self.event, self.store, sender or self.sender, dispatch=True)

    def test_intent_before_send_and_replay_deduplicated(self):
        first = self.run_supply()
        self.assertEqual(first["status"], "SENT_AWAITING_OBSERVER_ACK")
        again = self.run_supply()
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(self.sent), 1)

    def test_preview_never_publishes_or_sends(self):
        result = s.run(self.config, self.event, self.store, self.sender)
        self.assertTrue(result["preview"])
        self.assertEqual(self.store.published, [])
        self.assertIn("별표", result["prompt"])

    def test_old_event_is_rejected(self):
        self.event["observed_at"] = (s.now() - dt.timedelta(minutes=5)).isoformat()
        with self.assertRaisesRegex(s.SupplyError, "STALE"):
            self.run_supply()
        self.assertFalse(self.sent)

    def test_wrong_panel_conversation_busy_or_composer(self):
        for key, value in [("panel_id", "panel-02"), ("conversation_id", "different"),
                           ("status", "WORKING"), ("composer_empty", False)]:
            with self.subTest(key=key):
                original = self.event[key]
                self.event[key] = value
                with self.assertRaises(s.SupplyError):
                    self.run_supply()
                self.event[key] = original
        self.assertFalse(self.store.published)

    def test_timeout_and_new_event_never_resend_active(self):
        def timeout(*args):
            self.sent.append("possibly delivered")
            raise TimeoutError()
        result = self.run_supply(timeout)
        self.assertEqual(result["status"], "UNCERTAIN_NO_RESEND")
        self.assertIsNone(result["sent"])
        self.run_supply()
        self.event["event_id"] = "test-002"
        self.event["expected_assignment_id"] = result["assignment_id"]
        result = self.run_supply()
        self.assertEqual(result["status"], "WAIT_ACTIVE_FAMILY")
        self.assertEqual(len(self.sent), 1)

    def test_crash_after_intent_never_resends(self):
        def crash(*args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_supply(crash)
        retry = self.run_supply()
        self.assertEqual(retry["status"], "INTENT_RECORDED_NO_RESEND")
        self.assertFalse(self.sent)

    def test_event_id_reuse_rejected(self):
        self.run_supply()
        self.event["conversation_id"] = "other"
        with self.assertRaisesRegex(s.SupplyError, "EVENT_ID_REUSED"):
            self.run_supply()

    def test_stale_replayed_event_returns_receipt_without_send(self):
        self.run_supply()
        with mock.patch.object(s, "now", return_value=s.now() + dt.timedelta(days=1)):
            self.assertTrue(self.run_supply()["deduplicated"])
        self.assertEqual(len(self.sent), 1)

    def test_rejected_push_replans_before_send(self):
        def change(store):
            store.sha = "other-head"
            store.files["status/active/C02.json"] = {"worker": "C02", "job_id": J1, "status": "ACTIVE"}
        self.store.before_publish = change
        result = self.run_supply()
        self.assertEqual(result["job_id"], J2)
        self.assertEqual(len(self.sent), 1)

    def test_source_change_before_send_holds(self):
        def change(store):
            if store.published:
                store.files["laws/zh/" + J1 + ".json"]["rows"][0]["source_hash"] = "changed"
        self.store.before_refresh = change
        self.assertEqual(self.run_supply()["status"], "UNCERTAIN_NO_RESEND")
        self.assertFalse(self.sent)

    def test_hold_never_bypassed(self):
        jobs = [json.loads(x) for x in self.store.files[self.config["source_manifest"]].splitlines()]
        jobs[0]["prior_queue_status"] = "SAFETY_HOLD"
        self.store.files[self.config["source_manifest"]] = "\n".join(json.dumps(x) for x in jobs)
        self.assertEqual(self.run_supply()["job_id"], J2)

    def make_completed(self, count=2):
        first = self.run_supply()
        self.event["event_id"] = "next-ready"
        self.event["expected_assignment_id"] = first["assignment_id"]
        self.store.files["review/zh/" + J1 + ".json"] = {
            "schema": "LAW_ZH_MINIMAL_REVIEW_V1", "job_id": J1, "status": "COMPLETE",
            "worker": "C01", "checked_rows": count, "edits": []}
        self.store.files["receipts/" + J1 + ".json"] = {
            "job_id": J1, "status": "COMPLETE", "worker": "C01", "checked_rows": count, "corrected_rows": 0}

    def test_complete_family_released_atomically_with_next_claim(self):
        self.make_completed()
        result = self.run_supply()
        self.assertEqual(result["job_id"], J2)
        self.assertTrue(any(p.startswith("status/completed/") for p in self.store.published[-2]))
        self.assertIn("status/active/C01.json", self.store.published[-2])

    def test_incomplete_row_count_cannot_free_worker(self):
        self.make_completed(1)
        with self.assertRaisesRegex(s.SupplyError, "ROW_COVERAGE"):
            self.run_supply()
        self.assertEqual(len(self.sent), 1)

    def test_correction_without_text_cannot_free_worker(self):
        self.make_completed()
        self.store.files["review/zh/" + J1 + ".json"]["edits"] = [{"id": "B1-zh-00001"}]
        self.store.files["receipts/" + J1 + ".json"]["corrected_rows"] = 1
        with self.assertRaisesRegex(s.SupplyError, "MISSING_CORRECTION_TEXT"):
            self.run_supply()
        self.assertEqual(len(self.sent), 1)

    def test_existing_correction_text_variants_are_supported(self):
        self.make_completed()
        for field in ("text", "new_text", "after", "corrected_text"):
            with self.subTest(field=field):
                self.store.files["review/zh/" + J1 + ".json"]["edits"] = [{"id": "B1-zh-00001", field: "correction"}]
                self.store.files["receipts/" + J1 + ".json"]["corrected_rows"] = 1
                snapshot = self.store.refresh()
                self.assertTrue(s.completed(snapshot, s.manifest(snapshot, self.config)[J1], self.store.files["status/active/C01.json"]))

    def test_stop_file_blocks_direct_supply_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = Path(directory) / "STOP"
            stop.write_text("user_stop_all_workers")
            self.config["stop_paths"] = [str(stop)]
            with self.assertRaisesRegex(s.SupplyError, "WORKFLOW_STOP_PRESENT"):
                self.run_supply()
        self.assertFalse(self.sent)
        self.assertFalse(self.store.published)

    def test_legacy_receipt_without_job_id_bound_by_path_and_review(self):
        self.make_completed()
        del self.store.files["receipts/" + J1 + ".json"]["job_id"]
        self.store.files["status/active/C01.json"]["schema"] = "NOTEX_WHOLE_LAW_FAMILY_ASSIGNMENT_V1"
        self.assertEqual(self.run_supply()["job_id"], J2)

    def test_new_receipt_missing_or_wrong_job_id_rejected(self):
        self.make_completed()
        receipt = self.store.files["receipts/" + J1 + ".json"]
        del receipt["job_id"]
        with self.assertRaisesRegex(s.SupplyError, "RECEIPT_JOB_ID_REQUIRED"):
            self.run_supply()
        receipt["job_id"] = J2
        with self.assertRaisesRegex(s.SupplyError, "COMPLETION_JOB_MISMATCH"):
            self.run_supply()

    def transport_observation(self, marker=None, draft=None):
        return {"success": True, "observation": {"success": True, "url": "https://chatgpt.com/c/conv-1",
                "status": "IDLE", "lastUser": marker or "previous", "elements": [] if not draft else [
                    {"role": "textbox", "text": draft},
                    {"tag": "button", "ariaLabel": "보내기", "disabled": False, "elementId": "send-1"}]}}

    def test_transport_verifies_sent_marker_without_second_send(self):
        text = "[marker]\n" + "instruction " * 20
        responses = [self.transport_observation(), {"success": True}, self.transport_observation("[marker]")]
        with mock.patch.object(s, "panel_command", side_effect=responses) as call:
            self.assertTrue(s.send_panel(self.config, self.event, text))
            self.assertEqual([x.args[1] for x in call.call_args_list], ["panel.observe", "panel.chat.send", "panel.observe"])

    def test_transport_completes_own_unsent_draft_once(self):
        text = "[marker]\n" + "instruction " * 20
        responses = [self.transport_observation(), {"success": False, "error": "send-button-not-found"},
                     self.transport_observation(draft=text[:150]), {"success": True},
                     self.transport_observation("[marker]")]
        with mock.patch.object(s, "panel_command", side_effect=responses) as call:
            self.assertTrue(s.send_panel(self.config, self.event, text))
            self.assertEqual([x.args[1] for x in call.call_args_list],
                             ["panel.observe", "panel.chat.send", "panel.observe", "panel.click", "panel.observe"])

    def test_transport_never_clicks_unrelated_draft_or_denial(self):
        for responses in ([self.transport_observation(), {"success": False, "error": "permission denied"}],
                          [self.transport_observation(draft="unrelated user draft")]):
            with self.subTest(responses=responses):
                with mock.patch.object(s, "panel_command", side_effect=responses) as call:
                    with self.assertRaises(s.SupplyError):
                        s.send_panel(self.config, self.event, "[marker]\n" + "instruction " * 20)
                    self.assertNotIn("panel.click", [x.args[1] for x in call.call_args_list])

    def test_ack_updates_state_without_send(self):
        first = self.run_supply()
        self.event.update(event_id="ack-001", kind="DELIVERY_ACK", status="WORKING",
                          expected_assignment_id=first["assignment_id"], marker=first["marker"])
        self.assertEqual(self.run_supply()["status"], "SENT_VERIFIED")
        self.assertEqual(len(self.sent), 1)

    def test_source_annex_omission_rejected(self):
        self.store.files["laws/zh/" + J1 + ".json"]["rows"][1]["doc"] = "B1"
        with self.assertRaisesRegex(s.SupplyError, "DOCUMENT_ROWS_INCOMPLETE"):
            self.run_supply()


class GitTransactionTests(unittest.TestCase):
    def test_real_remote_cas_preserves_checkout_and_rejects_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            remote, clone = base / "owner" / "repo.git", base / "clone"
            remote.parent.mkdir()
            def git(*args, cwd=None):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
            git("init", "--bare", str(remote))
            git("clone", str(remote), str(clone))
            git("config", "user.name", "Test", cwd=clone)
            git("config", "user.email", "test@example.invalid", cwd=clone)
            git("checkout", "-b", "main", cwd=clone)
            (clone / "README").write_text("source")
            git("add", "README", cwd=clone)
            git("commit", "-m", "seed", cwd=clone)
            git("push", "origin", "main", cwd=clone)
            store = s.GitStore({"repo_root": str(clone), "repository": "owner/repo", "branch": "main"})
            before = git("rev-parse", "HEAD", cwd=clone)
            snap = store.refresh()
            commit = store.publish(snap.sha, {"status/supply-events/one.json": {"ok": True}}, "one")
            self.assertNotEqual(commit, before)
            self.assertEqual(git("rev-parse", "HEAD", cwd=clone), before)
            self.assertEqual(git("status", "--porcelain", cwd=clone), "")
            with self.assertRaises(s.Conflict):
                store.publish(snap.sha, {"status/supply-events/two.json": {}}, "stale")
            with self.assertRaises(s.SupplyError):
                store.publish(commit, {"laws/ko/source.json": {}}, "forbidden")


class PassiveObserverTests(unittest.TestCase):
    def setUp(self):
        self.config, _, _ = fixture()
        timestamp = s.now().timestamp()
        self.active = {"assignment_id": "WF-old", "job_id": J1, "worker": "C01", "status": "ACTIVE"}
        self.report = {"schema": "LAO_PASSIVE_OBSERVATION_V1", "pc": self.config["pc"], "mode": "PASSIVE",
                       "boot": "boot-1", "sequence": 1, "observed_at": timestamp, "health": "OK",
                       "panels": [{"panel_id": "panel-01", "observation_ok": True, "draft_present": False,
                                   "url": "https://chatgpt.com/c/conv-1", "observed_at": timestamp,
                                   "native_state": "IDLE", "state": "COMPLETION_REPORTED",
                                   "assignment": {"worker": "C01", "job_id": J1,
                                                  "marker": "WORKC-WHOLE-LAW-FAMILY:WF-old:C01:" + J1}}]}
        self.health = {k: v for k, v in self.report.items() if k != "panels"}

    def convert(self):
        return s.observer_event(self.config, self.report, self.health, "C01", self.active)

    def test_observer_completion_becomes_ready_not_verified_complete(self):
        event = self.convert()
        self.assertEqual(event["kind"], "READY")
        self.assertEqual(event["expected_assignment_id"], "WF-old")
        self.assertNotIn("complete_verified", event)

    def test_stale_or_inflight_report_rejected(self):
        self.health["sequence"] = 2
        with self.assertRaisesRegex(s.SupplyError, "SNAPSHOT_IN_FLIGHT"):
            self.convert()
        self.health["sequence"] = 1
        self.health["observed_at"] -= 61
        with self.assertRaisesRegex(s.SupplyError, "STALE"):
            self.convert()

    def test_stop_flags_are_enforced_without_deleting_them(self):
        self.report["workflow_controls"] = [{"exists": True, "path": "workflow/STOP", "value": "user_stop_all_workers"}]
        with self.assertRaisesRegex(s.SupplyError, "WORKFLOW_STOP_PRESENT"):
            self.convert()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "STOP"
            path.write_text("user_stop_all_workers")
            with self.assertRaisesRegex(s.SupplyError, "WORKFLOW_STOP_PRESENT"):
                s.require_unstopped({"stop_paths": [str(path)]})
            self.assertTrue(path.exists())

    def test_wrong_family_approval_or_draft_blocks(self):
        row = self.report["panels"][0]
        row["assignment"]["job_id"] = J2
        with self.assertRaisesRegex(s.SupplyError, "WAIT_OBSERVER_ASSIGNMENT"):
            self.convert()
        row["assignment"]["job_id"] = J1
        row["state"] = "APPROVAL_REQUIRED"
        with self.assertRaises(s.SupplyError):
            self.convert()
        row["state"] = "COMPLETION_REPORTED"
        row["draft_present"] = True
        with self.assertRaises(s.SupplyError):
            self.convert()

    def test_matching_new_marker_becomes_ack_while_working(self):
        marker = "LAW-SUPPLY-V2:S2-new:C01:" + J1
        self.active.update(schema="LAW_SUPPLIER_ASSIGNMENT_V2", assignment_id="S2-new",
                           marker="[" + marker + "]", delivery_status="UNCERTAIN_NO_RESEND")
        self.report["panels"][0].update(native_state="WORKING", state="WORKING")
        self.report["panels"][0]["assignment"]["marker"] = marker
        self.assertEqual(self.convert()["kind"], "DELIVERY_ACK")


if __name__ == "__main__":
    unittest.main()
