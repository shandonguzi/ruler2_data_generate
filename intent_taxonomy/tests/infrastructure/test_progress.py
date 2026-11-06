from __future__ import annotations

import json
import sys
import threading

import pandas as pd
import pytest

from intent_engine.workflows.hierarchy_iteration import hierarchy_iteration_steps
from intent_engine.infrastructure.progress import PROGRESS_PREFIX, ProgressReporter, ProgressState


def test_classification_completion_is_not_whole_job_completion():
    state = ProgressState()
    state.apply({"event": "plan", "steps": [{"id": "classify", "label": "分类"}, {"id": "review", "label": "审核"}]})
    state.apply({"event": "start", "id": "classify"})
    state.apply({"event": "update", "id": "classify", "completed": 100, "total": 100, "cached": 20, "failed": 2})
    snap = state.snapshot()
    assert snap["current"]["completed"] == 100
    assert snap["current"]["cached"] == 20
    assert snap["current"]["failed"] == 2
    assert snap["percent"] < 50
    state.apply({"event": "complete", "id": "classify"})
    state.apply({"event": "start", "id": "review"})
    assert state.snapshot()["percent"] == 50
    assert state.snapshot()["current"]["total"] is None
    state.apply({"event": "complete", "id": "review"})
    assert state.snapshot()["percent"] == 99
    assert state.snapshot("succeeded")["percent"] == 100


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failure_and_cancellation_never_report_success(status):
    reporter = ProgressReporter()
    reporter.plan([{"id": "a", "label": "阶段一"}])
    reporter.start("a")
    reporter.update(7, 10)
    snap = reporter.state.snapshot(status)
    assert snap["percent"] < 100
    assert snap["current"]["status"] == status
    # Projection does not overwrite the source execution state.
    assert reporter.state.steps[0]["status"] == "running"


def test_skipped_rounds_and_disabled_stages_are_explicit():
    cfg = {"skill_routing": {"enabled": False}, "taxonomy_canonicalization": {"enabled": False},
           "longtail_assimilation": {"enabled": False}}
    reporter = ProgressReporter()
    reporter.plan(hierarchy_iteration_steps(3))
    reporter.start('iteration_prepare')
    reporter.complete()
    assert not any(s["id"].endswith((":routing", ":canonicalize", ":longtail")) for s in reporter.state.steps)
    for step in list(reporter.state.steps):
        if step.get("round",0) == 1:
            reporter.start(step["id"])
    reporter.complete()
    reporter.skip(s["id"] for s in reporter.state.steps if s.get("round",0) > 1)
    reporter.start("iteration_validation")
    reporter.start("iteration_reports")
    reporter.complete()
    snapshot = reporter.state.snapshot("succeeded")
    assert snapshot["skipped_stages"] == 10
    assert snapshot["completed_stages"] == snapshot["total_stages"] == 8
    assert snapshot["percent"] == 100


def test_unknown_totals_and_invalid_events_do_not_invent_a_percentage():
    state = ProgressState()
    assert state.snapshot()["percent"] is None
    assert state.apply({"event": "update", "id": "missing", "completed": 10, "total": 20}) is False
    assert state.apply({"event": "plan", "steps": [{"id": "x", "label": "阶段"}, {"id": "x", "label": "重复"}]}) is False
    assert state.steps == []


def test_worker_events_are_separate_from_logs_and_preserve_unicode(monkeypatch, capsys):
    import web_server as web
    monkeypatch.setenv("INTENT_PROGRESS", "1")
    reporter = ProgressReporter()
    reporter.plan([{"id": "a", "label": "语义分类"}])
    reporter.start("a")
    reporter.update(3, 3, cached=1)
    reporter.complete()
    manager = web.JobManager()
    job = web.Job("test", "iterate", [])
    for line in capsys.readouterr().out.splitlines():
        manager._append(job, line)
    assert job.logs == []
    assert job.to_dict()["progress"]["current"]["label"] == "语义分类"
    assert job.to_dict()["progress"]["current"]["completed"] == 3
    manager._append(job, PROGRESS_PREFIX + "broken JSON")
    assert "broken JSON" in job.logs[-1]


def test_log_offsets_continue_after_ring_buffer_trimming():
    import web_server as web
    manager = web.JobManager()
    job = web.Job("test", "iterate", [], logs=[str(n) for n in range(20000)])
    manager._append(job, "new1")
    assert job.log_start_offset == 5001
    manager._append(job, "new2")
    payload = job.to_dict(include_logs=True, offset=20000)
    assert payload["logs"] == ["new1", "new2"]
    assert payload["next_offset"] == 20002


