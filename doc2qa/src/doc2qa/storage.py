from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from .schema import QA


def atomic_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, values):
    atomic_text(
        path,
        "".join(
            json.dumps(v.model_dump() if hasattr(v, "model_dump") else v, ensure_ascii=False) + "\n"
            for v in values
        ),
    )


def read_jsonl(path: Path, schema=None):
    records = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            records.append(schema.model_validate(data) if schema else data)
        except Exception as exc:
            raise ValueError(f"Invalid record at {path}:{n}: {type(exc).__name__}") from exc
    return records


def export(records: list[QA], directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    accepted = [q for q in records if q.review_status == "auto_pass"]
    review = [q for q in records if q.review_status == "pending_review"]
    rejected = [q for q in records if q.review_status == "rejected"]
    duplicates = [q for q in records if q.review_status == "duplicate"]
    for name, values in (
        ("candidates", records),
        ("dataset", accepted),
        ("review_queue", review),
        ("rejected", rejected),
        ("duplicates", duplicates),
    ):
        write_jsonl(directory / f"{name}.jsonl", values)
    write_json(directory / "dataset.json", [q.model_dump() for q in accepted])
    fields = list(QA.model_fields)
    with (directory / "dataset.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for qa in accepted:
            row = qa.model_dump()
            writer.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
            )
    write_json(
        directory / "deepeval_goldens.json",
        [
            {
                "input": q.question,
                "expected_output": q.answer,
                "context": [e.quote for e in q.evidence],
                "source_file": "|".join(sorted({e.source_file for e in q.evidence})),
                "additional_metadata": {"qa_id": q.qa_id, "context_id": q.context_id},
            }
            for q in accepted
        ],
    )
    summary = {
        "candidates": len(records),
        "accepted": len(accepted),
        "pending_review": len(review),
        "rejected": len(rejected),
        "duplicates": len(duplicates),
        "pass_rate": len(accepted) / len(records) if records else 0,
        "cross_document_accepted": sum(len(q.source_document_ids) > 1 for q in accepted),
        "accepted_per_document": dict(Counter(d for q in accepted for d in q.source_document_ids)),
        "accepted_per_type": dict(Counter(q.qa_type for q in accepted)),
        "reasons": dict(Counter(r for q in records for r in q.reasons)),
    }
    write_json(directory / "summary.json", summary)
    return summary


def evaluation_dataset(path: Path):
    """Construct local DeepEval Goldens, never fabricate test-case actual_output."""
    from deepeval.dataset import EvaluationDataset, Golden

    records = json.loads(path.read_text(encoding="utf-8"))
    return EvaluationDataset(goldens=[Golden(**record) for record in records])
