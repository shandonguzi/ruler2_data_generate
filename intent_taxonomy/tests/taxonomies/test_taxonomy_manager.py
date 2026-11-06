from pathlib import Path

import pytest

from intent_engine.models import DomainDef, TaskDef, Taxonomy
from intent_engine.taxonomies.taxonomy import load_taxonomy
from intent_engine.taxonomies.taxonomy_manager import apply_managed_taxonomy, build_managed_taxonomy
from intent_engine.infrastructure.utils import stable_task_id


def base_taxonomy():
    return Taxonomy(
        version=9,
        domains=[DomainDef(name="影音娱乐"), DomainDef(name="健康医疗")],
        tasks=[
            TaskDef(
                task_id="task_keepid",
                domain="影音娱乐",
                object="电影",
                action="推荐",
                description="推荐电影",
                created_in_version=3,
            )
        ],
    )


def test_edit_keeps_task_id_and_increments_once():
    tax = base_taxonomy()
    draft = [
        {
            **tax.tasks[0].model_dump(),
            "object": "影视内容",
            "object_aliases": ["电影"],
        },
        {
            "task_id": "__new__1",
            "domain": "健康医疗",
            "object": "用药",
            "action": "查询",
            "description": "用药知识查询",
        },
    ]
    new, changes = build_managed_taxonomy(tax, draft)
    assert new.version == 10
    assert len(new.tasks) == 2
    edited = next(t for t in new.tasks if t.task_id == "task_keepid")
    assert edited.object == "影视内容"
    assert edited.created_in_version == 3
    added = next(t for t in new.tasks if t.task_id != "task_keepid")
    assert added.task_id == stable_task_id("健康医疗", "用药", "查询")
    assert added.created_in_version == 10
    assert changes["counts"] == {"added": 1, "updated": 1, "deleted": 0, "tasks_before": 1, "tasks_after": 2}


def test_noop_does_not_increment_version():
    tax = base_taxonomy()
    new, changes = build_managed_taxonomy(tax, [tax.tasks[0].model_dump()])
    assert new.version == 9
    assert changes["changed"] is False


def test_duplicate_doa_rejected():
    tax = base_taxonomy()
    with pytest.raises(ValueError, match="Duplicate canonical Task"):
        build_managed_taxonomy(
            tax,
            [
                tax.tasks[0].model_dump(),
                {"task_id": "__new__x", "domain": "影音娱乐", "object": "电影", "action": "推荐"},
            ],
        )


def test_apply_snapshots_and_changelog(tmp_path: Path):
    tax = base_taxonomy()
    current = tmp_path / "data" / "taxonomy" / "current.json"
    reports = tmp_path / "data" / "reports"
    skills = tmp_path / "data" / "skills.json"
    current.parent.mkdir(parents=True)
    current.write_text(tax.model_dump_json(indent=2), encoding="utf-8")
    skills.parent.mkdir(parents=True, exist_ok=True)
    skills.write_text('{"skills": []}', encoding="utf-8")

    draft = [{**tax.tasks[0].model_dump(), "description": "updated"}]
    new, changes = apply_managed_taxonomy(tax, draft, current, reports, skills)
    assert new.version == 10
    assert load_taxonomy(str(current)).version == 10
    assert (current.parent / "versions" / "taxonomy_v009.json").exists()
    assert (current.parent / "versions" / "taxonomy_v010.json").exists()
    assert (reports / "taxonomy_manual_edit_v010_changelog.json").exists()
    assert changes["counts"]["updated"] == 1


def test_delete_skill_referenced_task_is_blocked(tmp_path: Path):
    tax = base_taxonomy()
    current = tmp_path / "current.json"
    reports = tmp_path / "reports"
    skills = tmp_path / "skills.json"
    current.write_text(tax.model_dump_json(indent=2), encoding="utf-8")
    skills.write_text('{"skills":[{"skill_id":"film-tv","supported_task_ids":["task_keepid"]}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="referenced by skills.json"):
        apply_managed_taxonomy(tax, [], current, reports, skills)
