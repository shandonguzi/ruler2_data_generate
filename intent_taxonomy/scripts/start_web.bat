@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

echo [Intent Taxonomy Engine]
echo Project: %CD%

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  where uv >nul 2>&1
  if not errorlevel 1 (
    echo Creating .venv with uv...
    uv venv .venv
  ) else (
    echo Creating .venv with Python...
    python -m venv .venv
  )
)

if not exist "%PY%" (
  echo Failed to create .venv.
  pause
  exit /b 1
)

"%PY%" -c "import importlib.util,sys,time; t=time.perf_counter(); missing=[m for m in ['openai','pandas','openpyxl','pydantic','yaml','tqdm','fastapi','uvicorn','multipart','json_repair'] if importlib.util.find_spec(m) is None]; print('[Startup] Dependency check: {:.3f} seconds'.format(time.perf_counter()-t),flush=True); sys.exit(bool(missing))" 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  "%PY%" -c "import subprocess,sys,time; t=time.perf_counter(); result=subprocess.call([sys.executable,'-m','pip','install','-r','requirements.txt']); print('[Startup] Dependency installation: {:.3f} seconds'.format(time.perf_counter()-t),flush=True); sys.exit(result)"
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
)

"%PY%" bootstrap.py
if errorlevel 1 (
  pause
  exit /b 1
)

echo Opening http://127.0.0.1:7860 ...
"%PY%" web_server.py
pause
