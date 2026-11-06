from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List

from intent_engine.models import Taxonomy, TaxonomyDecision, TaskDef
from intent_engine.infrastructure.utils import ensure_dir, stable_task_id


def load_taxonomy(path: str) -> Taxonomy:
    with open(path, "r", encoding="utf-8") as f:
        return Taxonomy.model_validate(json.load(f))


def save_taxonomy(taxonomy: Taxonomy, path: str):
    p = Path(path)
    ensure_dir(p.parent)
    # Readers see either the complete old file or the complete new file.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                         prefix=f".{p.name}.", suffix=".tmp", delete=False) as f:
            temp_path = Path(f.name)
            json.dump(taxonomy.model_dump(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, p)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def snapshot_taxonomy(taxonomy: Taxonomy, current_path: str):
    current = Path(current_path)
    versions_dir = ensure_dir(current.parent / "versions")
    version_path = versions_dir / f"taxonomy_v{taxonomy.version:03d}.json"
    save_taxonomy(taxonomy, str(version_path))
    return version_path


def task_by_id(taxonomy: Taxonomy) -> Dict[str, TaskDef]:
    return {t.task_id: t for t in taxonomy.tasks}


def canonical_task_id(tasks: Dict[str, TaskDef], domain: str, obj: str, action: str) -> str:
    """Find by canonical semantics, preserving manually assigned/renamed IDs."""
    key = (domain.strip(), obj.strip(), action.strip())
    for task in tasks.values():
        if (task.domain.strip(), task.object.strip(), task.action.strip()) == key:
            return task.task_id
    base = stable_task_id(*key)
    tid = base
    suffix = 2
    # A manual rename can leave the old semantic hash assigned to another DOA.
    while tid in tasks:
        tid = f"{base}_{suffix}"
        suffix += 1
    return tid


def _dedupe_text(values, limit: int = 20):
    return list(dict.fromkeys(str(x).strip() for x in values if str(x).strip()))[:limit]


def apply_decisions(taxonomy: Taxonomy, decisions: Iterable[TaxonomyDecision]) -> tuple[Taxonomy, List[dict]]:
    next_version = taxonomy.version + 1
    tasks = {t.task_id: t.model_copy(deep=True) for t in taxonomy.tasks}
    changelog: List[dict] = []

    for d in decisions:
        if d.operation == "KEEP":
            changelog.append(d.model_dump())
            continue

        source_tasks = [tasks[tid].model_copy(deep=True) for tid in d.target_task_ids if tid in tasks]

        if d.operation in {"MERGE", "SPLIT"}:
            for tid in d.target_task_ids:
                tasks.pop(tid, None)

        if d.operation in {"ADD", "MERGE", "SPLIT"}:
            for nt in d.new_tasks:
                tid = canonical_task_id(tasks, nt.domain, nt.object, nt.action)
                existing = tasks.get(tid)
                if existing is not None:
                    # Enrich an already-existing canonical Task rather than creating
                    # a duplicate. This is common when a MERGE proposal resolves to
                    # a Task that was already present.
                    existing.positive_examples = _dedupe_text([
                        *existing.positive_examples, *nt.positive_examples,
                        *[x for s in source_tasks for x in s.positive_examples],
                    ], 12)
                    existing.boundary_notes = _dedupe_text([
                        *existing.boundary_notes, *nt.boundary_notes,
                        *[x for s in source_tasks for x in s.boundary_notes],
                    ], 16)
                    existing.subtasks = _dedupe_text([
                        *existing.subtasks, *nt.subtasks,
                        *[x for s in source_tasks for x in getattr(s, "subtasks", [])],
                    ], 40)
                    existing.domain_aliases = _dedupe_text([
                        *existing.domain_aliases,
                        *[s.domain for s in source_tasks if s.domain != existing.domain],
                        *[x for s in source_tasks for x in s.domain_aliases],
                    ])
                    existing.object_aliases = _dedupe_text([
                        *existing.object_aliases,
                        *[s.object for s in source_tasks if s.object != existing.object],
                        *[x for s in source_tasks for x in s.object_aliases],
                    ])
                    existing.action_aliases = _dedupe_text([
                        *existing.action_aliases,
                        *[s.action for s in source_tasks if s.action != existing.action],
                        *[x for s in source_tasks for x in s.action_aliases],
                    ])
                    continue

                domain_aliases = _dedupe_text([
                    *[s.domain for s in source_tasks if s.domain != nt.domain],
                    *[x for s in source_tasks for x in s.domain_aliases],
                ])
                object_aliases = _dedupe_text([
                    *[s.object for s in source_tasks if s.object != nt.object],
                    *[x for s in source_tasks for x in s.object_aliases],
                ])
                action_aliases = _dedupe_text([
                    *[s.action for s in source_tasks if s.action != nt.action],
                    *[x for s in source_tasks for x in s.action_aliases],
                ])
                tasks[tid] = TaskDef(
                    task_id=tid,
                    domain=nt.domain,
                    object=nt.object,
                    action=nt.action,
                    description=nt.description,
                    positive_examples=_dedupe_text([
                        *nt.positive_examples,
                        *[x for s in source_tasks for x in s.positive_examples],
                    ], 12),
                    boundary_notes=_dedupe_text([
                        *nt.boundary_notes,
                        *[x for s in source_tasks for x in s.boundary_notes],
                    ], 16),
                    domain_aliases=domain_aliases,
                    object_aliases=object_aliases,
                    action_aliases=action_aliases,
                    subtasks=_dedupe_text([
                        *nt.subtasks,
                        *[x for s in source_tasks for x in getattr(s, "subtasks", [])],
                    ], 40),
                    created_in_version=next_version,
                )

        changelog.append(d.model_dump())

    result = taxonomy.model_copy(deep=True)
    result.tasks = sorted(tasks.values(), key=lambda x: (x.domain, x.object, x.action, x.task_id))

    # Do not create an empty taxonomy version merely because the reviewer
    # proposed an operation whose targets were stale/invalid or whose result
    # is already represented by the current taxonomy. Multi-round iteration
    # should advance only on an effective state mutation.
    before = [t.model_dump() for t in sorted(taxonomy.tasks, key=lambda x: x.task_id)]
    after = [t.model_dump() for t in sorted(result.tasks, key=lambda x: x.task_id)]
    result.version = next_version if before != after else taxonomy.version
    return result, changelog
