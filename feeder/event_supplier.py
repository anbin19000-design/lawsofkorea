#!/usr/bin/env python3
"""One observer event -> at most one whole-family assignment. No observation loop."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import Request, urlopen
from urllib.parse import urlparse

VERSION = "2.3.1"
EVENT_SCHEMA = "LAW_SUPPLY_EVENT_V1"
ID = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
JOB = re.compile(r"F-[a-f0-9]{20}\Z")
WRITABLE = ("status/active/", "status/claims/", "status/delivery/",
            "status/completed/", "status/supply-events/")


class SupplyError(RuntimeError):
    pass


class Conflict(SupplyError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso():
    return now().isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def correction_text(edit):
    # Field names verified in the two repositories' existing review files.
    return next((edit[k] for k in ("text", "new_text", "after", "corrected_text") if k in edit), None)


def check(condition, message):
    if not condition:
        raise SupplyError(message)


def json_file(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def read_observation(config):
    """Resolve the short atomic-replacement gap without reusing stale evidence."""
    for attempt in range(3):
        report = json_file(config["observer_latest"])
        health = json_file(config["observer_health"])
        if (report.get("boot"), report.get("sequence")) == (health.get("boot"), health.get("sequence")):
            return report, health
        if attempt < 2:
            time.sleep(0.03)
    raise SupplyError("OBSERVER_SNAPSHOT_IN_FLIGHT")


def require_unstopped(config):
    check(not any(Path(p).exists() for p in config.get("stop_paths", [])), "WORKFLOW_STOP_PRESENT")


class Snapshot:
    def __init__(self, store, sha):
        self.store, self.sha, self.cache = store, sha, {}
        entries = store.git("ls-tree", "-r", "--name-only", sha).stdout
        self.paths = set(entries.splitlines())

    def text(self, path):
        check(path in self.paths, "MISSING_FILE:" + path)
        if path not in self.cache:
            self.cache[path] = self.store.git("show", self.sha + ":" + path).stdout
        return self.cache[path]

    def read(self, path, default=None):
        return json.loads(self.text(path)) if path in self.paths else default

    def blob(self, path):
        return self.store.git("rev-parse", self.sha + ":" + path).stdout.strip()


class GitStore:
    """Read remote snapshots; publish a fast-forward commit using a private index.

    Never changes the caller's checkout, index or branch. A rejected push causes
    a fresh plan, never a blind rebase of an old claim.
    """
    def __init__(self, config):
        self.config = config
        self.root = Path(config["repo_root"])
        self.branch = config["branch"]
        check(re.fullmatch(r"[A-Za-z0-9_./-]+", self.branch) is not None
              and ".." not in self.branch, "INVALID_BRANCH")
        remote = self.git("config", "--get", "remote.origin.url").stdout.strip()
        remote_path = re.sub(r"\.git$", "", remote.replace("\\", "/").rstrip("/"))
        check(remote_path.endswith("/" + config["repository"])
              or remote_path.endswith(":" + config["repository"]), "WRONG_REPOSITORY")

    def git(self, *args, input=None, env=None, required=True):
        options = os.environ.copy()
        options["GIT_TERMINAL_PROMPT"] = "0"
        options["GCM_INTERACTIVE"] = "Never"
        if env:
            options.update(env)
        trace = self.config.get("trace_path")
        if trace:
            Path(trace).write_text(encoded({"operation": args[0], "phase": "START", "at": iso()}), encoding="utf-8")
        proc = subprocess.run(["git", "-C", str(self.root), *args], input=input,
                              text=True, encoding="utf-8", capture_output=True,
                              env=options, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if trace:
            Path(trace).write_text(encoded({"operation": args[0], "phase": "END", "exit_code": proc.returncode,
                                           "at": iso(), "error": re.sub(r"https?://\S+", "[remote]", proc.stderr)[-800:] if proc.returncode else None}), encoding="utf-8")
        if required and proc.returncode:
            # Never echo an authenticated remote URL or credential helper output.
            raise SupplyError("GIT_OPERATION_FAILED:" + args[0])
        return proc

    @contextlib.contextmanager
    def lock(self):
        common = Path(self.git("rev-parse", "--git-common-dir").stdout.strip())
        if not common.is_absolute():
            common = self.root / common
        with open(common / "law-supplier-v2.lock", "a+b") as stream:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SupplyError("SUPPLIER_ALREADY_RUNNING") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def refresh(self):
        self.git("fetch", "origin", self.branch)
        sha = self.git("rev-parse", "refs/remotes/origin/" + self.branch).stdout.strip()
        return Snapshot(self, sha)

    def publish(self, base, changes, message):
        check(bool(changes), "EMPTY_TRANSACTION")
        check(all(p.startswith(WRITABLE) and ".." not in p for p in changes),
              "WRITE_OUTSIDE_SUPPLIER_STATUS")
        with tempfile.TemporaryDirectory(prefix="law-supply-index-") as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            self.git("read-tree", base, env=env)
            for path, value in changes.items():
                if value is None:
                    self.git("update-index", "--force-remove", "--", path, env=env)
                else:
                    blob = self.git("hash-object", "-w", "--stdin", input=encoded(value)).stdout.strip()
                    self.git("update-index", "--add", "--cacheinfo", "100644", blob, path, env=env)
            tree = self.git("write-tree", env=env).stdout.strip()
            commit = self.git("commit-tree", tree, "-p", base, "-m", message).stdout.strip()
        pushed = self.git("push", "origin", commit + ":refs/heads/" + self.branch, required=False)
        fresh = self.refresh()
        # Also resolves a transport timeout/error after GitHub actually accepted it.
        if self.git("merge-base", "--is-ancestor", commit, fresh.sha, required=False).returncode == 0:
            return commit
        if fresh.sha != base:
            raise Conflict("BRANCH_CHANGED_REPLAN")
        check(pushed.returncode == 0, "PUBLISH_FAILED_NO_DISPATCH:" +
              re.sub(r"https?://\S+", "[remote]", pushed.stderr)[-1000:])
        raise SupplyError("PUBLISH_NOT_VERIFIED_NO_DISPATCH")


def paths(job):
    check(JOB.fullmatch(job) is not None, "INVALID_JOB_ID")
    return {"ko": "laws/ko/" + job + ".json", "zh": "laws/zh/" + job + ".json",
            "en": "laws/en/" + job + ".json", "review": "review/zh/" + job + ".json",
            "receipt": "receipts/" + job + ".json"}


def manifest(snapshot, config):
    if config.get("partition_manifest_blob"):
        check(snapshot.blob(config["source_manifest"]) == config["partition_manifest_blob"],
              "PARTITION_CHANGED_REVALIDATE_BOTH_PCS")
    rows = [json.loads(line) for line in snapshot.text(config["source_manifest"]).splitlines() if line.strip()]
    check(len({x["job_id"] for x in rows}) == len(rows), "DUPLICATE_MANIFEST_JOB")
    return {x["job_id"]: x for x in rows}


def source(snapshot, job):
    p = paths(job["job_id"])
    ko, zh = snapshot.read(p["ko"]), snapshot.read(p["zh"])
    check(ko and zh and p["en"] in snapshot.paths, "SOURCE_PACKAGE_MISSING")
    check(ko.get("job_id") == zh.get("job_id") == job["job_id"], "SOURCE_JOB_MISMATCH")
    docs, rows = ko.get("documents", []), zh.get("rows", [])
    keys = {d["key"] for d in docs}
    check(len(keys) == len(docs) == job["documents"] and len(rows) == job["rows"] > 0,
          "SOURCE_COUNTS_MISMATCH")
    check(any(d.get("kind") == "법률" for d in docs), "BASE_ACT_MISSING")
    check({r.get("doc") for r in rows} == keys, "DOCUMENT_ROWS_INCOMPLETE")
    check(len({r["id"] for r in rows}) == len(rows), "DUPLICATE_SOURCE_ROW")
    by_key = {d["key"]: d for d in docs}
    check(all(r.get("source_hash") and r.get("law_id") == by_key[r["doc"]].get("law_id") for r in rows),
          "SOURCE_IDENTITY_MISMATCH")
    return docs, rows, {k: snapshot.blob(p[k]) for k in ("ko", "zh", "en")}


def completed(snapshot, job, assignment=None):
    p = paths(job["job_id"])
    review, receipt = snapshot.read(p["review"]), snapshot.read(p["receipt"])
    if not review or not receipt:
        return False
    if review.get("status") != "COMPLETE" or receipt.get("status") != "COMPLETE":
        return False
    _, rows, blobs = source(snapshot, job)
    check(review.get("schema") == "LAW_ZH_MINIMAL_REVIEW_V1", "INVALID_REVIEW_SCHEMA")
    check(review.get("job_id") == job["job_id"], "COMPLETION_JOB_MISMATCH")
    if "job_id" not in receipt:
        # The original NOTEX receipt contract omitted job_id. Its exact filename,
        # matching review ID, worker, all-row count and edit IDs jointly bind it.
        check(assignment and assignment.get("schema") != "LAW_SUPPLIER_ASSIGNMENT_V2"
              and assignment.get("job_id") == job["job_id"], "RECEIPT_JOB_ID_REQUIRED")
    else:
        check(receipt["job_id"] == job["job_id"], "COMPLETION_JOB_MISMATCH")
    for record in (review, receipt):
        check(type(record.get("checked_rows")) is int and record["checked_rows"] == len(rows),
              "INCOMPLETE_ROW_COVERAGE")
        if assignment:
            check(record.get("worker") == assignment["worker"], "COMPLETION_WORKER_MISMATCH")
    check(review.get("worker") == receipt.get("worker"), "COMPLETION_WORKER_MISMATCH")
    edits = review.get("edits")
    check(isinstance(edits, list), "INVALID_EDITS")
    check(all(isinstance(e, dict) and isinstance(correction_text(e), str)
              and correction_text(e).strip() for e in edits), "MISSING_CORRECTION_TEXT")
    # Existing workers use id or row_id; preserve both documented legacy variants.
    ids = [e.get("id", e.get("row_id")) for e in edits]
    check(len(ids) == len(set(ids)) and set(ids) <= {r["id"] for r in rows}, "INVALID_EDIT_ROW_IDS")
    check(type(receipt.get("corrected_rows")) is int and receipt["corrected_rows"] == len(ids),
          "CORRECTED_COUNT_MISMATCH")
    originals = {r["id"]: r for r in rows}
    for edit, ident in zip(edits, ids):
        for key in ("source_hash", "law_id", "target_id", "doc"):
            if key in edit:
                check(edit[key] == originals[ident].get(key), "EDIT_IDENTITY_MISMATCH")
    if assignment and assignment.get("source_blobs"):
        check(assignment["source_blobs"] == blobs, "SOURCE_CHANGED_DURING_REVIEW")
    return True


def active_records(snapshot):
    records = {}
    for p in sorted(snapshot.paths):
        if re.fullmatch(r"status/active/[A-Z][0-9]{2}\.json", p):
            item = snapshot.read(p)
            if item.get("status") == "ACTIVE":
                records[item["worker"]] = item
    check(len({x["job_id"] for x in records.values()}) == len(records), "DUPLICATE_ACTIVE_FAMILY")
    return records


def validate_event(event, config, current=None):
    check(event.get("schema") == EVENT_SCHEMA, "INVALID_EVENT_SCHEMA")
    check(ID.fullmatch(str(event.get("event_id", ""))) is not None, "INVALID_EVENT_ID")
    check(event.get("pc") == config["pc"], "WRONG_PC")
    workers = {w["worker"]: w for w in config["workers"] if w.get("enabled")}
    check(event.get("worker") in workers, "UNKNOWN_WORKER")
    check(event.get("panel_id") == workers[event["worker"]]["panel_id"], "WRONG_PANEL")
    check(event.get("kind") in ("READY", "DELIVERY_ACK"), "INVALID_EVENT_KIND")
    try:
        observed = dt.datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00"))
        check(observed.tzinfo is not None, "EVENT_TIMEZONE_REQUIRED")
        age = ((current or now()) - observed).total_seconds()
    except (KeyError, TypeError, ValueError) as exc:
        raise SupplyError("INVALID_EVENT_TIME") from exc
    check(-5 <= age <= config.get("event_max_age_seconds", 120), "STALE_OBSERVER_EVENT")
    check(event.get("composer_empty") is True, "COMPOSER_NOT_EMPTY")
    check(event.get("status") in ("READY", "IDLE") or event["kind"] == "DELIVERY_ACK", "WORKER_NOT_READY")


def binding_matches(snapshot, event, config):
    binding = snapshot.read(config.get("bindings_path", "status/worker-bindings.json"))
    check(binding and binding.get("pc") == config["pc"] and binding.get("repository") == config["repository"],
          "WORKER_BINDINGS_MISMATCH")
    worker = next((x for x in binding["workers"] if x["worker"] == event["worker"]), None)
    check(worker and worker.get("verified") is True and worker["panel_id"] == event["panel_id"], "UNVERIFIED_WORKER_BINDING")
    check(worker.get("conversation_id") == event.get("conversation_id"), "CONVERSATION_CHANGED")


def prompt(config, job, assignment, docs, sha):
    jid, worker = job["job_id"], assignment["worker"]
    marker = "[LAW-SUPPLY-V2:" + assignment["assignment_id"] + ":" + worker + ":" + jid + "]"
    titles = "\n".join("- " + d["kind"] + ": " + d["title"] for d in docs)
    return marker + f"""
