import pytest

from intent_engine.workflows.workflow_visuals import visualization_page


def snapshot():
    return {"completed_stages": ["extract", "cluster"],
            "rows": [{"row_id": i, "raw_text": f"原句{i}", "status": "valid"} for i in range(65)] +
                    [{"row_id": 100, "raw_text": "失败原句", "status": "processing_error"}],
            "intents": [{"intent_id": f"i{i}", "row_id": i, "raw_text": f"原句{i}",
                         "text": "相同意图" if i < 60 else "其他意图", "status": "valid" if i < 64 else "ambiguous",
                         "cluster_id": 0 if i < 60 else -1 if i < 64 else None} for i in range(65)],
            "clusters": [{"cluster_id": 0, "sample_count": 2, "keywords": ["打开"], "review": {"verdict": "consistent"}}]}


def test_aggregates_use_full_membership_not_preview_or_review_sample():
    data = visualization_page(snapshot(), view="clusters")
    assert data["totals"] == {"rows": 66, "intents": 65, "clusters": 1, "noise": 4,
                              "unassigned": 1, "ambiguous": 1, "errors": 1}
    assert data["clusters"][0]["count"] == 60
    assert data["clusters"][0]["unique_count"] == 1
    assert len(data["records"]) == 20
    assert data["pages"] == 4


def test_filters_pagination_and_source_context():
    data = snapshot()
    data["intents"].append({"intent_id": "extra", "row_id": 0, "raw_text": "原句0", "text": "另一个动作", "cluster_id": -1})
    page = visualization_page(data, cluster="0", query="原句0")
    assert page["total"] == 1
    assert len(page["records"][0]["intents"]) == 2  # Keep relation indices and complete raw-sentence context.
    assert visualization_page(data, view="clusters", cluster="-1")["total"] == 5
    assert visualization_page(data, view="clusters", cluster="unassigned")["total"] == 1
    assert visualization_page(data, query="失败")["records"][0]["status"] == "processing_error"
    assert visualization_page(data, page=999)["page"] == 4
    assert visualization_page(data, query="不存在")["total"] == 0
    assert visualization_page(data, query="i64")["records"][0]["row_id"] == 64
    with pytest.raises(ValueError):
        visualization_page(data, cluster="999")


def test_empty_and_extract_only_snapshot():
    page = visualization_page({})
    assert page["records"] == [] and page["pages"] == 1 and not page["clustered"]


def test_latest_classification_is_linked_without_overwriting_original_atom():
    source = snapshot()
    source["iteration"] = {"assignments": [{"intent_id": "i0", "domain": "应用", "slots": {"应用名": "支付宝"}}]}
    page = visualization_page(source)
    assert page["records"][0]["intents"][0]["classification"]["slots"] == {"应用名": "支付宝"}
    assert "classification" not in source["intents"][0]


def test_visualization_api_scopes_artifacts(tmp_path, monkeypatch):
    import json
    import web_server as web
    from fastapi import HTTPException
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(snapshot()))
    manifest = tmp_path / "coldstart_test_workflow.json"
    manifest.write_text(json.dumps({"run_id": "test", "artifacts": {"cluster": str(source)}}))
    monkeypatch.setattr(web, "_reports_dir", lambda: tmp_path)
    assert web.api_workflow_visualization("test", "cluster", view="clusters")["total"] == 65
    with pytest.raises(HTTPException):
        web.api_workflow_visualization("test", "missing")
    manifest.write_text(json.dumps({"run_id": "test", "artifacts": {"cluster": str(tmp_path.parent / "outside.json")}}))
    with pytest.raises(HTTPException):
        web.api_workflow_visualization("test", "cluster")
