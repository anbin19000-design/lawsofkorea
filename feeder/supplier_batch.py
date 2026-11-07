#!/usr/bin/env python3
"""One scheduled batch for every worker, driven by the existing passive observer."""
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path
import time

import event_supplier as supplier
import batch_engine


@contextlib.contextmanager
def batch_lock(path):
    with open(path, "a+b") as handle:
        handle.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def history_record(run, status):
    history = run / "supplier-history.jsonl"
    if history.exists() and history.stat().st_size > 2_000_000:
        history.replace(run / "supplier-history.previous.jsonl")
    with history.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(status, ensure_ascii=True) + "\n")


def main(root=None):
    root = Path(root) if root else Path(__file__).resolve().parent
    run = root / "run"
    run.mkdir(exist_ok=True)
    with batch_lock(run / "supplier-batch.lock") as acquired:
        if not acquired:
            print('{"status":"BATCH_ALREADY_RUNNING","sent":false}')
            return 0
        started = time.time()
        status = {"schema": "LAW_SUPPLIER_BATCH_V1", "version": supplier.VERSION,
                  "pid": os.getpid(), "started_at": supplier.iso(), "interval_seconds": 10,
                  "status": "RUNNING", "observer_driven": True}
        atomic(run / "supplier-latest.json", status)
        try:
            config = supplier.json_file(root / "supplier-config.json")
            config["trace_path"] = str(run / "git-progress.json")
            supplier.check(config.get("enabled") is True, "SUPPLIER_DISABLED")
            supplier.require_unstopped(config)
            result = batch_engine.execute(config, supplier.GitStore(config))
            status.update(result, status="COMPLETED", finished_at=supplier.iso(),
                          elapsed_seconds=round(time.time() - started, 3))
            atomic(run / "supplier-latest.json", status)
            history_record(run, status)
            print(json.dumps(status, ensure_ascii=True))
            return 0
        except Exception as exc:
            status.update(status="HOLD", finished_at=supplier.iso(), sent=False,
                          error=str(exc) if isinstance(exc, supplier.SupplyError) else type(exc).__name__)
            atomic(run / "supplier-latest.json", status)
            history_record(run, status)
            print(json.dumps(status, ensure_ascii=True))
            return 2


def service():
    import supervisor
    root = Path(__file__).resolve().parent
    run = root / "run"
    run.mkdir(exist_ok=True)
    with batch_lock(run / "supplier-service.lock") as acquired:
        if not acquired:
            return 0
        boot = supplier.iso()
        sequence = 0
        while not (run / "SERVICE_STOP").exists():
            started = time.monotonic(); sequence += 1
            heartbeat = {"pid": os.getpid(), "started_at": boot, "version": supplier.VERSION,
                         "interval_seconds": 10, "sequence": sequence,
                         "heartbeat_at": supplier.iso(), "phase": "BATCH"}
            atomic(run / "supplier-service.json", heartbeat)
            main(root)
            heartbeat.update(heartbeat_at=supplier.iso(), phase="WAITING",
                             elapsed_seconds=round(time.monotonic() - started, 3))
            atomic(run / "supplier-service.json", heartbeat)
            try:
                supervisor.refresh_dashboard(root)
            except Exception as exc:
                atomic(run / "dashboard-error.json", {"at": supplier.iso(), "error": type(exc).__name__})
            while time.monotonic() - started < 10 and not (run / "SERVICE_STOP").exists():
                time.sleep(0.5)
        atomic(run / "supplier-service.json", {"pid": os.getpid(), "phase": "STOPPED",
                                               "heartbeat_at": supplier.iso(), "version": supplier.VERSION})


if __name__ == "__main__":
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        raise SystemExit(service() if "--service" in sys.argv else main())
    except Exception:
        log = Path(__file__).resolve().parent / "run" / "service-error.log"
        with log.open("a", encoding="utf-8") as stream:
            stream.write(traceback.format_exc() + "\n")
        raise

