@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set WORKC_GITHUB_FEEDER_LIVE=1
"C:\Program Files\Python313\python.exe" "%~dp0github_instruction_feeder.py" service --confirm ENABLE_WORKC_GITHUB_DISPATCH_V1
