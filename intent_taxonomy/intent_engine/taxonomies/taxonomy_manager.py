from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from intent_engine.models import DomainDef, TaskDef, Taxonomy
from intent_engine.taxonomies.taxonomy import save_taxonomy, snapshot_taxonomy
from intent_engine.infrastructure.utils import ensure_dir, stable_task_id


EDITABLE_FIELDS = (
    "domain",
    "object",
    "action",
    "description",
    "positive_examples",
    "boundary_notes",
    "domain_aliases",
    "object_aliases",
    "action_aliases",
    "subtasks",
)

DOMAIN_EDITABLE_FIELDS = (
    "name",
    "parent_domain",
    "description",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_list(value: Any, limit: int = 50) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # The web editor sends arrays, but accepting newline / pipe separated
        # strings makes the API tolerant for manual callers too.
        value = value.replace("\r", "\n")
        parts: list[str] = []
        for line in value.split("\n"):
            parts.extend(line.split("|"))
        value = parts
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
    return list(dict.fromkeys(_text(x) for x in value if _text(x)))[:limit]


def _task_state(task: TaskDef) -> dict[str, Any]:
    return {k: getattr(task, k) for k in ("task_id", *EDITABLE_FIELDS, "created_in_version")}


def _domain_state(domain: DomainDef) -> dict[str, Any]:
    return {k: getattr(domain, k) for k in DOMAIN_EDITABLE_FIELDS}


def _build_domains(
    taxonomy: Taxonomy,
    proposed_domains: Iterable[dict[str, Any]] | None,
) -> tuple[list[DomainDef], dict[str, Any]]:
    """Project domain edits while keeping historical JSON backward compatible."""
    if proposed_domains is None:
        domains = [d.model_copy(deep=True) for d in taxonomy.domains]
    else:
        domains = []
        seen: set[str] = set()
        for index, raw in enumerate(proposed_domains):
            if not isinstance(raw, dict):
                raise ValueError(f"Domain #{index + 1} must be an object")
            name = _text(raw.get("name"))
            if not name:
                raise ValueError(f"Domain #{index + 1}: name cannot be empty")
            if name in seen:
                raise ValueError(f"Duplicate Domain: {name}")
            seen.add(name)
            domains.append(DomainDef(
                name=name,
                parent_domain=_text(raw.get("parent_domain")),
                description=_text(raw.get("description")),
            ))

    before = {_text(d.name): _domain_state(d) for d in taxonomy.domains}
    after = {_text(d.name): _domain_state(d) for d in domains}
    return domains, {
        "added_domains": sorted(set(after) - set(before)),
        "deleted_domains": sorted(set(before) - set(after)),
        "updated_domains": sorted(
            name for name in set(before) & set(after) if before[name] != after[name]
        ),
    }


def build_managed_taxonomy(
    taxonomy: Taxonomy,
    proposed_tasks: Iterable[dict[str, Any]],
    proposed_domains: Iterable[dict[str, Any]] | None = None,
) -> tuple[Taxonomy, dict[str, Any]]:
    """Validate a full web-editor draft and project it into one taxonomy version.

    Existing Task IDs stay stable during manual corrections. New Tasks receive
    stable IDs derived from canonical Domain/Object/Action. ``subtasks`` do not
    participate in the task id: they are concrete variants below one Atomic Task.

    ``proposed_domains`` is optional for compatibility with v1.3 callers. When
    supplied it is the complete desired Domain list and supports ``parent_domain``.
    """
    domains, domain_changes = _build_domains(taxonomy, proposed_domains)
    current_by_id = {t.task_id: t for t in taxonomy.tasks}
    configured_domains = {d.name for d in domains}
    next_version = taxonomy.version + 1

    projected: list[TaskDef] = []
    seen_ids: set[str] = set()
    seen_doa: dict[tuple[str, str, str], str] = {}
    added: list[str] = []
    updated: list[str] = []

    for index, raw in enumerate(proposed_tasks):
        if not isinstance(raw, dict):
            raise ValueError(f"Task #{index + 1} must be an object")

        domain = _text(raw.get("domain"))
        obj = _text(raw.get("object"))
        action = _text(raw.get("action"))
        if not domain or not obj or not action:
            raise ValueError(f"Task #{index + 1}: domain/object/action cannot be empty")
        if configured_domains and domain not in configured_domains:
            raise ValueError(f"Task #{index + 1}: unknown domain '{domain}'")

        incoming_id = _text(raw.get("task_id"))
        is_new = not incoming_id or incoming_id.startswith("__new__")
        if is_new:
            task_id = stable_task_id(domain, obj, action)
            if task_id in current_by_id:
                raise ValueError(
                    f"New Task {domain}/{obj}/{action} already exists as {task_id}; edit the existing Task instead"
                )
            created_in_version = next_version
        else:
            if incoming_id not in current_by_id:
                raise ValueError(f"Unknown existing task_id: {incoming_id}")
            task_id = incoming_id
            created_in_version = current_by_id[task_id].created_in_version

        if task_id in seen_ids:
            raise ValueError(f"Duplicate task_id in draft: {task_id}")
        seen_ids.add(task_id)

        doa_key = (domain, obj, action)
        if doa_key in seen_doa:
            raise ValueError(
                f"Duplicate canonical Task {domain}/{obj}/{action}: {seen_doa[doa_key]} and {task_id}"
            )
        seen_doa[doa_key] = task_id

        task = TaskDef(
            task_id=task_id,
            domain=domain,
            object=obj,
            action=action,
            description=_text(raw.get("description")),
            positive_examples=_text_list(raw.get("positive_examples"), 30),
            boundary_notes=_text_list(raw.get("boundary_notes"), 30),
            domain_aliases=_text_list(raw.get("domain_aliases"), 30),
            object_aliases=_text_list(raw.get("object_aliases"), 30),
            action_aliases=_text_list(raw.get("action_aliases"), 30),
            subtasks=_text_list(raw.get("subtasks"), 50),
            created_in_version=created_in_version,
        )
        projected.append(task)

        if is_new:
            added.append(task_id)
        elif _task_state(task) != _task_state(current_by_id[task_id]):
            updated.append(task_id)

    deleted = sorted(set(current_by_id) - seen_ids)
    projected.sort(key=lambda x: (x.domain, x.object, x.action, x.task_id))

    # A Domain cannot disappear while Tasks still point at it.
    task_domains = {t.domain for t in projected}
    missing_domain_defs = sorted(task_domains - configured_domains)
    if missing_domain_defs:
        raise ValueError("Tasks reference missing Domain definitions: " + ", ".join(missing_domain_defs))

    result = taxonomy.model_copy(deep=True)
    result.domains = sorted(domains, key=lambda x: (x.parent_domain, x.name))
    result.tasks = projected

    before_tasks = [_task_state(t) for t in sorted(taxonomy.tasks, key=lambda x: x.task_id)]
    after_tasks = [_task_state(t) for t in sorted(projected, key=lambda x: x.task_id)]
    before_domains = [_domain_state(d) for d in sorted(taxonomy.domains, key=lambda x: x.name)]
    after_domains = [_domain_state(d) for d in sorted(result.domains, key=lambda x: x.name)]
    changed = before_tasks != after_tasks or before_domains != after_domains
    result.version = next_version if changed else taxonomy.version

    counts = {
        "added": len(added),
        "updated": len(updated),
        "deleted": len(deleted),
        "tasks_before": len(taxonomy.tasks),
        "tasks_after": len(projected),
    }
    domain_counts = {
        "added": len(domain_changes["added_domains"]),
        "updated": len(domain_changes["updated_domains"]),
        "deleted": len(domain_changes["deleted_domains"]),
    }
    changes = {
        "changed": changed,
        "from_version": taxonomy.version,
        "to_version": result.version,
        "added_task_ids": sorted(added),
        "updated_task_ids": sorted(updated),
        "deleted_task_ids": deleted,
        **domain_changes,
        "counts": counts,
        "domain_counts": domain_counts,
    }
    return result, changes


def referenced_task_ids(skills_path: str | Path | None) -> dict[str, list[str]]:
    if not skills_path:
        return {}
    p = Path(skills_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    refs: dict[str, list[str]] = {}
    for skill in data.get("skills", []) or []:
        skill_id = _text(skill.get("skill_id") or skill.get("name"))
        for task_id in skill.get("supported_task_ids", []) or []:
            tid = _text(task_id)
            if tid:
                refs.setdefault(tid, []).append(skill_id)
    return {k: list(dict.fromkeys(v)) for k, v in refs.items()}


def apply_managed_taxonomy(
    current: Taxonomy,
    proposed_tasks: Iterable[dict[str, Any]],
    taxonomy_path: str | Path,
    reports_dir: str | Path,
    skills_path: str | Path | None = None,
    proposed_domains: Iterable[dict[str, Any]] | None = None,
) -> tuple[Taxonomy, dict[str, Any]]:
    projected, changes = build_managed_taxonomy(current, proposed_tasks, proposed_domains)
    if not changes["changed"]:
        return projected, changes

    refs = referenced_task_ids(skills_path) if skills_path else {}
    blocked = {
        tid: refs[tid]
        for tid in changes["deleted_task_ids"]
        if tid in refs and refs[tid]
    }
    if blocked:
        detail = "; ".join(f"{tid} -> {', '.join(skills)}" for tid, skills in blocked.items())
        raise ValueError(f"Cannot delete Tasks referenced by skills.json: {detail}")

    taxonomy_path = Path(taxonomy_path)
    snapshot_taxonomy(current, str(taxonomy_path))
    save_taxonomy(projected, str(taxonomy_path))
    snapshot_taxonomy(projected, str(taxonomy_path))

    reports = ensure_dir(reports_dir)
    changelog_path = reports / f"taxonomy_manual_edit_v{projected.version:03d}_changelog.json"
    payload = {
        **changes,
        "domain_changes": _diff_domains(current, projected),
        "changes": _diff_tasks(current, projected),
    }
    changelog_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    changes["changelog"] = str(changelog_path)
    return projected, changes


def _diff_domains(before: Taxonomy, after: Taxonomy) -> list[dict[str, Any]]:
    b = {d.name: d for d in before.domains}
    a = {d.name: d for d in after.domains}
    rows: list[dict[str, Any]] = []
    for name in sorted(set(b) | set(a)):
        old, new = b.get(name), a.get(name)
        if old is None and new is not None:
            rows.append({"operation": "ADD_DOMAIN", "name": name, "after": _domain_state(new)})
        elif old is not None and new is None:
            rows.append({"operation": "DELETE_DOMAIN", "name": name, "before": _domain_state(old)})
        elif old is not None and new is not None and _domain_state(old) != _domain_state(new):
            rows.append({
                "operation": "UPDATE_DOMAIN",
                "name": name,
                "before": _domain_state(old),
                "after": _domain_state(new),
            })
    return rows


def _diff_tasks(before: Taxonomy, after: Taxonomy) -> list[dict[str, Any]]:
    b = {t.task_id: t for t in before.tasks}
    a = {t.task_id: t for t in after.tasks}
    rows: list[dict[str, Any]] = []
    for tid in sorted(set(b) | set(a)):
        old = b.get(tid)
        new = a.get(tid)
        if old is None and new is not None:
            rows.append({"operation": "ADD", "task_id": tid, "after": _task_state(new)})
        elif old is not None and new is None:
            rows.append({"operation": "DELETE", "task_id": tid, "before": _task_state(old)})
        elif old is not None and new is not None and _task_state(old) != _task_state(new):
            changed_fields = [
                k for k in EDITABLE_FIELDS if getattr(old, k) != getattr(new, k)
            ]
            rows.append({
                "operation": "UPDATE",
                "task_id": tid,
                "changed_fields": changed_fields,
                "before": _task_state(old),
                "after": _task_state(new),
            })
    return rows
