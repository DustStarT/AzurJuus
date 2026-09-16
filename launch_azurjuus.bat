@echo off
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo Run Python 3.11-3.13: python tools\setup_runtime.py
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 desktop.py
if errorlevel 1 (
  echo.
  echo AzurJuus desktop failed to start.
  pause
)
