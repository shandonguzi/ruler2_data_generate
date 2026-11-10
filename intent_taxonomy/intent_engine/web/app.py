from __future__ import annotations

import json
import codecs
import math
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from intent_engine.infrastructure.startup_timing import timed, report, STARTED
_DEPENDENCIES_STARTED = time.perf_counter()

import pandas as pd
import uvicorn
from fastapi import Request, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from intent_engine.settings.config import load_config, model_settings_snapshot, save_model_settings
from intent_engine.infrastructure.llm_client import LocalLLM, _ensure_local_no_proxy
from intent_engine.infrastructure.progress import PROGRESS_PREFIX, ProgressState
from intent_engine.infrastructure.excel_io import INPUT_EXTENSIONS, inspect_input_file, read_input_table
from intent_engine.infrastructure.model_requests import UsageLedger, USAGE_PREFIX, request_extra_body, tracked_request, response_usage

from intent_engine.paths import PROJECT_ROOT

report("Web 第三方及业务依赖加载", _DEPENDENCIES_STARTED)
ROOT = PROJECT_ROOT
CONFIG_PATH = ROOT / "config" / "config.yaml"
WEB_DIR = ROOT / "web"
UPLOAD_DIR = ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_ensure_local_no_proxy()


@dataclass
class Job:
    job_id: str
    job_type: str
    command: list[str]
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    process: Any = field(default=None, repr=False)
    progress: ProgressState = field(default_factory=ProgressState)
    token_usage: UsageLedger = field(default_factory=UsageLedger)
    stage_usage: dict[str, UsageLedger] = field(default_factory=dict)
    log_start_offset: int = 0
    workflow_run_id: str | None = None
    history_log_path: Path | None = field(default=None, repr=False)
    logs_loaded: bool = field(default=True, repr=False)
    log_lock: Any = field(default_factory=threading.Lock, repr=False)

    def load_logs(self):
        with self.log_lock:
            if self.logs_loaded:
                return
            from intent_engine.infrastructure.log_tail import read_log_tail
            total = self.log_start_offset
            self.logs = read_log_tail(self.history_log_path) if self.history_log_path and self.history_log_path.exists() else []
            self.log_start_offset = max(0,total-len(self.logs))
            self.logs_loaded = True

    def to_dict(self, include_logs: bool = False, offset: int = 0) -> dict[str, Any]:
        if include_logs:
            self.load_logs()
        d = {
            "job_id": self.job_id,
            "workflow_run_id": self.workflow_run_id,
            "job_type": self.job_type,
            "command": self.command,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "error": self.error,
            "log_count": self.log_start_offset + len(self.logs),
            "progress": self.progress.snapshot(self.status),
            "token_usage": self.token_usage.snapshot(),
            "stage_usage": {key: ledger.snapshot() for key, ledger in self.stage_usage.items()},
            "elapsed_seconds": max(0, (self.finished_at or time.time()) - self.started_at) if self.started_at else 0,
        }
        if include_logs:
            start=max(0,offset-self.log_start_offset,len(self.logs)-500)
            d["logs"] = self.logs[start:]
            d["logs_skipped"] = max(0,self.log_start_offset+start-offset)
            d["next_offset"] = self.log_start_offset + len(self.logs)
        return d


