from pathlib import Path

from intent_engine.classification.classifier import IntentClassifier
from intent_engine.models import DomainDef, TaskDef, Taxonomy
from intent_engine.taxonomies.taxonomy import load_taxonomy
from intent_engine.taxonomies.taxonomy_manager import build_managed_taxonomy


def tax():
    return Taxonomy(
        version=9,
        domains=[DomainDef(name="通讯-运营商APP", parent_domain="GUI操控")],
        tasks=[
            TaskDef(
                task_id="task_phone",
                domain="通讯-运营商APP",
                object="话费",
                action="充值",
                subtasks=["默认号码充值"],
            )
        ],
    )


def test_old_json_defaults_parent_and_subtasks(tmp_path: Path):
    p = tmp_path / "current.json"
    p.write_text(
        '{"version":1,"domains":[{"name":"通讯"}],"tasks":[{"task_id":"t1","domain":"通讯","object":"话费","action":"查询"}]}',
        encoding="utf-8",
    )
    loaded = load_taxonomy(str(p))
    assert loaded.domains[0].parent_domain == ""
    assert loaded.tasks[0].subtasks == []


def test_manager_updates_parent_domain_and_subtasks():
    current = tax()
    domains = [{"name": "通讯-运营商APP", "parent_domain": "GUI操控", "description": "运营商应用"}]
    tasks = [{**current.tasks[0].model_dump(), "subtasks": ["默认号码充值", "自定义缴费金额"]}]
    result, changes = build_managed_taxonomy(current, tasks, domains)
    assert result.version == 10
    assert result.domains[0].parent_domain == "GUI操控"
    assert result.tasks[0].subtasks == ["默认号码充值", "自定义缴费金额"]
    assert changes["counts"]["updated"] == 1
    assert changes["domain_counts"]["updated"] == 1




def test_subtasks_are_exposed_to_task_judge_payload():
    task = tax().tasks[0]
    payload = IntentClassifier._task_payload(task)
    assert payload["subtasks"] == ["默认号码充值"]


class FakeLLM:
    def chat_json(self, system_prompt, user_prompt, schema, max_tokens=None):
        return schema.model_validate({
            "domains": [{"parent_domain": "GUI操控", "name": "通讯-运营商APP"}],
            "tasks": [
                {
                    "parent_domain": "GUI操控",
                    "domain": "通讯-运营商APP",
                    "object": "话费",
                    "action": "充值",
                    "subtasks": ["自定义缴费金额", "指定号码+自定义缴费金额"],
                    "target_task_id": "task_phone",
                    "source_text": "话费充值(自定义缴费金额、指定号码+自定义缴费金额)"
                },
                {"parent_domain": "GUI操控", "domain": "通讯-运营商APP", "object": "话费发票", "action": "开具"},
                {"parent_domain": "GUI操控", "domain": "通讯-运营商APP", "object": "基础套餐", "action": "查询"},
                {"parent_domain": "GUI操控", "domain": "通讯-运营商APP", "object": "流量", "action": "充值"},
                {"parent_domain": "GUI操控", "domain": "通讯-运营商APP", "object": "话费", "action": "查询"}
            ]
        })
