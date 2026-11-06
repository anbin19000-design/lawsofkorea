# WORK-C GitHub Instruction Feeder

Installed disabled. It reads the WORK-C assignment manifest and law files from this GitHub working tree.

Three locks are required before live supply:
1. `feeder/config.json`: `enabled=true`, `mode=LIVE`;
2. environment `WORKC_GITHUB_FEEDER_LIVE=1`;
3. service confirmation `ENABLE_WORKC_GITHUB_DISPATCH_V1`.

Safe commands do not supply work:
```powershell
python feeder/github_instruction_feeder.py validate --write-report
python feeder/github_instruction_feeder.py preview --limit 6
```

The Windows task `WORKC_GitHub_Law_Instruction_Feeder` is installed disabled with a one-minute trigger.

## English and semantic references

Each assignment has `laws/en/<job_id>.json`. It links rows to Korean-English pairs and to shared English law documents under `documents/en/`. Reviewed row references are under `reviewed/en/`; semantic evidence and H-language renderings are under `meaning/pairs/`. Korean remains authoritative. Family-version fallbacks and semantic evidence are references, not automatic approvals.
