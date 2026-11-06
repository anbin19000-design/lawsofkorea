import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "feeder" / "github_instruction_feeder.py"
PY = sys.executable


def run(*args):
    return subprocess.run([PY, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def counts():
    return {name: len(list((ROOT / "status" / name).glob("*.json"))) for name in ("active", "claims", "delivery")}


before = counts()
v = run("validate")
assert v.returncode == 0, v.stderr
vd = json.loads(v.stdout)
assert vd["valid"] is True, vd
assert vd["enabled"] is False
assert vd["mode"] == "PREPARE_ONLY"
assert vd["dispatch_guard_locked"] is True
assert vd["manifest_jobs"] == vd["paired_jobs"] > 0
p = run("preview", "--limit", "6")
assert p.returncode == 0, p.stderr
pd = json.loads(p.stdout)
assert pd["mutated"] is False and pd["supplied"] is False
assert len(pd["assignments"]) == 6
g = run("run-once")
assert g.returncode != 0
assert "FEEDER_DISABLED" in g.stderr
assert counts() == before == {"active": 0, "claims": 0, "delivery": 0}
print(json.dumps({"ok": True, "no_supply": True, "manifest_jobs": vd["manifest_jobs"], "workers": vd["workers"]}, ensure_ascii=False))