class JobManager:
    def __init__(self, history_dir: Path | None = None):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.history_dir = Path(history_dir) if history_dir is not None else None
        if self.history_dir:
            with timed("历史任务摘要恢复"):
                self._restore()
            print(f"[启动] 已恢复 {len(self.jobs)} 个历史任务，日志按需加载",flush=True)

    def _save(self, job: Job):
        if self.history_dir is None:
            return
        directory = self.history_dir / job.job_id
        directory.mkdir(parents=True, exist_ok=True)
        state = job.to_dict()
        state["progress_state"] = vars(job.progress)
        state["log_start_offset"] = job.log_start_offset
        from intent_engine.infrastructure.atomic_json import write_json_atomically
        write_json_atomically(directory / "state.json", state, sync=True)


    def _restore(self):
        import re
        for path in self.history_dir.glob("*/state.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                job_id = state["job_id"]
                if not re.fullmatch(r"[a-f0-9]{12}", job_id) or path.parent.name != job_id:
                    continue
                job = Job(**{key: state[key] for key in ("job_id", "job_type", "command", "status", "created_at", "started_at", "finished_at", "returncode", "error")})
                job.workflow_run_id = state.get("workflow_run_id")
                progress = state["progress_state"]
                job.progress = ProgressState(**progress)
                job.token_usage = UsageLedger.from_snapshot(state["token_usage"])
                job.stage_usage = {key: UsageLedger.from_snapshot(value) for key, value in state["stage_usage"].items()}
                job.history_log_path = path.parent / "logs.jsonl"
                job.logs_loaded = False
                job.log_start_offset = max(0,state.get("log_count",0))
                interrupted = job.status in {"queued", "running", "paused"}
                if job.status in {"queued", "running", "paused"}:
                    job.status = "failed"
                    job.error = "服务已重启，无法继续追踪此任务；任务未自动恢复执行。"
                    job.finished_at = time.time()
                self.jobs[job_id] = job
                if interrupted:
                    self._save(job)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue  # One damaged record must not prevent service startup.


    @contextmanager
    def taxonomy_edit(self):
        # Share the job reservation lock across version check and file writes.
        # This serializes saves and prevents a pipeline starting mid-save.
        with self.lock:
            active = [j for j in self.jobs.values() if j.status in {"queued", "running", "paused"}]
            if active:
                job = active[0]
                raise HTTPException(409, f"任务 {job.job_id} ({job.job_type}) 正在运行，完成后可修改体系或模型配置")
            yield

    @contextmanager
    def workflow_cleanup(self, run_id):
        from intent_engine.workflows.workflow_cleanup import ensure_workflow_not_in_use, WorkflowInUseError
        with self.lock:
            try:
                ensure_workflow_not_in_use(_reports_dir(), run_id, self.jobs.values())
            except WorkflowInUseError as exc:
                raise HTTPException(409, str(exc)) from exc
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from exc
            yield

    def start(self, job_type: str, command: list[str]) -> Job:
        with self.lock:
            active = [j for j in self.jobs.values() if j.status in {"queued", "running", "paused"}]
            if active:
                raise RuntimeError(f"Another job is already running: {active[0].job_id} ({active[0].job_type})")
            job = Job(job_id=uuid.uuid4().hex[:12], job_type=job_type, command=command)
            self.jobs[job.job_id] = job
            self._save(job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _append(self, job: Job, text: str):
        text = text.rstrip()
        if not text:
            return
        with self.lock:
            if text.startswith("@@INTENT_TRACE@@"):
                try:
                    event = json.loads(text[len("@@INTENT_TRACE@@"):])
                    event["stage_id"] = job.progress.current_id or "__unassigned__"
                    root = PROJECT_ROOT / "data" / "request_traces" / job.job_id
                    root.mkdir(parents=True, exist_ok=True)
                    with (root / "requests.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    from intent_engine.infrastructure.request_trace_store import prepare_request_index
                    prepare_request_index(root / "requests.jsonl")
                    run_id = event.get("run_id")
                    if run_id:
                        job.workflow_run_id = run_id
                        from intent_engine.workflows.workflow_layout import workflow_paths, check_no_symlinks
                        target = workflow_paths(_reports_dir(), run_id)["manifest"].parent / "requests" / "requests.jsonl"
                        check_no_symlinks(target, _reports_dir())
                        if target.parent.parent.is_dir():
                            target.parent.mkdir(exist_ok=True)
                            with target.open("a", encoding="utf-8") as f:
                                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                            prepare_request_index(target)
                    return
                except (ValueError, OSError):
                    return
            if text.startswith(USAGE_PREFIX):
                try:
                    event = json.loads(text[len(USAGE_PREFIX):])
                    if event.get("kind") not in {"llm", "embedding"} or not isinstance(event.get("model"), str):
                        raise ValueError("invalid usage event")
                    job.token_usage.apply(event)
                    # Usage is emitted before each request returns; sequential stages
                    # wait for their workers, so stream order identifies its phase.
                    current = next((s for s in job.progress.steps if s["id"] == job.progress.current_id), None)
                    stage_id = current["id"] if current and current["status"] == "running" else "__unassigned__"
                    job.stage_usage.setdefault(stage_id, UsageLedger()).apply(event)
                    self._save(job)
                    return
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            if text.startswith(PROGRESS_PREFIX):
                try:
                    event = json.loads(text[len(PROGRESS_PREFIX):])
                    if isinstance(event, dict) and job.progress.apply(event):
                        if event.get("event") == "start":
                            job.stage_usage.setdefault(event["id"], UsageLedger())
                        self._save(job)
                        return
                except (ValueError, TypeError, OverflowError):
                    pass  # Preserve malformed telemetry as a diagnostic log.
            job.load_logs()
            job.logs.append(text)
            if self.history_dir:
                directory = self.history_dir / job.job_id
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "logs.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(text, ensure_ascii=False) + "\n")
            # Bound memory for very long runs.
            if len(job.logs) > 20000:
                job.log_start_offset += len(job.logs) - 15000
                job.logs = job.logs[-15000:]
            self._save(job)

    def _run(self, job: Job):
        env = os.environ.copy()
        # Force child Python (main.py / tqdm / print) to emit UTF-8 even on
        # Chinese Windows where redirected stdout would otherwise use GBK/CP936.
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["INTENT_PROGRESS"] = "1"
        env["INTENT_JOB_ID"] = job.job_id
        marker = self.history_dir / job.job_id / "pause" if self.history_dir else None
        if marker is not None:
            env["INTENT_JOB_PAUSE_FILE"] = str(marker.resolve())
        job.status = "running"
        job.started_at = time.time()
        self._append(job, "$ " + " ".join(job.command))
        try:
            proc = subprocess.Popen(
                job.command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=0,
            )
            job.process = proc
            assert proc.stdout is not None
            buffer = ""
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
                buffer += decoder.decode(chunk)
                # tqdm commonly refreshes with carriage returns rather than newlines.
                while "\n" in buffer or "\r" in buffer:
                    n = buffer.find("\n") if "\n" in buffer else 10**9
                    r = buffer.find("\r") if "\r" in buffer else 10**9
                    pos = min(n, r)
                    part = buffer[:pos]
                    buffer = buffer[pos + 1 :]
                    if part.strip():
                        self._append(job, part)
            buffer += decoder.decode(b"", final=True)
            if buffer.strip():
                self._append(job, buffer)
            job.returncode = proc.wait()
            if job.status != "cancelled":
                job.status = "succeeded" if job.returncode == 0 else "failed"
        except Exception as e:  # pragma: no cover - defensive for local environments
            if job.status != "cancelled":
                job.status = "failed"
            job.error = repr(e)
            self._append(job, f"[WEB ERROR] {e}")
        finally:
            job.process = None
            job.finished_at = time.time()
            self.sync_workflow_status(job)
            with self.lock:
                if marker is not None:
                    marker.unlink(missing_ok=True)
                if self.jobs.get(job.job_id) is job:
                    self._save(job)

    def sync_workflow_status(self, job):
        """Reconcile only this job's unfinished manifest, preserving saved results."""
        if job.status not in {"cancelled", "failed"} or job.finished_at is None:
            return
        from intent_engine.workflows.workflow_layout import find_manifest
        from intent_engine.infrastructure.atomic_json import write_json_atomically
        if not job.workflow_run_id:
            return
        try:
            path = find_manifest(_reports_dir(), job.workflow_run_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("job_id") != job.job_id or data.get("status") not in {"running", "queued"}:
                return
            data.update(status=job.status, finished_at=job.finished_at)
            write_json_atomically(path, data)
        except (OSError, ValueError, KeyError) as exc:
            print(f"[WEB] Unable to synchronize workflow status: {exc}", flush=True)

    def get(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job

    def set_paused(self, job_id: str, paused: bool) -> Job:
        with self.lock:
            job = self.jobs[job_id]
            process = job.process
            if self.history_dir is None or process is None or process.poll() is not None:
                raise ValueError("任务进程不可用")
            if job.status not in {"running", "paused"}:
                raise ValueError("仅运行中的任务可暂停或继续")
            marker = self.history_dir / job_id / "pause"
            if paused:
                marker.touch()
            else:
                marker.unlink(missing_ok=True)
            job.status = "paused" if paused else "running"
            self._save(job)
            return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        proc = job.process
        if job.status not in {"queued", "running", "paused"} or proc is None:
            return job
        self._append(job, "[WEB] Cancel requested by user")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        finally:
            job.status = "cancelled"
            job.returncode = proc.returncode
            job.finished_at = time.time()
            self.sync_workflow_status(job)
            with self.lock:
                if self.jobs.get(job.job_id) is job:
                    self._save(job)
        return job

    def recent(self, limit: int = 20) -> list[Job]:
        with self.lock:
            jobs = list(self.jobs.values())
        return sorted(jobs, key=lambda x: x.created_at, reverse=True)[:limit]


jobs = JobManager(PROJECT_ROOT / "data" / "job_history")
_ROUTES_STARTED = time.perf_counter()
from contextlib import asynccontextmanager


@asynccontextmanager
async def startup_lifespan(app):
    started = getattr(app.state,"http_start_time",time.perf_counter())
    print("[启动] 正在初始化 ASGI 服务…", flush=True)
    report("ASGI 启动初始化", started)
    report("Web 进程初始化累计（不含启动脚本）", STARTED)
    print("[启动] 应用初始化完成，等待服务器绑定监听端口", flush=True)
    yield


app = FastAPI(title="Intent Taxonomy Console", version="1.4", lifespan=startup_lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")




class DiscoveryRequest(BaseModel):
    input: str
    text_column: str | None = None
    sheet_name: str | None = None
    limit: int | None = Field(default=None, ge=1)
    workers: int = Field(default=4, ge=1, le=32)
    review: bool = True
    iteration_rounds: int = Field(default=0, ge=-1, le=10)
    iteration_scope: Literal["all", "unresolved_only", "noise_only"] | None = None


class WorkflowDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)


class WorkflowStageRequest(BaseModel):
    append_input: str | None = None
    text_column: str | None = None
    sheet_name: str | None = None
    stage: Literal["extract", "cluster", "review", "pairs", "domains", "iterate", "export"]
    iteration_rounds: int = Field(default=3, ge=-1, le=10)

    @field_validator('iteration_rounds')
    @classmethod
    def nonzero_rounds(cls,value):
        if value==0:raise ValueError('独立迭代请选择自动或1–10轮')
        return value

    iteration_scope: Literal["all", "unresolved_only", "noise_only"] | None = None
    run_id: str | None = None
    artifact: str | None = None
    workers: int = Field(default=4, ge=1, le=32)
    use_external: bool = False
    external_source: str | None = None


class WorkflowTaxonomyEditRequest(BaseModel):
    run_id: str
    artifact: str
    revision: str
    tasks: list[dict[str, Any]]
    domains: list[dict[str, Any]]


class ModelEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2048)
    model: str = Field(max_length=256)
    api_key_env: str = Field(default="", max_length=128, pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    extra_body: dict[str, Any] | None = None
    thinking_mode: Literal["default", "on", "off"] | None = None
    thinking_parameter: str | None = Field(default=None, max_length=256)

    @field_validator("base_url", "model", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        if value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment or any(c.isspace() for c in value):
                raise ValueError("API 地址应为有效的 http/https 地址，不包含账号、密码、查询参数或片段")
            try:
                url.port
            except ValueError as exc:
                raise ValueError("API 端口无效") from exc
        return value


class ModelSettingsRequest(BaseModel):
    extra_llms: list[ModelEndpointRequest] | None = Field(default=None,max_length=16)
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    llm: ModelEndpointRequest
    embedding: ModelEndpointRequest


















def _cfg() -> dict:
    cfg = load_config(str(CONFIG_PATH))
    _ensure_local_no_proxy(*((cfg.get(section) or {}).get("base_url") for section in ("llm", "embedding")))
    return cfg


def _reports_dir() -> Path:
    return Path(_cfg()["paths"]["reports_dir"])






def _safe_report_path(value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    allowed = [ROOT.resolve(), _reports_dir().resolve(), UPLOAD_DIR.resolve()]
    if not any(p == a or a in p.parents for a in allowed):
        raise HTTPException(403, "Only project/report/upload files can be opened from the web console")
    return p


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _file_meta(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "name": path.name,
        "path": _rel(path),
        "size": st.st_size,
        "modified": st.st_mtime,
        "modified_text": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _json_safe(value: Any) -> Any:
    """Recursively convert pandas/numpy missing values and non-finite floats to JSON-safe values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is pd.NA:
        return None

    # numpy scalar -> native Python scalar. pandas uses numpy underneath,
    # so this also handles np.float64(np.nan), np.int64, etc.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            native = item()
            if native is not value:
                return _json_safe(native)
        except (TypeError, ValueError):
            pass

    # Covers NaT and scalar missing values not handled above. Avoid calling
    # pd.isna on containers because it returns an array.
    if not isinstance(value, (str, bytes)):
        try:
            missing = pd.isna(value)
            if isinstance(missing, bool) and missing:
                return None
        except (TypeError, ValueError):
            pass
    return value


def _report_files(pattern="*"):
    from intent_engine.workflows.workflow_layout import check_no_symlinks
    reports = _reports_dir()
    reports.mkdir(parents=True, exist_ok=True)
    files = []
    for path in reports.rglob(pattern):
        if any(part.startswith(".") for part in path.relative_to(reports).parts):
            continue
        try:
            check_no_symlinks(path, reports)
            if path.is_file():
                files.append(path)
        except ValueError:
            continue
    return files


def _list_reports(pattern: str = "*") -> list[dict[str, Any]]:
    files = sorted(_report_files(pattern), key=lambda p:p.stat().st_mtime, reverse=True)
    reports = _reports_dir()
    return [{**_file_meta(p), "name": str(p.relative_to(reports))} for p in files]


def _latest(pattern: str) -> Path | None:
    return max(_report_files(pattern), key=lambda p:p.stat().st_mtime, default=None)






@app.get("/", response_class=HTMLResponse)
def home():
    index = WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>web/index.html missing</h1>", status_code=500)
    import hashlib
    import re
    html = index.read_text(encoding="utf-8")
    def versioned(match):
        url = match.group(1)
        path = WEB_DIR / url.lstrip("/")
        if not path.is_file():
            return match.group(0)
        version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        return f'"{url}?v={version}"'
    html = re.sub(r'"(/static/[^"?]+\.(?:js|css))"', versioned, html)
    return HTMLResponse(html, headers={"Cache-Control":"no-cache"})


@app.get("/api/status")
def api_status():
    cfg = _cfg()
    from intent_engine.workflows.workflow_artifacts import list_workflows
    return {"llm": {key: cfg["llm"].get(key) for key in ("base_url", "model", "max_workers")},
            "workflows": {"count": len(list_workflows(_reports_dir()))},
            "reports": {"count": len(_list_reports())}}



class TopicLanguageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ngram_range: list[int]
    use_stop_words: bool = True
    stop_words: list[str] = Field(default_factory=list, max_length=2000)


class ClusteringSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: Literal["english", "chinese"]
    languages: dict[str, TopicLanguageSettings]
    flat_topics: bool | None = None


@app.get("/api/clustering-settings")
def api_clustering_settings():
    from intent_engine.settings.clustering_settings import clustering_settings_snapshot
    return clustering_settings_snapshot(CONFIG_PATH)


@app.post("/api/clustering-settings")
def api_save_clustering_settings(req: ClusteringSettingsRequest):
    from intent_engine.settings.clustering_settings import save_clustering_settings
    with jobs.taxonomy_edit():
        try:
            return save_clustering_settings(CONFIG_PATH, req.revision, req.language,
                                            {key:value.model_dump() for key,value in req.languages.items()}, req.flat_topics)
        except ValueError as exc:
            raise HTTPException(409 if "已变更" in str(exc) else 400, str(exc)) from exc


class PromptSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompts: dict[str, str | None]


@app.get("/api/prompt-settings")
def api_prompt_settings():
    from intent_engine.settings.prompt_settings import prompt_settings_snapshot
    try:
        return prompt_settings_snapshot(CONFIG_PATH)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/prompt-settings")
def api_save_prompt_settings(req: PromptSettingsRequest):
    from intent_engine.settings.prompt_settings import save_prompt_settings
    with jobs.taxonomy_edit():
        try:
            return save_prompt_settings(CONFIG_PATH, req.revision, req.prompts)
        except ValueError as exc:
            raise HTTPException(409 if "已变更" in str(exc) else 400, str(exc)) from exc


@app.get("/api/model-settings")
def api_model_settings():
    return model_settings_snapshot(CONFIG_PATH)


@app.post("/api/model-settings/check/{kind}")
def api_check_model(kind: Literal["llm", "embedding"], req: ModelEndpointRequest):
    from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError
    if not req.base_url or not req.model or any(ord(c) < 32 for c in req.model):
        raise HTTPException(400, "请填写有效的 API 地址和模型名")
    supplied_key = req.api_key.get_secret_value() if req.api_key else ""
    if supplied_key and req.clear_api_key:
        raise HTTPException(400, "清除 API Key 和填写新 Key 不能同时选择")
    saved = _cfg().get(kind, {})
    request_cfg = {**saved, "base_url": req.base_url, "model": req.model}
    for name in ("extra_body", "thinking_mode", "thinking_parameter"):
        value = getattr(req, name)
        if value is not None:
            request_cfg[name] = value
    try:
        body = request_extra_body(request_cfg, thinking=kind == "llm")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    env_name = req.api_key_env or ("INTENT_LLM_API_KEY" if kind == "llm" else "INTENT_EMBEDDING_API_KEY")
    key = os.environ.get(env_name) or supplied_key or ("" if req.clear_api_key else saved.get("api_key")) or "EMPTY"
    _ensure_local_no_proxy(req.base_url)
    started = time.monotonic()
    try:
        with OpenAI(base_url=req.base_url, api_key=key, timeout=15, max_retries=0) as client:
            if kind == "llm":
                kwargs = {"model": req.model, "messages": [{"role": "user", "content": "请只回复 OK"}], "max_tokens": 256}
                if body:
                    kwargs["extra_body"] = body
                response = tracked_request("llm", request_cfg, client.chat.completions.create, **kwargs)
                if not response.choices or not (response.choices[0].message.content or "").strip():
                    raise HTTPException(502, "对话接口返回空内容，请检查模型输出或推理配置")
                extra = {}
            else:
                kwargs = {"model": req.model, "input": ["原子意图模型连接测试"]}
                if saved.get("dimensions"):
                    kwargs["dimensions"] = int(saved["dimensions"])
                if body:
                    kwargs["extra_body"] = body
                response = tracked_request("embedding", request_cfg, client.embeddings.create, **kwargs)
                if len(response.data) != 1 or response.data[0].index != 0:
                    raise HTTPException(502, "Embedding 返回数量或索引异常")
                vector = response.data[0].embedding
                if not vector or not all(math.isfinite(float(value)) for value in vector) or not any(vector):
                    raise HTTPException(502, "Embedding 返回无效向量")
                extra = {"dimensions": len(vector)}
    except HTTPException:
        raise
    except APITimeoutError:
        raise HTTPException(504, "检测超时（15 秒），请确认服务已启动或稍后重试") from None
    except APIConnectionError:
        raise HTTPException(502, "无法连接模型服务，请检查 API 地址、端口和网络") from None
    except APIStatusError as exc:
        # Do not return provider bodies, which can contain credentials or request text.
        messages = {401: "认证失败，请检查 API Key", 403: "访问被拒绝，请检查模型权限",
                    404: "接口或模型不存在，请检查 API 地址和模型名", 429: "服务限流或额度不足"}
        raise HTTPException(502, messages.get(exc.status_code, f"模型服务返回 HTTP {exc.status_code}，请检查服务日志")) from None
    except Exception:
        raise HTTPException(502, "模型服务响应异常，请检查接口格式与服务日志") from None
    return {"ok": True, "kind": kind, "model": req.model,
            "elapsed_ms": round((time.monotonic() - started) * 1000), "usage": response_usage(response, kind), **extra}


@app.post("/api/model-settings")
def api_save_model_settings(req: ModelSettingsRequest):
    updates = {}
    for name in ("llm", "embedding"):
        settings = getattr(req, name)
        if (name == "llm" and (not settings.base_url or not settings.model)) or bool(settings.base_url) != bool(settings.model):
            raise HTTPException(400, "对话模型必须填写 API 地址和模型名；Embedding 可全部留空，或同时填写两项")
        if any(ord(c) < 32 for c in settings.model):
            raise HTTPException(400, "模型名不能包含控制字符")
        values = {key: getattr(settings, key) for key in ("base_url", "model", "api_key_env")}
        if settings.extra_body is not None:
            values["extra_body"] = settings.extra_body
        if name == "llm":
            for key in ("thinking_mode", "thinking_parameter"):
                if getattr(settings, key) is not None:
                    values[key] = getattr(settings, key)
        try:
            existing = _cfg().get(name, {})
            request_extra_body({**existing, **values}, thinking=name == "llm")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        key = settings.api_key.get_secret_value() if settings.api_key else ""
        if settings.clear_api_key and key:
            raise HTTPException(400, "清除 API Key 和填写新 Key 不能同时选择")
        if settings.clear_api_key:
            values["api_key"] = ""
        elif key:
            values["api_key"] = key
        updates[name] = values
    if req.extra_llms is not None:
        endpoints=[]
        for endpoint in req.extra_llms:
            if not endpoint.base_url or not endpoint.model:
                raise HTTPException(400,"额外 LLM 必须填写地址和模型名")
            if endpoint.api_key:
                raise HTTPException(400,"额外 LLM 请通过 API Key 环境变量配置凭据")
            values=endpoint.model_dump(exclude={'api_key','clear_api_key'},exclude_none=True)
            request_extra_body({**_cfg()['llm'],**values})
            endpoints.append(values)
        updates['llm']['endpoints']=endpoints
    # Share the job reservation lock so jobs cannot start halfway through a save.
    with jobs.taxonomy_edit():
        try:
            return save_model_settings(CONFIG_PATH, req.revision, updates)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@app.get("/api/llm-check")
def api_llm_check():
    # Keep this independent of the main LLM wrapper; /models is a cheap connectivity check.
    import urllib.request
    cfg = _cfg()
    base = str(cfg["llm"].get("base_url", "")).rstrip("/")
    url = base + "/models"
    try:
        key = os.environ.get(cfg["llm"].get("api_key_env") or "INTENT_LLM_API_KEY") or cfg["llm"].get("api_key") or "EMPTY"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        ids = [x.get("id") for x in payload.get("data", []) if isinstance(x, dict)] if isinstance(payload, dict) else []
        return {"ok": True, "url": url, "models": ids}
    except Exception as e:
        return JSONResponse({"ok": False, "url": url, "error": str(e)}, status_code=503)


@app.get("/api/reports")
def api_reports():
    return {"all": _list_reports()}












@app.get("/api/table-preview")
def api_table_preview(path: str, sheet: str | None = None, limit: int = 200):
    p = _safe_report_path(path)
    if not p.exists():
        raise HTTPException(404, f"File not found: {p}")
    if p.suffix.lower() not in {".xlsx", ".xls"}:
        raise HTTPException(400, "Preview currently supports Excel only")
    xl = pd.ExcelFile(p)
    selected = sheet if sheet in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(p, sheet_name=selected).head(max(1, min(limit, 1000)))
    # Do not rely on DataFrame.where(..., None) alone: float columns keep a
    # float dtype, so None is coerced straight back to NaN. Sanitize the
    # final Python records recursively before FastAPI serializes them.
    payload = {
        "path": _rel(p),
        "sheets": xl.sheet_names,
        "sheet": selected,
        "rows": df.to_dict(orient="records"),
        "columns": list(df.columns),
    }
    return _json_safe(payload)


@app.get("/api/download")
def api_download(path: str):
    p = _safe_report_path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p, filename=p.name)


def _input_path(value: str) -> Path:
    raw = (value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"\"", "'"}:
        raw = raw[1:-1].strip()
    if not raw:
        raise HTTPException(400, "请选择或上传 .xlsx 或 .csv 文件")
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else ROOT / path).resolve()
    if not path.exists():
        raise HTTPException(400, f"输入文件不存在：{path}")
    if not path.is_file():
        raise HTTPException(400, "输入路径是文件夹，请选择具体的 .xlsx 或 .csv 文件")
    if path.suffix.lower() not in INPUT_EXTENSIONS:
        raise HTTPException(400, "输入文件仅支持 .xlsx、.csv 或 .xls")
    return path


def _validate_input_request(req) -> Path:
    path = _input_path(req.input)
    column = req.text_column if req.text_column is not None else _cfg()["pipeline"]["text_column"]
    try:
        df, _ = read_input_table(path, sheet_name=req.sheet_name, nrows=1)
        if column not in df.columns:
            raise ValueError(f"文件中不存在列 {column!r}，可用列：{list(df.columns)}")
        if df.empty:
            raise ValueError("文件只有列名，没有可分类的数据行")
    except Exception as exc:
        raise HTTPException(400, f"无法读取输入数据：{exc}") from exc
    return path


@app.get("/api/input-preview")
def api_input_preview(path: str, sheet: str | None = None):
    selected = _input_path(path)
    try:
        return _json_safe({"path": _rel(selected), "name": selected.name, **inspect_input_file(selected, sheet)})
    except Exception as exc:
        raise HTTPException(400, f"无法读取文件，请确认第一行为列名：{exc}") from exc


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    name = Path((file.filename or "upload.xlsx").replace("\\", "/")).name
    if Path(name).suffix.lower() not in INPUT_EXTENSIONS:
        raise HTTPException(400, "请选择 .xlsx 或 .csv 文件（也兼容旧版 .xls）")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = UPLOAD_DIR / f"{Path(name).stem}_{stamp}_{uuid.uuid4().hex[:8]}{Path(name).suffix.lower()}"
    content = await file.read()
    try:
        dest.write_bytes(content)
        preview = await run_in_threadpool(inspect_input_file, dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"文件无法读取，请确认格式正确且第一行为列名：{exc}") from exc
    return _json_safe({**_file_meta(dest), "preview": preview})


def _start_job(job_type: str, cmd: list[str]) -> dict[str, Any]:
    try:
        return jobs.start(job_type, cmd).to_dict()
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/discovery-settings")
def api_discovery_settings():
    from intent_engine.atomic.atomic_discovery import embedding_settings, check_dependencies
    from intent_engine.infrastructure.iteration_scope import normalize_iteration_scope
    cfg = _cfg()
    issues = []
    for check in (lambda: embedding_settings(cfg), check_dependencies):
        try:
            check()
        except ValueError as exc:
            issues.append(str(exc))
    settings = cfg.get("embedding", {})
    return {"ready": not issues, "issues": issues,
            "iteration_scope": normalize_iteration_scope(cfg.get("atomic_discovery",{}).get("iteration_scope","unresolved_only")),
            "base_url": settings.get("base_url", ""), "model": settings.get("model", "")}


@app.post("/api/jobs/classification-workflow")
def api_job_classification_workflow(req: DiscoveryRequest):
    # This route promises the complete workflow, including review and hierarchy.
    return api_job_discover(req.model_copy(update={"review": True}))


@app.post("/api/jobs/discover")
def api_job_discover(req: DiscoveryRequest):
    from intent_engine.atomic.atomic_discovery import embedding_settings, check_dependencies
    inp = _validate_input_request(req)
    try:
        embedding_settings(_cfg())
        check_dependencies()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cmd = [sys.executable, "main.py", "discover", "--input", str(inp), "--workers", str(req.workers)]
    if req.text_column:
        cmd += ["--text-column", req.text_column]
    if req.sheet_name is not None:
        cmd += ["--sheet", req.sheet_name]
    if req.limit is not None:
        cmd += ["--limit", str(req.limit)]
    if not req.review:
        cmd.append("--no-review")
    cmd += ["--iteration-rounds", str(req.iteration_rounds)]
    if req.iteration_scope:
        cmd += ["--iteration-scope", req.iteration_scope]
    return _start_job("discover", cmd)


@app.get("/api/workflows")
def api_workflows(brief: bool = False):
    started = time.perf_counter()
    from intent_engine.workflows.workflow_artifacts import list_workflows
    from intent_engine.atomic.atomic_discovery import WORKFLOW_STAGES, STAGE_REQUIRED
    from intent_engine.workflows.workflow_capabilities import workflow_capabilities
    reports = _reports_dir()
    with jobs.lock:
        terminal_jobs = list(jobs.jobs.values())
    for job in terminal_jobs:
        jobs.sync_workflow_status(job)
    runs = list_workflows(reports)
    print(f"[流程清单耗时] {len(runs)} 个版本 | {time.perf_counter()-started:.4f}s | 轻量={brief}", flush=True)
    return {"runs": runs if brief else [{**run, **workflow_capabilities(reports, run)} for run in runs],
            "stages": WORKFLOW_STAGES, "required": STAGE_REQUIRED}


@app.get("/api/workflows/{run_id}/capabilities")
def api_workflow_capabilities(run_id: str):
    from intent_engine.workflows.workflow_artifacts import load_workflow
    from intent_engine.workflows.workflow_capabilities import workflow_capabilities
    try:
        reports=_reports_dir()
        return workflow_capabilities(reports,load_workflow(reports,run_id))
    except (ValueError,OSError,KeyError,TypeError) as exc:
        raise HTTPException(400,str(exc)) from exc


@app.get("/api/workflows/{run_id}/delete-preview")
def api_workflow_delete_preview(run_id: str):
    from intent_engine.workflows.workflow_cleanup import workflow_delete_plan
    with jobs.workflow_cleanup(run_id):
        try:
            from intent_engine.workflows.workflow_artifacts import load_workflow
            from intent_engine.workflows.job_history_cleanup import associated_jobs
            plan = workflow_delete_plan(_reports_dir(), run_id)
            manifest = load_workflow(_reports_dir(), run_id)
            history = associated_jobs(jobs, run_id, manifest.get("job_id"), PROJECT_ROOT / "data" / "request_traces")
            plan["history_jobs"] = [{"job_id": job.job_id, "status": job.status} for job in history]
            return plan
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc


@app.delete("/api/workflows/{run_id}")
def api_delete_workflow(run_id: str, req: WorkflowDeleteRequest):
    from intent_engine.workflows.workflow_cleanup import delete_workflow
    with jobs.workflow_cleanup(run_id):
        try:
            from intent_engine.workflows.workflow_artifacts import load_workflow
            from intent_engine.workflows.job_history_cleanup import associated_jobs, remove_histories
            manifest = load_workflow(_reports_dir(), run_id)
            trace_root = PROJECT_ROOT / "data" / "request_traces"
            history = associated_jobs(jobs, run_id, manifest.get("job_id"), trace_root)
            with remove_histories(jobs, history, trace_root) as cleanup:
                result = delete_workflow(_reports_dir(), run_id, req.revision)
            return {**result, **cleanup}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/workflows/{run_id}/artifact/{artifact}")
def api_workflow_artifact(run_id: str, artifact: str, limit: int = 50):
    from intent_engine.workflows.workflow_artifacts import workflow_artifact
    try:
        path = workflow_artifact(_reports_dir(), run_id, artifact)
        if path.suffix != ".json":
            raise ValueError("请选择阶段 JSON 预览")
        data = json.loads(path.read_text(encoding="utf-8"))
        maximum = max(1, min(limit, 200))
        if artifact == "sentence_results":
            from intent_engine.exporting.sentence_results import sentence_result_tables
            return {"run_id": run_id, "artifact": artifact, "path": _rel(path),
                    "summary": {"rows": len(data.get("records", []))},
                    "tables": {"原句最终结果": sentence_result_tables(data)["原句结果"][:maximum]}}
        tables = {"原句": data.get("rows", [])[:maximum], "原子意图": data.get("intents", [])[:maximum],
                  "簇审核": data.get("clusters", [])[:maximum], "聚类细分": data.get("cluster_refinement", {}).get("history", [])[:maximum], "跨簇建议": data.get("pair_reviews", [])[:maximum],
                  "候选任务": data.get("candidate_taxonomy", {}).get("tasks", [])[:maximum],
                  "交付主题映射": [{"intent_id":key, **value} for key,value in data.get("delivery_topics", {}).get("assignments", {}).items()][:maximum],
                  "覆盖统计": [data.get("delivery_topics", {}).get("coverage", {})],
                  "错误": data.get("errors", [])[:maximum], "Token用量": data.get("token_usage", {}).get("models", [])[:maximum]}
        tables["迭代记录"] = [{key: value for key, value in record.items() if key not in {"taxonomy_before", "taxonomy_after", "assignments", "evidence", "changes"}}
                              for record in data.get("iteration", {}).get("rounds", [])]
        tables["迭代停止指标"]=[{"轮次":r['round'],"单成员簇数量":r.get('cluster_counts',{}).get('singleton_clusters'),
            "噪声意图数量":r.get('cluster_counts',{}).get('noise_intents'),
            "上轮单成员簇数量":(r.get('previous_cluster_counts')or{}).get('singleton_clusters'),
            "上轮噪声意图数量":(r.get('previous_cluster_counts')or{}).get('noise_intents')}
            for r in data.get('iteration',{}).get('rounds',[])]
        if data.get("atomic_task_names"):
            tables["原子意图任务名称"]=data["atomic_task_names"].get("records",[])[:maximum]
        if data.get("scene_mapping"):
            mapping=data["scene_mapping"]
            scene_index={s["scene_id"]:s for s in mapping.get("scenes",[])}
            tables["S4 场景关联"]=[{**r,**{k:scene_index.get(r.get("scene_id"),{}).get(k,"") for k in ("S1","S2","S3","S4")}} for r in mapping.get("records",[])[:maximum]]
            tables["S4 场景目录"]=mapping.get("scenes",[])[:maximum]
            tables["S4 任务汇总"]=mapping.get("scene_tasks",[])[:maximum]
        tables["迭代分类"] = data.get("iteration", {}).get("assignments", [])[:maximum]
        tables["迭代变更"] = [{"round": record["round"], **change} for record in data.get("iteration", {}).get("rounds", []) for change in record["changes"]][:maximum]
        from intent_engine.atomic.topic_hierarchy import hierarchy_export_tables
        tables.update({name: rows[:maximum] for name, rows in hierarchy_export_tables(data).items()})
        from intent_engine.exporting.sentence_results import build_sentence_results, sentence_result_tables
        sentence_result = build_sentence_results(data)
        sentence_result["records"] = sentence_result["records"][:maximum]
        tables["原句最终结果"] = sentence_result_tables(sentence_result)["原句结果"]
        return {"run_id": run_id, "artifact": artifact, "path": _rel(path), "summary": data.get("summary"), "tables": tables}
    except (ValueError, OSError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/workflows/{run_id}/diagnostics")
def api_workflow_diagnostics(run_id: str):
    from intent_engine.workflows.workflow_artifacts import load_workflow, workflow_artifact
    from intent_engine.workflows.workflow_diagnostics import workflow_diagnostics
    try:
        manifest = load_workflow(_reports_dir(), run_id)
        available = manifest.get("artifacts", {})
        from intent_engine.atomic.atomic_discovery import WORKFLOW_STAGES
        key = "report" if "report" in available else next((key for key in reversed(available) if key in WORKFLOW_STAGES), None)
        data = json.loads(workflow_artifact(_reports_dir(), run_id, key).read_text(encoding="utf-8")) if key else manifest
        return workflow_diagnostics({**data, "requested_stage": manifest.get("requested_stage", data.get("requested_stage"))})
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/workflows/{run_id}/visualization/{artifact}")
def api_workflow_visualization(run_id: str, artifact: str, view: str = "intents", cluster: str = "all", topic: str = "all",
                               q: str = "", page: int = 1):
    from intent_engine.workflows.workflow_artifacts import workflow_artifact
    from intent_engine.workflows.workflow_visuals import visualization_page
    try:
        path = workflow_artifact(_reports_dir(), run_id, artifact)
        if path.suffix != ".json":
            raise ValueError("请选择阶段 JSON 查看可视化")
        data = json.loads(path.read_text(encoding="utf-8"))
        return visualization_page(data, view=view, cluster=cluster, topic=topic, query=q, page=page, page_size=5)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/workflows/upload-artifact")
async def api_upload_workflow_artifact(file: UploadFile = File(...)):
    from intent_engine.atomic.atomic_discovery import read_stage_source
    if not (file.filename or "").lower().endswith(".json"):
        raise HTTPException(400, "请选择流程中间结果 JSON")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "中间结果文件不能超过 10 MB")
    path = UPLOAD_DIR / f"stage_source_{uuid.uuid4().hex}.json"
    try:
        path.write_bytes(content)
        await run_in_threadpool(read_stage_source, path, "extract")
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"中间结果格式无效：{exc}") from exc
    return {"path": _rel(path)}


@app.get("/api/workflows/{run_id}/taxonomy/{artifact}")
def api_workflow_taxonomy(run_id: str, artifact: str):
    from intent_engine.workflows.workflow_artifacts import load_workflow_taxonomy
    try:
        return load_workflow_taxonomy(_reports_dir(), run_id, artifact)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/workflows/taxonomy-edit")
def api_edit_workflow_taxonomy(req: WorkflowTaxonomyEditRequest):
    from intent_engine.workflows.workflow_artifacts import save_workflow_taxonomy
    with jobs.taxonomy_edit():
        try:
            return save_workflow_taxonomy(_reports_dir(), req.run_id, req.artifact, req.revision, req.tasks, req.domains)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/workflows/{run_id}/exports")
def api_workflow_exports(run_id: str, artifact: str = "report"):
    from intent_engine.exporting.catalog import export_catalog
    try:
        return export_catalog(_reports_dir(), run_id, artifact)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/workflows/{run_id}/query-atomic-intents")
def api_query_atomic_intents(run_id: str, artifact: str = "report"):
    from intent_engine.workflows.workflow_artifacts import workflow_artifact
    from intent_engine.exporting.query_tasks import query_task_csv, saved_task_records
    try:
        path=workflow_artifact(_reports_dir(),run_id,artifact)
        data=json.loads(path.read_text(encoding="utf-8"))
        records=saved_task_records(data)
        if not records:raise ValueError("该结果没有有效原子意图")
    except (ValueError,OSError,KeyError,TypeError) as e:
        raise HTTPException(400,str(e)) from e
    return Response(content=query_task_csv(records,data.get("rows")).encode("utf-8-sig"),media_type="text/csv",
                    headers={"Content-Disposition":'attachment; filename="query_atomic_intents.csv"',
                             "X-Export-Missing-Names":str(sum(r["status"]=="missing_name" for r in records))})


class SceneMappingRequest(BaseModel):
    run_id: str
    artifact: str
    workers: int = Field(default=4,ge=1,le=32)


@app.post("/api/jobs/atomic-tasks")
def api_atomic_tasks(req: SceneMappingRequest):
    from intent_engine.workflows.workflow_artifacts import workflow_artifact
    from intent_engine.atomic.atomic_discovery import read_stage_source
    try:
        source=workflow_artifact(_reports_dir(),req.run_id,req.artifact)
        read_stage_source(source,'cluster')
    except (ValueError,OSError,KeyError,TypeError) as e:raise HTTPException(400,str(e)) from e
    return _start_job('atomic_tasks',[sys.executable,'main.py','export-atomic-tasks','--source',str(source),'--workers',str(req.workers)])


@app.post("/api/jobs/scene-mapping")
def api_scene_mapping(req: SceneMappingRequest):
    from intent_engine.workflows.workflow_artifacts import workflow_artifact
    from intent_engine.atomic.atomic_discovery import read_stage_source
    from intent_engine.workflows.scene_mapping import load_scenes
    try:
        source=workflow_artifact(_reports_dir(),req.run_id,req.artifact)
        data=read_stage_source(source,'cluster')
        if not any(a.get('status')=='valid' for a in data['intents']):raise ValueError('没有有效原子意图')
        load_scenes()
    except (ValueError,OSError,KeyError,TypeError) as e:raise HTTPException(400,str(e)) from e
    return _start_job('s4_mapping',[sys.executable,'main.py','map-scenes','--source',str(source),'--workers',str(req.workers)])


@app.post("/api/jobs/workflow-stage")
def api_job_workflow_stage(req: WorkflowStageRequest):
    from intent_engine.atomic.atomic_discovery import read_stage_source, embedding_settings, check_dependencies
    from intent_engine.workflows.workflow_artifacts import workflow_artifact
    try:
        if req.use_external:
            if not req.external_source:
                raise ValueError("请在高级设置中选择其他中间结果文件")
            source = _safe_report_path(req.external_source)
        else:
            if req.external_source:
                raise ValueError("其他文件只能通过高级设置显式启用")
            if not req.run_id or not req.artifact:
                raise ValueError("请先选择流程及其已生成的中间结果")
            source = workflow_artifact(_reports_dir(), req.run_id, req.artifact)
        read_stage_source(source, req.stage)
        if req.stage == "cluster":
            embedding_settings(_cfg())
            check_dependencies()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    command = [sys.executable, "main.py", "discover", "--source", str(source),
               "--stage", req.stage, "--workers", str(req.workers)]
    if req.append_input:
        if req.stage != "iterate":
            raise HTTPException(400, "追加文件仅支持继续迭代")
        from types import SimpleNamespace
        append_path = _validate_input_request(SimpleNamespace(input=req.append_input, text_column=req.text_column, sheet_name=req.sheet_name))
        command += ["--input", str(append_path)]
        if req.text_column:
            command += ["--text-column", req.text_column]
        if req.sheet_name:
            command += ["--sheet-name", req.sheet_name]
    if req.stage == "iterate":
        command += ["--iteration-rounds", str(req.iteration_rounds)]
        if req.iteration_scope:
            command += ["--iteration-scope", req.iteration_scope]
    return _start_job(f"workflow:{req.stage}", command)














@app.post("/api/jobs/{job_id}/pause")
@app.post("/api/jobs/{job_id}/continue")
def api_set_job_pause(job_id: str, request: Request):
    try:
        return jobs.set_paused(job_id, request.url.path.endswith("/pause")).to_dict()
    except KeyError:
        raise HTTPException(404, "Job not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    try:
        return jobs.cancel(job_id).to_dict()
    except KeyError:
        raise HTTPException(404, "Job not found")


@app.get("/api/jobs")
def api_jobs():
    return [j.to_dict() for j in jobs.recent()]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, offset: int = 0):
    try:
        job = jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found")
    return job.to_dict(include_logs=True, offset=max(0, offset))


@app.get("/api/jobs/{job_id}/logs-download")
def api_job_logs_download(job_id: str):
    try:
        jobs.get(job_id)
    except KeyError:
        raise HTTPException(404,"任务不存在")
    if jobs.history_dir is None:raise HTTPException(404,"没有历史日志目录")
    root=jobs.history_dir.resolve()
    path=jobs.history_dir/job_id/'logs.jsonl'
    if path.is_symlink() or root not in path.resolve().parents:raise HTTPException(403,"日志路径无效")
    if not path.is_file():raise HTTPException(404,"没有日志文件")
    return FileResponse(path,filename=f"{job_id}_logs.jsonl",media_type="application/x-ndjson")


@app.post("/api/jobs/{job_id}/resume")
def api_resume_job(job_id: str):
    with jobs.lock:
        previous = jobs.jobs.get(job_id)
        if previous is None:
            raise HTTPException(404, "任务不存在")
        if previous.status not in {"failed", "cancelled"} or previous.finished_at is None:
            raise HTTPException(409, "仅已结束的失败或取消任务可以复制续跑")
        command = list(previous.command)
        if "discover" not in command:
            raise HTTPException(400, "该任务不支持续跑")
        if "--resume-trace" in command:
            index = command.index("--resume-trace")
            del command[index:index+2]
        trace = PROJECT_ROOT / "data" / "request_traces" / job_id / "requests.jsonl"
        if trace.is_file():
            command += ["--resume-trace", str(trace)]
        # Prefer the last persisted state over replaying the original input.
        if previous.workflow_run_id:
            from intent_engine.workflows.workflow_artifacts import workflow_artifact
            try:
                snapshot = workflow_artifact(_reports_dir(), previous.workflow_run_id, "report")
                saved = json.loads(snapshot.read_text(encoding="utf-8"))
                from intent_engine.atomic.atomic_discovery import validate_stage_source
                validate_stage_source(saved, "iterate")
            except (ValueError, OSError, KeyError, TypeError):
                raise HTTPException(409, "最近状态不能安全恢复迭代，请使用阶段处理选择可用中间结果")
            for flag in ("--input", "--source", "--stage", "--iteration-rounds", "--limit", "--text-column", "--sheet-name"):
                if flag in command:
                    index = command.index(flag)
                    del command[index:index+2]
            total = int((saved.get("iteration") or {}).get("max_rounds") or saved.get("parameters", {}).get("iteration_rounds", 3))
            if saved.get("parameters",{}).get("iteration_auto"):total=-1
            command += ["--source", str(snapshot), "--stage", "iterate", "--iteration-rounds", str(total), "--resume-state"]

    return _start_job(previous.job_type + ":resume:" + job_id, command)


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str):
    from intent_engine.workflows.job_history_cleanup import remove_histories
    with jobs.lock:
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        if job.status in {"queued", "running", "paused"} or job.process is not None or job.finished_at is None:
            raise HTTPException(409, "任务尚未结束，不能删除任务历史")
        try:
            with remove_histories(jobs, [job], PROJECT_ROOT / "data" / "request_traces") as result:
                pass
            return result
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs/{job_id}/requests")
def api_job_requests(job_id: str, stage: str = "", offset: int = 0, view: str = "list", cluster: int | None = None):
    try:
        jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found")
    path = PROJECT_ROOT / "data" / "request_traces" / job_id / "requests.jsonl"
    return read_request_records(path, stage=stage, offset=offset, view=view, cluster=cluster)


def read_request_records(path: Path, *, stage="", offset=0, terms=(), view="list", cluster=None):
    from intent_engine.infrastructure.request_trace_store import read_indexed_requests
    return read_indexed_requests(path, stage=stage, offset=offset, terms=terms, view=view, cluster=cluster)


@app.get("/api/workflows/{run_id}/requests")
def api_workflow_requests(run_id: str, stage: str = "", offset: int = 0, terms: str = "[]", view: str = "list", cluster: int | None = None):
    from intent_engine.workflows.workflow_artifacts import load_workflow
    from intent_engine.workflows.workflow_layout import find_manifest, check_no_symlinks
    try:
        load_workflow(_reports_dir(), run_id)
        filters = json.loads(terms)
        if not isinstance(filters, list) or len(filters) > 100 or any(not isinstance(t, str) for t in filters):
            raise ValueError("请求过滤条件无效")
        path = find_manifest(_reports_dir(), run_id).parent / "requests" / "requests.jsonl"
        check_no_symlinks(path, _reports_dir())
        return read_request_records(path, stage=stage, offset=offset, terms=filters, view=view, cluster=cluster)
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


report("路由与页面注册", _ROUTES_STARTED)

def main():
    host = os.environ.get("INTENT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("INTENT_WEB_PORT", "7860"))
    if os.environ.get("INTENT_WEB_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    print(f"[启动] 启动 HTTP 服务 {host}:{port}", flush=True)
    app.state.http_start_time = time.perf_counter()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