def test_real_worker_stream_reports_before_exit(tmp_path, monkeypatch):
    """Exercise the real pipe, UTF-8 decoder and protocol while the child is live."""
    import web_server as web
    gate = tmp_path / "release"
    source = r'''import sys,time
from pathlib import Path
from intent_engine.infrastructure.progress import ProgressReporter
p=ProgressReporter()
p.plan([{"id":"classify","label":"分类阶段"},{"id":"review","label":"审核阶段"}])
p.start("classify")
sys.stderr.write("\rclassify 50%")
sys.stderr.flush()
p.update(1,2)
print("中文日志：正在执行",flush=True)
deadline=time.monotonic()+8
while not Path(sys.argv[1]).exists() and time.monotonic()<deadline: time.sleep(.01)
p.update(2,2)
p.start("review")
p.complete()
'''
    job = web.Job("test", "iterate", [sys.executable, "-u", "-c", source, str(gate)])
    manager = web.JobManager()
    observed = threading.Event()
    original = manager._append

    def append(job, line):
        original(job, line)
        current = job.progress.snapshot()["current"]
        if current and current["completed"] == 1:
            observed.set()

    monkeypatch.setattr(manager, "_append", append)
    thread = threading.Thread(target=manager._run, args=(job,))
    thread.start()
    try:
        assert observed.wait(5), "progress must arrive while the worker is still running"
        assert job.status == "running"
        assert job.to_dict()["progress"]["percent"] == 25
    finally:
        gate.touch()
        thread.join(timeout=10)
        if thread.is_alive() and job.process:
            job.process.kill()
            thread.join(timeout=5)
    assert not thread.is_alive()
    assert job.status == "succeeded"
    assert job.to_dict()["progress"]["percent"] == 100
    assert "中文日志：正在执行" in job.logs
    assert not any(line.startswith(PROGRESS_PREFIX) for line in job.logs)


def test_classification_counters_include_cache_and_failed_rows(tmp_path, monkeypatch):
    from intent_engine.classification.classifier import IntentClassifier
    from intent_engine.models import DomainDef, SemanticIntent, SemanticResult, Taxonomy
    from intent_engine.workflows.pipeline import IterativePipeline
    pipeline = IterativePipeline.__new__(IterativePipeline)
    pipeline.cfg = {"pipeline": {"text_column": "text"}, "llm": {"max_workers": 1}}
    pipeline.llm = None
    pipeline.checkpoint_dir = tmp_path
    pipeline.progress = ProgressReporter()
    pipeline.progress.plan([{"id": "classify", "label": "分类"}])
    pipeline.progress.start("classify")
    tax = Taxonomy(domains=[DomainDef(name="测试")])

    def classify(self, text):
        if text == "error":
            raise RuntimeError("request failed")
        intent = SemanticIntent(domain="测试", object="对象", action="查询")
        if text == "match_error":
            intent.status = "ambiguous"
            intent.match_error = "request failed"
        return SemanticResult(raw_text=text, intents=[intent])

    monkeypatch.setattr(IntentClassifier, "classify", classify)
    df = pd.DataFrame({"text": ["success", "error", "match_error"]})
    pipeline.classify_dataframe(df, tax, 1)
    pipeline.classify_dataframe(df, tax, 1)
    current = pipeline.progress.state.snapshot()["current"]
    assert current["completed"] == current["total"] == 3
    assert current["cached"] == 1
    assert current["failed"] == 2






@pytest.mark.parametrize('outcome', ['failed', 'blocked', 'completed_with_errors', 'skipped'])
def test_stage_outcomes_are_not_silently_replaced_by_success(outcome):
    reporter = ProgressReporter()
    reporter.plan([{'id': 'review', 'label': '审核'}, {'id': 'export', 'label': '导出'}])
    reporter.start('review')
    reporter.complete(outcome)
    reporter.start('export')
    assert reporter.state.steps[0]['status'] == outcome
    assert reporter.state.snapshot()['completed_stages'] == (1 if outcome == 'completed_with_errors' else 0)


def test_classification_selection_metadata_survives_worker_updates():
    reporter = ProgressReporter()
    reporter.plan([{'id':'round_2:classify','label':'matching'}])
    reporter.start('round_2:classify')
    reporter.update(0,3,reused=97,selection_scope='unresolved_only')
    # Batch worker progress must retain reuse metadata while reporting cache/errors separately.
    reporter.update(3,3,cached=1,failed=1)
    stage=reporter.state.snapshot()['current']
    assert stage['total']==3 and stage['completed']==3
    assert stage['reused']==97
    assert stage['cached']==1 and stage['failed']==1
    assert stage['selection_scope']=='unresolved_only'