지금부터 아래 법령가족 1건 전체를 끝까지 중국어 검수하십시오.
저장소: {config['repository']}
브랜치: {config['branch']}
원문 기준 커밋: {sha}
법령가족: {job['title']}
Job ID: {jid}
전체 문서: {len(docs)}개 / 중국어 행: {job['rows']}개
{titles}

법률·시행령·시행규칙·별표·별지·서식 전체가 하나의 배정입니다. 같은 워커가 전체를 맡고 일부만 COMPLETE로 처리하지 마십시오.
공식 GitHub 도구로 laws/ko/{jid}.json, laws/zh/{jid}.json, laws/en/{jid}.json과 연결된 documents/en, reviewed/en, meaning/pairs를 읽으십시오. 로컬 DB·패킷·worker_client·Python을 검수 데이터 경로로 사용하지 마십시오.
한국어 원문이 기준이고 영어·의미기록은 참고입니다. 명백한 오역·누락·미번역·수치·인용·용어 오류만 최소 교정하고 ID·해시·표·별표/서식 관계를 보존하십시오.
모든 행을 실제 검토한 뒤 review/zh/{jid}.json (schema LAW_ZH_MINIMAL_REVIEW_V1, job_id, worker, checked_rows, edits, status COMPLETE)과 receipts/{jid}.json (job_id, worker, checked_rows, corrected_rows, status COMPLETE, note)을 함께 커밋하십시오. edits에는 원본 row의 id와 text 필드의 교정문을 기록하고 corrected_rows는 고유 교정 행 수로 기록하십시오.
GitHub update_ref에서 명시적 권한 거절이 없는 GithubGraphQLAPIError/UNKNOWN 실행 오류가 발생하면 동일 실패 요청을 반복하지 마십시오. 기존 결과와 원본 해시를 확인한 뒤 공식 GitHub create_file(Contents API)로 검수 결과, 영수증 순서로 저장하는 별도 커밋 2개를 허용합니다. 이미 저장된 파일은 덮어쓰지 말고 확인하며, 두 파일 모두 브랜치에서 검증되기 전에는 완료로 처리하지 마십시오.
기존 완료 결과를 반복하거나 laws/*를 수정하지 마십시오. 원문이 기준 커밋에서 바뀌거나 안전·권한 거절이 발생하면 HOLD로 보고하고 재시도·우회하지 마십시오. 완료 시 두 파일이 브랜치에 존재하는지 다시 확인하고 SHA를 보고하십시오. 검수 저장은 운영 DB 반영과 별개입니다.
"""


def plan(snapshot, config, event):
    check(ID.fullmatch(str(event.get("event_id", ""))) is not None, "INVALID_EVENT_ID")
    event_path = "status/supply-events/" + event["event_id"] + ".json"
    existing = snapshot.read(event_path)
    if existing:
        check(existing.get("event_hash") == digest(encoded(event)), "EVENT_ID_REUSED_WITH_DIFFERENT_CONTENT")
        return {"result": {**existing, "deduplicated": True}, "changes": {}}
    validate_event(event, config)
    binding_matches(snapshot, event, config)
    all_jobs, active = manifest(snapshot, config), active_records(snapshot)
    prior = active.get(event["worker"])
    check(event.get("expected_assignment_id") == (prior or {}).get("assignment_id"), "STALE_ASSIGNMENT_EVENT")
    if event["kind"] == "DELIVERY_ACK":
        check(prior is not None, "NO_ASSIGNMENT_TO_ACK")
        check(event.get("marker") == prior.get("marker"), "DELIVERY_MARKER_MISMATCH")
        state = {**prior, "delivery_status": "SENT_VERIFIED"}
        receipt = {"schema": "LAW_SUPPLY_RECEIPT_V2", "event_id": event["event_id"],
                   "event_hash": digest(encoded(event)), "assignment_id": prior["assignment_id"],
                   "worker": event["worker"], "job_id": prior["job_id"], "status": "SENT_VERIFIED", "at": iso()}
        return {"result": receipt, "changes": {"status/active/" + event["worker"] + ".json": state,
                "status/claims/" + state["assignment_id"] + ".json": state,
                "status/delivery/" + state["assignment_id"] + ".json": receipt, event_path: receipt}}
    changes = {}
    if prior:
        check(prior["job_id"] in all_jobs, "ACTIVE_JOB_OUTSIDE_MANIFEST")
        if not completed(snapshot, all_jobs[prior["job_id"]], prior):
            return {"result": {"status": "WAIT_ACTIVE_FAMILY", "worker": event["worker"], "job_id": prior["job_id"]}, "changes": {}}
        finished = {**prior, "status": "COMPLETE", "completed_at": iso()}
        aid = prior["assignment_id"]
        changes.update({"status/completed/" + aid + ".json": finished,
                        "status/claims/" + aid + ".json": finished,
                        "status/active/" + event["worker"] + ".json": None})
        old_delivery = snapshot.read("status/delivery/" + aid + ".json", {})
        changes["status/delivery/" + aid + ".json"] = {**old_delivery, "state": "COMPLETED", "completed_at": iso()}
    used = {x["job_id"] for x in active.values()}
    heavy_limit = config.get("heavy_threshold_rows", 3000)
    heavy_count = sum(all_jobs.get(x["job_id"], {}).get("rows", heavy_limit) >= heavy_limit
                      for w, x in active.items() if w != event["worker"])
    chosen, docs, blobs = None, None, None
    for job in sorted(all_jobs.values(), key=lambda x: (x["rows"], x["documents"], x["job_id"])):
        jid, p = job["job_id"], paths(job["job_id"])
        if jid in used or p["review"] in snapshot.paths or p["receipt"] in snapshot.paths:
            continue
        if any(p.startswith("holds/") and jid in p for p in snapshot.paths):
            continue
        # An earlier rejection is not cleared by moving a task to GitHub.
        if any(word in str(job.get("prior_queue_status", "")).upper() for word in ("HOLD", "BLOCK", "DENIED", "NO_RETRY")):
            continue
        if job.get("assignment_unit") != "WHOLE_LAW_FAMILY" or job.get("includes_all_linked_documents") is not True:
            continue
        if job["rows"] >= heavy_limit and heavy_count >= config.get("max_heavy_active", 1):
            continue
        docs, _, blobs = source(snapshot, job)
        chosen = job
        break
    receipt = {"schema": "LAW_SUPPLY_RECEIPT_V2", "event_id": event["event_id"],
               "event_hash": digest(encoded(event)), "worker": event["worker"], "pc": config["pc"], "at": iso()}
    if chosen is None:
        receipt["status"] = "NO_ELIGIBLE_FAMILY"
        changes[event_path] = receipt
        return {"result": receipt, "changes": changes}
    aid = "S2-" + digest(config["pc"] + ":" + event["event_id"])[:32]
    claim = {"schema": "LAW_SUPPLIER_ASSIGNMENT_V2", "assignment_id": aid,
             "worker": event["worker"], "panel_id": event["panel_id"], "pc": config["pc"],
             "job_id": chosen["job_id"], "family_id": chosen["job_id"], "title": chosen["title"],
             "documents": chosen["documents"], "rows": chosen["rows"], "status": "ACTIVE",
             "delivery_status": "INTENT_RECORDED_NO_RESEND", "claimed_at": iso(),
             "unit_policy": "ACT_DECREE_RULE_ANNEX_FORMS_ONE_FAMILY",
             "source_commit": snapshot.sha, "source_blobs": blobs,
             "conversation_hash": digest(event["conversation_id"]), "event_id": event["event_id"]}
    text = prompt(config, chosen, claim, docs, snapshot.sha)
    claim["marker"] = text.splitlines()[0]
    receipt.update(status="INTENT_RECORDED_NO_RESEND", assignment_id=aid, job_id=chosen["job_id"],
                   prompt_sha256=digest(text), marker=claim["marker"])
    changes.update({"status/active/" + event["worker"] + ".json": claim,
                    "status/claims/" + aid + ".json": claim,
                    "status/delivery/" + aid + ".json": receipt, event_path: receipt})
    return {"result": receipt, "changes": changes, "prompt": text, "claim": claim}


def panel_command(config, command, **params):
    if command == "panel.observe":
        params.setdefault("maxElements", 1000)
        params.setdefault("maxMessages", 1)
    gateway = config["transport"]["panel_gateway"].rstrip("/")
    check(gateway == "http://127.0.0.1:32722", "UNVERIFIED_PANEL_GATEWAY")
    request = Request(gateway + "/command",
                      data=json.dumps({"command": command, "params": params}, ensure_ascii=True).encode("ascii"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=25) as stream:
        response = json.load(stream)
    check(isinstance(response, dict), "INVALID_PANEL_RESPONSE")
    return response


def panel_observation(response):
    node = response
    while isinstance(node, dict):
        if isinstance(node.get("observation"), dict):
            return node["observation"]
        node = node.get("result")
    raise SupplyError("PANEL_OBSERVATION_MISSING")


def verify_panel_destination(observation, event):
    parsed = urlparse(observation.get("url", ""))
    check(parsed.hostname == "chatgpt.com" and parsed.scheme == "https"
          and parsed.path == "/c/" + event["conversation_id"], "CONVERSATION_CHANGED")
    check(observation.get("success") is True, "PANEL_OBSERVATION_FAILED")


def sent_marker(observation, marker):
    return str(observation.get("lastUser") or "").startswith(marker)


def send_panel(config, event, text):
    # Bounded verification of this transmission; the passive observer owns monitoring.
    # panel.chat.send may type the text yet fail to press Send. Complete only that
    # identified draft, never type a second copy or click an unrelated draft.
    marker = text.splitlines()[0]
    before = panel_observation(panel_command(config, "panel.observe", panelId=event["panel_id"]))
    verify_panel_destination(before, event)
    if sent_marker(before, marker):
        return True
    check(before.get("status") in ("IDLE", "READY") and not any(
        e.get("role") == "textbox" and e.get("text", "").strip() for e in before.get("elements", [])),
        "PANEL_CHANGED_BEFORE_TYPING")
    response = panel_command(config, "panel.chat.send", panelId=event["panel_id"], text=text)
    messages = []
    node = response
    while isinstance(node, dict):
        messages.extend(str(node.get(k, "")) for k in ("error", "message", "reason"))
        node = node.get("result")
    check(not re.search(r"denied|forbidden|unauthori[sz]ed|approval|policy|permission|거절|권한", " ".join(messages), re.I),
          "PANEL_PERMISSION_DENIED")
    observed = panel_observation(panel_command(config, "panel.observe", panelId=event["panel_id"]))
    verify_panel_destination(observed, event)
    if sent_marker(observed, marker):
        return True
    elements = observed.get("elements", [])
    drafts = [e for e in elements if e.get("role") == "textbox" and e.get("text", "").strip()]
    buttons = [e for e in elements if e.get("tag") == "button" and not e.get("disabled")
               and e.get("ariaLabel") in ("보내기", "Send", "Send prompt")]
    normalize = lambda value: " ".join(value.split())
    check(observed.get("status") in ("IDLE", "READY") and len(drafts) == len(buttons) == 1,
          "SEND_BUTTON_NOT_READY")
    draft = normalize(drafts[0]["text"])
    check(draft.startswith(marker) and len(draft) > len(marker) + 30
          and normalize(text).startswith(draft), "DRAFT_DOES_NOT_MATCH_ASSIGNMENT")
    require_unstopped(config)
    from verified_draft import finish_own_draft
    finish_own_draft(config, event, text)
    observed = panel_observation(panel_command(config, "panel.observe", panelId=event["panel_id"]))
    verify_panel_destination(observed, event)
    check(sent_marker(observed, marker), "SEND_NOT_CONFIRMED_NO_RESEND")
    return True


def run(config, event, store, sender=send_panel, dispatch=False):
    check(socket.gethostname().upper() == config["pc"].upper(), "WRONG_EXECUTION_HOST")
    if dispatch:
        check(config.get("enabled") is True, "SUPPLIER_DISABLED")
        require_unstopped(config)
    with store.lock():
        for attempt in range(3):
            snapshot = store.refresh()
            work = plan(snapshot, config, event)
            if not dispatch:
                return {**work["result"], "preview": True, "sent": False,
                        "source_head": snapshot.sha, "prompt": work.get("prompt")}
            if not work["changes"]:
                return {**work["result"], "sent": False}
            try:
                commit = store.publish(snapshot.sha, work["changes"], "Supply event " + event["event_id"])
                break
            except Conflict:
                if attempt == 2:
                    raise
        else:
            raise SupplyError("CLAIM_NOT_PUBLISHED")
        if "prompt" not in work:
            return {**work["result"], "commit": commit, "sent": False}
        # Durable intent exists before the sole send attempt. A crash here is held.
        result = dict(work["result"])
        try:
            validate_event(event, config)  # freshness can expire during fetch/push
            require_unstopped(config)
            latest = store.refresh()
            current = latest.read("status/active/" + event["worker"] + ".json")
            check(current and current.get("assignment_id") == result["assignment_id"], "CLAIM_CHANGED_BEFORE_SEND")
            binding_matches(latest, event, config)
            check(source(latest, manifest(latest, config)[current["job_id"]])[2] == current["source_blobs"], "SOURCE_CHANGED_BEFORE_SEND")
            sender(config, event, work["prompt"])
            result.update(status="SENT_AWAITING_OBSERVER_ACK", sent=True)
        except Exception as exc:
            result.update(status="UNCERTAIN_NO_RESEND", sent=None, error_type=type(exc).__name__,
                          error=str(exc) if isinstance(exc, SupplyError) else type(exc).__name__)
        result["attempted_at"] = iso()
        result["claim_commit"] = commit
        # Publication can be retried; the message cannot.
        for attempt in range(3):
            try:
                latest = store.refresh()
            except Exception:
                return {**result, "publication": "DELIVERY_RECORD_PENDING_NO_RESEND"}
            current = latest.read("status/active/" + event["worker"] + ".json")
            if not current or current.get("assignment_id") != result["assignment_id"]:
                return {**result, "publication": "CLAIM_CHANGED_RECORD_PENDING"}
            changes = {"status/delivery/" + result["assignment_id"] + ".json": result,
                       "status/supply-events/" + event["event_id"] + ".json": result}
            current["delivery_status"] = result["status"]
            changes["status/active/" + event["worker"] + ".json"] = current
            changes["status/claims/" + result["assignment_id"] + ".json"] = current
            try:
                result["delivery_commit"] = store.publish(latest.sha, changes, "Record supply delivery " + result["assignment_id"])
                return result
            except Conflict:
                continue
            except Exception:
                return {**result, "publication": "DELIVERY_RECORD_PENDING_NO_RESEND"}
        return {**result, "publication": "DELIVERY_RECORD_PENDING_NO_RESEND"}


def inspect(config, store):
    snapshot = store.refresh()
    jobs = manifest(snapshot, config)
    active = active_records(snapshot)
    ready, errors = [], []
    for worker, item in active.items():
        try:
            complete = item["job_id"] in jobs and completed(snapshot, jobs[item["job_id"]], item)
            ready.append({"worker": worker, "job_id": item["job_id"], "complete_verified": complete})
        except SupplyError as exc:
            errors.append({"worker": worker, "error": str(exc)})
    return {"version": VERSION, "pc": config["pc"], "repository": config["repository"],
            "branch": config["branch"], "head": snapshot.sha, "manifest_jobs": len(jobs),
            "active": ready, "validation_errors": errors, "observes_panels": False,
            "runs_service": False, "writes": False,
            "stop_paths_present": [p for p in config.get("stop_paths", []) if Path(p).exists()]}


def observer_event(config, report, health, worker, active, current=None, allow_recovery=False):
    """Translate passive.2 output; never inspect a browser or trust completion prose."""
    check(report.get("schema") == health.get("schema") == "LAO_PASSIVE_OBSERVATION_V1", "INVALID_OBSERVER_SCHEMA")
    check(report.get("pc") == health.get("pc") == config["pc"], "WRONG_OBSERVER_PC")
    check(report.get("mode") == "PASSIVE" and report.get("health") == health.get("health")
          and report.get("health") in ("OK", "DEGRADED"),
          "OBSERVER_NOT_HEALTHY")
    if report.get("health") == "DEGRADED":
        check(not report.get("list_error") and report.get("expected_panels") == len(report.get("panels", [])),
              "OBSERVER_LIST_INCOMPLETE")
    check((report.get("boot"), report.get("sequence")) == (health.get("boot"), health.get("sequence")),
          "OBSERVER_SNAPSHOT_IN_FLIGHT")
    clock = (current or now()).timestamp()
    for value in (report.get("observed_at"), health.get("observed_at")):
        check(isinstance(value, (float, int)) and -5 <= clock-value <= config.get("observer_stale_seconds", 60),
              "OBSERVER_STALE")
    check(not any(x.get("exists") is True and ("STOP" in str(x.get("path", "")).upper()
              or "user_stop" in str(x.get("value", "")).lower()) for x in report.get("workflow_controls", [])),
          "WORKFLOW_STOP_PRESENT")
    binding = next((w for w in config["workers"] if w["worker"] == worker and w.get("enabled")), None)
    check(binding is not None, "UNKNOWN_WORKER")
    matches = [p for p in report.get("panels", []) if p.get("panel_id") == binding["panel_id"]]
    check(len(matches) == 1, "OBSERVER_PANEL_MISSING_OR_DUPLICATE")
    row = matches[0]
    check(row.get("observation_ok") is True and row.get("draft_present") is False, "OBSERVER_PANEL_NOT_READY")
    check(-5 <= clock-row["observed_at"] <= config.get("observer_stale_seconds", 60), "OBSERVER_PANEL_STALE")
    assignment = row.get("assignment") or {}
    check(assignment.get("worker") in (None, worker), "OBSERVER_WORKER_MISMATCH")
    for key in ("repository", "branch"):
        if assignment.get(key):
            check(assignment[key] == config[key], "OBSERVER_REPOSITORY_MISMATCH")
    kind = "READY"
    if active:
        check(assignment.get("job_id") == active["job_id"], "WAIT_OBSERVER_ASSIGNMENT")
        seen = str(assignment.get("marker") or "").strip("[]")
        check(active["assignment_id"] in seen.split(":"), "WAIT_OBSERVER_ASSIGNMENT")
        if active.get("schema") == "LAW_SUPPLIER_ASSIGNMENT_V2" and active.get("delivery_status") != "SENT_VERIFIED":
            check(seen == active["marker"].strip("[]"), "DELIVERY_MARKER_MISMATCH")
            kind = "DELIVERY_ACK"
    if kind == "READY":
        check(row.get("native_state") in ("IDLE", "READY")
              and row.get("state") in (("IDLE", "READY", "COMPLETION_REPORTED", "STORAGE_BLOCKED", "REPORTED_ERROR")
                                        if allow_recovery and active else ("IDLE", "READY", "COMPLETION_REPORTED")),
              "OBSERVER_WORKER_NOT_READY")
    else:
        check(row.get("state") in ("WORKING", "IDLE", "READY", "COMPLETION_REPORTED"), "OBSERVER_WORKER_NOT_READY")
    parsed = urlparse(row.get("url") or "")
    check(parsed.scheme == "https" and parsed.hostname == "chatgpt.com" and re.fullmatch(r"/c/[A-Za-z0-9-]+", parsed.path),
          "INVALID_OBSERVER_CONVERSATION")
    event = {"schema": EVENT_SCHEMA,
             "event_id": "obs-" + digest(str(report["boot"]) + ":" + str(report["sequence"]) + ":" + worker + ":" + kind)[:40],
             "kind": kind, "pc": config["pc"], "worker": worker, "panel_id": binding["panel_id"],
             "conversation_id": parsed.path.split("/")[-1],
             "observed_at": dt.datetime.fromtimestamp(row["observed_at"], dt.timezone.utc).isoformat(),
             "status": row["native_state"], "composer_empty": True,
             "expected_assignment_id": (active or {}).get("assignment_id")}
    if kind == "DELIVERY_ACK":
        event["marker"] = active["marker"]
    return event


def consume_observer(config, store, worker=None, dispatch=False):
    """One pass over already-written observer files, with no polling process."""
    workers = [worker] if worker else [w["worker"] for w in config["workers"] if w.get("enabled")]
    results = []
    for ident in workers:
        try:
            require_unstopped(config)
            report = json_file(config["observer_latest"])
            health = json_file(config["observer_health"])
            snapshot = store.refresh()
            active = active_records(snapshot).get(ident)
            event = observer_event(config, report, health, ident, active)
            result = run(config, event, store, dispatch=dispatch)
            if (dispatch and event["kind"] == "DELIVERY_ACK"
                    and next(p for p in report["panels"] if p["panel_id"] == event["panel_id"])["native_state"] in ("IDLE", "READY")):
                # A completed worker need not wait another minute merely for its ACK.
                report = json_file(config["observer_latest"])
                health = json_file(config["observer_health"])
                active = active_records(store.refresh()).get(ident)
                next_event = observer_event(config, report, health, ident, active)
                if next_event["kind"] == "READY":
                    result = run(config, next_event, store, dispatch=True)
            result.pop("prompt", None)
            results.append({"worker": ident, **result})
        except Exception as exc:
            results.append({"worker": ident, "status": "HOLD", "sent": False,
                            "error": str(exc) if isinstance(exc, SupplyError) else type(exc).__name__})
    return {"pc": config["pc"], "observer_read_only": True, "preview": not dispatch, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("supplier-config.json")))
    parser.add_argument("command", choices=("inspect", "preview", "supply", "observer-preview", "observer-supply"))
    parser.add_argument("--event", help="Local observer JSON path; '-' reads stdin")
    parser.add_argument("--worker", help="Optional one worker for observer-preview/observer-supply")
    args = parser.parse_args()
    try:
        config = json_file(args.config)
        check(config.get("schema") == "LAW_EVENT_SUPPLIER_CONFIG_V2", "INVALID_CONFIG")
        store = GitStore(config)
        if args.command == "inspect":
            result = inspect(config, store)
        elif args.command.startswith("observer-"):
            result = consume_observer(config, store, args.worker, dispatch=args.command == "observer-supply")
        else:
            check(bool(args.event), "OBSERVER_EVENT_REQUIRED")
            event = json.loads(sys.stdin.read()) if args.event == "-" else json_file(args.event)
            result = run(config, event, store, dispatch=args.command == "supply")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "HOLD", "sent": False, "error": str(exc) if isinstance(exc, SupplyError) else type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


