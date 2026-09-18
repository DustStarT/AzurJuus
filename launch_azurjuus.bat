@echo off
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo Run Python 3.11-3.13: python tools\setup_runtime.py
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -X utf8 tools\desktop_entry.py
exit /b
