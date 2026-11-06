"""Workflow-local assignment records and taxonomy comparison helpers."""
import hashlib
import json


def fingerprint(taxonomy):
    data = taxonomy.model_dump(exclude={"version"})
    for task in data["tasks"]:
        task.pop("created_in_version", None)
    data["tasks"].sort(key=lambda task: task["task_id"])
    data["domains"].sort(key=lambda domain: domain["name"])
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def taxonomy_changes(before, after):
    changes = []
    for key, identity in (("tasks", "task_id"), ("domains", "name")):
        previous = {item[identity]: item for item in before.model_dump()[key]}
        current = {item[identity]: item for item in after.model_dump()[key]}
        for name in sorted(previous.keys() | current.keys()):
            if previous.get(name) != current.get(name):
                changes.append({"kind": key, "id": name, "operation": "ADD" if name not in previous else "DELETE" if name not in current else "UPDATE",
                                "before": previous.get(name), "after": current.get(name)})
    return changes


def assignments(atoms, results, version, taxonomy_fingerprint):
    records = []
    for index, atom in enumerate(atoms):
        result = results[index] if index < len(results) else None
        intents = result.intents if result else []
        intent = intents[0] if len(intents) == 1 else None
        records.append({"intent_id": atom["intent_id"], "row_id": atom["row_id"], "raw_text": atom["raw_text"],
                        "text": atom["text"], "span": atom["span"], "taxonomy_version": version,
                        "topic_path": atom.get("topic_path", []),
                        "taxonomy_fingerprint": taxonomy_fingerprint,
                        "status": intent.status if intent else "processing_error", "domain": intent.domain if intent else "",
                        "object": intent.object if intent else "", "action": intent.action if intent else "",
                        "slots": intent.slots if intent else atom.get("slots", {}),
                        "task_id": intent.matched_task_id if intent else None,
                        "confidence": intent.confidence if intent else 0,
                        "conflicting_task_ids": list(intent.conflicting_task_ids) if intent else [],
                        "error": intent.match_error if intent else "分类未返回恰好一条原子意图"})
    return records
