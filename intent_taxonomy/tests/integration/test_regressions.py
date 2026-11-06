"""Regression coverage for cache identity, failed matches, Task IDs and writes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pandas as pd
import pytest

from intent_engine.classification.classifier import IntentClassifier
from intent_engine.infrastructure.excel_io import flatten_result
from intent_engine.models import DomainDef, SemanticIntent, SemanticResult, TaskDef, Taxonomy, TaxonomyDecision
from intent_engine.workflows.pipeline import IterativePipeline
from intent_engine.taxonomies.taxonomy import apply_decisions, load_taxonomy, save_taxonomy
from intent_engine.taxonomies.taxonomy_manager import build_managed_taxonomy
from intent_engine.infrastructure.utils import stable_task_id


def taxonomy():
    return Taxonomy(domains=[DomainDef(name="影音娱乐")], tasks=[TaskDef(
        task_id=stable_task_id("影音娱乐", "电影", "播放"),
        domain="影音娱乐", object="电影", action="播放",
    )])


def intent_result(text="播放电影"):
    return SemanticResult(raw_text=text, intents=[SemanticIntent(
        domain="影音娱乐", object="电影", action="播放", span=text,
    )])


@pytest.fixture
def pipeline(tmp_path):
    obj = IterativePipeline.__new__(IterativePipeline)
    obj.checkpoint_dir = tmp_path
    obj.llm = None
    obj.cfg = {
        "pipeline": {"text_column": "text"},
        "llm": {"max_workers": 1, "model": "model-a"},
    }
    return obj


def test_cache_reuses_identical_run_and_isolates_changed_inputs(pipeline, monkeypatch):
    calls = []

    def classify(self, text):
        calls.append(text)
        return intent_result(text)

    monkeypatch.setattr(IntentClassifier, "classify", classify)
    tax = taxonomy()
    df = pd.DataFrame({"text": ["播放电影", "播放音乐"]})
    pipeline.classify_dataframe(df, tax, 1)
    pipeline.classify_dataframe(df, tax, 1)
    assert calls == ["播放电影", "播放音乐"]

    changed = pd.DataFrame({"text": ["播放新闻", "播放音乐"]})
    result = pipeline.classify_dataframe(changed, tax, 1)
    assert result[0].raw_text == "播放新闻"
    assert len(calls) == 4

    # Reordering rows must also invalidate the positional cache.
    result = pipeline.classify_dataframe(df.iloc[::-1], tax, 1)
    assert [r.raw_text for r in result] == ["播放音乐", "播放电影"]
    assert len(calls) == 6


@pytest.mark.parametrize("change", ["model", "matching", "taxonomy"])
def test_cache_invalidated_by_configuration_or_same_version_taxonomy(pipeline, monkeypatch, change):
    calls = []
    monkeypatch.setattr(IntentClassifier, "classify", lambda self, text: calls.append(text) or intent_result(text))
    tax = taxonomy()
    df = pd.DataFrame({"text": ["播放电影"]})
    pipeline.classify_dataframe(df, tax, 1)
    if change == "model":
        pipeline.cfg["llm"]["model"] = "model-b"
    elif change == "matching":
        pipeline.cfg["task_matching"] = {"candidate_top_k": 2}
    else:
        tax.tasks[0].description = "updated without version bump"
    pipeline.classify_dataframe(df, tax, 1)
    assert len(calls) == 2


@pytest.mark.parametrize("stage", ["pairwise", "partial_pairwise", "final"])
def test_failed_matching_is_not_discovery_evidence(stage, monkeypatch):
    tax = taxonomy()
    if stage == "partial_pairwise":
        tax.tasks.append(TaskDef(task_id="other", domain="影音娱乐", object="音乐", action="播放"))
    classifier = IntentClassifier(None, tax, matching_cfg={"enabled": True})

    def pairwise(result, intent, task):
        if stage == "pairwise" or (stage == "partial_pairwise" and task.task_id == "other"):
            raise RuntimeError("temporary outage")
        from intent_engine.models import PairwiseTaskDecision
        return PairwiseTaskDecision(task_id=task.task_id, match=True, confidence=0.99, reason="equivalent")

    def final(*args):
        raise RuntimeError("temporary outage")

    monkeypatch.setattr(classifier, "_pairwise_judge", pairwise)
    monkeypatch.setattr(classifier, "_final_judge", final)
    result = classifier.match(intent_result())
    assert result.intents[0].status == "ambiguous"
    assert "temporary outage" in result.intents[0].match_error


def test_failed_rows_are_retried_and_successes_cached(pipeline, monkeypatch):
    calls = []

    def classify(self, text):
        calls.append(text)
        result = intent_result(text)
        if text == "retry" and calls.count("retry") == 1:
            result.intents[0].status = "ambiguous"
            result.intents[0].match_error = "temporary outage"
        return result

    monkeypatch.setattr(IntentClassifier, "classify", classify)
    df = pd.DataFrame({"text": ["cached", "retry"]})
    pipeline.classify_dataframe(df, taxonomy(), 1)
    result = pipeline.classify_dataframe(df, taxonomy(), 1)
    pipeline.classify_dataframe(df, taxonomy(), 1)
    assert calls.count("cached") == 1
    assert calls.count("retry") == 2
    assert not result[1].intents[0].match_error


def test_add_after_manual_rename_reuses_existing_id_and_enriches():
    tax = taxonomy()
    tid = tax.tasks[0].task_id
    edited, _ = build_managed_taxonomy(tax, [{**tax.tasks[0].model_dump(), "object": "影视内容"}])
    result, _ = apply_decisions(edited, [TaxonomyDecision(operation="ADD", new_tasks=[{
        "domain": "影音娱乐", "object": "影视内容", "action": "播放", "positive_examples": ["播放电视剧"],
    }])])
    assert len(result.tasks) == 1
    assert result.tasks[0].task_id == tid
    assert "播放电视剧" in result.tasks[0].positive_examples


def test_add_original_semantics_after_rename_does_not_overwrite_renamed_task():
    tax = taxonomy()
    edited, _ = build_managed_taxonomy(tax, [{**tax.tasks[0].model_dump(), "object": "影视内容"}])
    result, _ = apply_decisions(edited, [TaxonomyDecision(operation="ADD", new_tasks=[{
        "domain": "影音娱乐", "object": "电影", "action": "播放",
    }])])
    assert {t.object for t in result.tasks} == {"影视内容", "电影"}
    assert len({t.task_id for t in result.tasks}) == 2




def test_failed_atomic_save_preserves_old_file_and_cleans_temp(tmp_path, monkeypatch):
    import intent_engine.taxonomies.taxonomy as module
    path = tmp_path / "current.json"
    tax = taxonomy()
    save_taxonomy(tax, str(path))

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr(module.os, "replace", fail)
    tax.version += 1
    with pytest.raises(OSError):
        save_taxonomy(tax, str(path))
    assert load_taxonomy(str(path)).version == 1
    assert list(tmp_path.iterdir()) == [path]




def test_job_cannot_start_during_manual_save_and_blocks_later_edits(monkeypatch):
    import web_server as web
    manager = web.JobManager()
    monkeypatch.setattr(manager, "_run", lambda job: None)
    started = Event()

    def start_job():
        started.set()
        return manager.start("iterate", ["unused"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with manager.taxonomy_edit():
            future = pool.submit(start_job)
            assert started.wait(5)
            assert not future.done()
        job = future.result(timeout=5)
    assert job.status == "queued"
    with pytest.raises(web.HTTPException) as exc:
        with manager.taxonomy_edit():
            pytest.fail("active job must block edits")
    assert exc.value.status_code == 409


def test_batch_reuses_initial_semantics_and_cache_tracks_them(pipeline,monkeypatch):
    pipeline.cfg['pipeline'].update(atomic_input=True,text_column='_atomic_text')
    monkeypatch.setattr(IntentClassifier,'match',lambda self,result:result)
    calls=[]
    original=IntentClassifier.classify_atomic
    def classify(self,*args,**kwargs):
        calls.append(1)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(IntentClassifier,'classify_atomic',classify)
    class NoParse:
        def chat_json(self,*args,**kwargs):pytest.fail('initial semantics must be reused')
    pipeline.llm=NoParse()
    frame=pd.DataFrame([{'_atomic_text':'播放电影','_atomic_semantics':{'text':'播放电影','object':'电影','action':'播放'}}])
    first=pipeline.classify_dataframe(frame,taxonomy(),1)
    pipeline.classify_dataframe(frame,taxonomy(),1)
    assert len(calls)==1 and first[0].intents[0].object=='电影'
    frame.iloc[0]['_atomic_semantics']['object']='影视内容'
    changed=pipeline.classify_dataframe(frame,taxonomy(),1)
    assert len(calls)==2 and changed[0].intents[0].object=='影视内容'
