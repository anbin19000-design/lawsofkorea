# LaoPanel continuous supervision — 2026-11-07

The live controller is supplier_batch.py --service (2.3.1), driven by the passive observer (2026.11.07-passive.10). Both use a 10-second target cadence. A batch can take longer when GitHub work or transport is slow. Separate per-worker outcomes keep a failed panel from blocking healthy panels.

## Process lifetime and dashboard

Windows task LawSupplierV2_ObserverBatch runs supervisor.py ensure once a minute and at logon. It checks the existing process locks and starts only missing registered services. It preserves workflow STOP controls, observer run/STOP and feeder run/SERVICE_STOP. An old heartbeat with a live lock is reported for inspection; it is not a reason to kill an in-flight send.

YOLLA-LaoPanel-Observer-Panel runs supervisor.py dashboard and opens run/supervision.html. The page refreshes every 10 seconds and distinguishes screen state, supplier results, stale evidence and manual-review alerts. WORKING means a live UI generation signal; it is not proof of GitHub completion. supervisor.py itself never assigns work, sends a prompt, or modifies a worker conversation.

The old WORK-C KIIP-LawZH-Autonomy-WORK-C scheduled task was disabled, and its DB-oriented lifecycle, dispatcher and monitor processes were stopped. Their records and protected holds remain intact. The NOTEX legacy KIIP campaign has control.enabled=false and paused=true; do not reactivate it.

## Reliability changes

- Windows lock probes no longer write a locked byte before attempting the lock.
- A brief latest.json / health.json replacement gap is read again, with a bounded 60-ms total delay, before being treated as inconsistent. Freshness and scope checks still apply.
- Service heartbeat records identify each batch and waiting phase. Failures are included in bounded supplier history.
- A benign later pause can get a new continuation episode only after receipt and working were seen, a later assistant count was observed, at least 90 seconds elapsed, and no error/hold wording appears. Maximum three episodes per assignment. Uncertain transmissions and repeated errors are not replayed.
- Transport errors cannot look like an idle ready worker.
- A new assignment resets elapsed-state continuity.
- The distinct Start a new chat recovery control is classified as CONVERSATION_RECOVERY_REQUIRED. The ordinary New chat navigation button does not trigger it. Inspect the actual visible error before replacing a conversation.
- Work-mode collapsed message lines are parsed with explicit field boundaries, so a branch name cannot absorb the next Korean label.

## Technical conversation handoff

Do not replace a generating worker or bypass any approval, login, quota, permission, safety, source-change, user-stop or NO_RETRY condition. Read the actual current error first. Preserve old URL, assignment, source hashes, results and dispatch history. Persist handoff intent before touching the UI. A copied automation draft may be cleared only after its full hash matches its recorded prompt and the draft is archived. Never clear a user draft.

Send one handoff prompt for the same worker and entire law family. Verify the new conversation URL and matching last-user assignment marker, then update only that worker binding with compare-and-swap semantics. A canceled transport call may still have delivered; inspect before taking any further action.

N06's confirmed Unknown error was recovered this way without changing its job. Its handoff receipt is status/delivery/handoff-S2-f02f1a93d9ac0edfc8745d0d01d75686.json in the NOTEX data repository. Its new conversation is https://chatgpt.com/c/6aee7c0e-3254-83e8-a39f-73d7fd213f99 . The supplier still validates both review and receipt before freeing the worker.

The official panel.observe output can omit the live error banner and retain a prior assistant response. The existing registered kiip_support/panel_dom.py PanelDomReader.read can read visible current-turn text and page alerts from the exact already-observed local WebView target. It must not be used to activate old dispatch logic or clear protected controls.

## Validation and rollback

Feeder: python -m unittest discover -s feeder -p test_*.py (56 tests).
Observer: python -m unittest discover -s feeder/observer_runtime -p test_observer.py (29 tests).
WORK-C repository checks: python -m unittest discover -s tests -v (14 tests); python scripts/validate.py (template validation only, not a validation of all live law reviews).
The legacy feeder test uses an isolated fixture and never invokes the enabled production configuration.

Each PC retains timestamped runtime code and scheduled-task XML backups. Restore only the intended versions after checking STOP, process identity and the batch lock. Never roll back law-review results or source files as part of a controller rollback.

## Stable completion of an already typed draft

verified_draft.py reads only the exact registered local WebView matching the bound conversation URL. It verifies the entire visible editor text against the expected supplier prompt, checks that the assignment marker has not already appeared as the last user message, rejects live stop/approval/login/recovery/error controls, and identifies the currently visible Send button by its label. It then clicks once. A mutable panel element number is no longer used for the fallback send action.

The helper never types or clears content and never opens a conversation. An ambiguous target, mismatched full draft, blocked or obscured button causes a hold. The normal supplier persists send intent first and verifies receipt afterward. The tests exercise 9 DOM scenarios, including the full draft mismatch, already-received marker, generating UI, approval, permission denial, duplicate buttons and obscured controls.
