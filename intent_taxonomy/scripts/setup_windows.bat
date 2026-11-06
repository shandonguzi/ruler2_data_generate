@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  where uv >nul 2>&1
  if not errorlevel 1 (uv venv .venv) else (python -m venv .venv)
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe bootstrap.py
pause
