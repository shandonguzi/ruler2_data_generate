$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location $taskRoot

$py = Join-Path $taskRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv .venv
  } else {
    python -m venv .venv
  }
}
if (-not (Test-Path $py)) { throw "Failed to create .venv" }

& $py -c "import importlib.util,sys,time; t=time.perf_counter(); missing=[m for m in ['openai','pandas','openpyxl','pydantic','yaml','tqdm','fastapi','uvicorn','multipart','json_repair'] if importlib.util.find_spec(m) is None]; print('[Startup] Dependency check: {:.3f} seconds'.format(time.perf_counter()-t),flush=True); sys.exit(bool(missing))" 2>$null
if ($LASTEXITCODE -ne 0) {
  & $py -c "import subprocess,sys,time; t=time.perf_counter(); result=subprocess.call([sys.executable,'-m','pip','install','-r','requirements.txt']); print('[Startup] Dependency installation: {:.3f} seconds'.format(time.perf_counter()-t),flush=True); sys.exit(result)"
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $py bootstrap.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py web_server.py
