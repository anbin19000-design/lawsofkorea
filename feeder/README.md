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
