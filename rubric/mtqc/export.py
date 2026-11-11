"""Rebuild self-contained CaseResults from events; export atomically."""
from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from . import JUDGES, SCHEMA_VERSION
from .records import atomic_write


def export_case_result(item: dict, engine) -> dict:
    input_id = item["input_id"]
    original = item["original_case"]
    revisions = deepcopy(engine._revisions(input_id))
    repairs = deepcopy(engine._repairs(input_id))
    for repair in repairs:
        if repair["event"] == "START":
            repair.update(status="ERROR", reason="Repair interrupted; run with the same config and --run-dir to resume",
                          change_summary=[], attempt_ids=[event["attempt_id"] for event in
                              engine.records.select("attempts", repair_attempt_id=repair["repair_attempt_id"]) if event["event"] == "START"])
    for revision in revisions:
        evaluation = engine._saved_evaluation(input_id, revision["revision_id"])
        if evaluation is None:
            from .decision import summarize_results
            from .orchestrator import empty_judge
            evaluation = summarize_results({name: empty_judge(name, "ERROR", "No evaluation saved; run with --run-dir to resume") for name in JUDGES}, engine.config["decision"])
        revision["evaluation"] = evaluation
    first_pass = next((revision for revision in revisions if revision["evaluation"]["quality_decision"] == "PASS"), None)
    final = first_pass or (revisions[-1] if revisions else None)
    initial = revisions[0] if revisions else None
    null_scores = {name: None for name in JUDGES}
    stop = "INPUT_ERROR" if original is None else engine.stop_reason(input_id, final) if final else "INTERRUPTED"
    result = {
        "case_id": item["case_id"], "input_id": input_id, "source": item["source"],
        "run_id": engine.records.manifest["run_id"], "schema_version": SCHEMA_VERSION,
        "original_case": original, "was_revised": len(revisions) > 1,
        "repair_attempt_count": len(repairs), "repair_count": max(0, len(revisions) - 1),
        "initial_revision_id": initial["revision_id"] if initial else None,
        "final_revision_id": final["revision_id"] if final else None,
        "final_differs_original": bool(final and initial and final["dialogue_hash"] != initial["dialogue_hash"]),
        "overall_decision": final["evaluation"]["quality_decision"] if final else "INPUT_ERROR" if original is None else "REVIEW",
        "stop_reason": stop or "INTERRUPTED",
        "eligible_for_ingestion": bool(final and final["evaluation"]["evaluation_status"] == "COMPLETED" and final["evaluation"]["quality_decision"] == "PASS"),
        "revisions": revisions, "repair_attempts": repairs,
        "initial_scores": initial["evaluation"]["scores"] if initial else null_scores.copy(),
        "final_scores": final["evaluation"]["scores"] if final else null_scores.copy(),
    }
    if original is None:
        result.update(raw_input=item["raw_input"], input_errors=item["input_errors"])
    return result


def _sheet_name(book, wanted):
    name, index = wanted, 2
    while name in book.sheetnames:
        name, index = f"{wanted}（{index}）", index + 1
    return name


def _cell(sheet, row, column, value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    cell = sheet.cell(row, column)
    if isinstance(value, str):
        cell.value = value[:32000] + "…（完整内容见JSONL）" if len(value) > 32000 else value
        cell.data_type = "s"  # LLM/user strings must never become Excel formulas.
    else:
        cell.value = value
    cell.alignment = Alignment(vertical="top", wrap_text=True)


def _write_table(sheet, headers, rows, first_column=1):
    for offset, header in enumerate(headers, first_column):
        _cell(sheet, 1, offset, header)
        cell = sheet.cell(1, offset)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="24476B")
        sheet.column_dimensions[cell.column_letter].width = 24
    for row_no, values in rows:
        for column, value in enumerate(values, first_column):
            _cell(sheet, row_no, column, value)


def write_excel(config: dict, results: list):
    destination = Path(config["output"]["excel_path"])
    source = config["input"]
    book = load_workbook(source["path"]) if source["format"] == "excel" else Workbook()
    if source["format"] != "excel":
        book.remove(book.active)
    summary = book.create_sheet(_sheet_name(book, "Case汇总"))
    versions = book.create_sheet(_sheet_name(book, "版本明细"))
    headers = ["case_id", "input_id", "scenario", "seed_id", "was_revised", "repair_attempt_count", "repair_count",
               *[f"initial_{name}" for name in JUDGES], *[f"final_{name}" for name in JUDGES],
               "initial_decision", "overall_decision", "final_revision_id", "stop_reason", "eligible_for_ingestion",
               "dimension_feedback", "models_prompts", "records_location"]
    rows, by_input = [], {}
    for row_no, result in enumerate(results, 2):
        original = result["original_case"] or {}
        final = next((rev for rev in result["revisions"] if rev["revision_id"] == result["final_revision_id"]), None)
        values = [result["case_id"], result["input_id"], original.get("scenario"), original.get("seed_qa", {}).get("source_id"),
                  result["was_revised"], result["repair_attempt_count"], result["repair_count"],
                  *result["initial_scores"].values(), *result["final_scores"].values(),
                  result["revisions"][0]["evaluation"]["quality_decision"] if result["revisions"] else "INPUT_ERROR",
                  result["overall_decision"], result["final_revision_id"], result["stop_reason"], result["eligible_for_ingestion"],
                  [{"judge_name": entry["judge_name"], "reason": entry["reason"]} for entry in final["evaluation"]["dimension_feedback"]] if final else result.get("input_errors"),
                  {name: {"model": spec["model"], "prompt_version": spec["version"], "prompt_sha256": spec["prompt_sha256"]}
                   for name, spec in {**config["judges"], "dialogue_repair": config["repair"]}.items()},
                  f"{config['output']['results_jsonl']} | input_id={result['input_id']}"]
        rows.append((row_no, values))
        by_input[result["input_id"]] = values
    _write_table(summary, headers, rows)
    summary.freeze_panes = "C2"
    summary.auto_filter.ref = summary.dimensions
    version_headers = ["case_id", "revision_id", "parent_revision_id", "repair_round", "dialogue_summary", "dialogue_diff", "change_summary",
                       *JUDGES, "reasons", "failed_factors", "evidence_turn_ids", "evaluation_status", "quality_decision", "records_location"]
    version_rows = []
    for result in results:
        for rev in result["revisions"]:
            evaluation = rev["evaluation"]
            repair = next((entry for entry in result["repair_attempts"] if entry["repair_attempt_id"] == rev["repair_attempt_id"]), {})
            values = [result["case_id"], rev["revision_id"], rev["parent_revision_id"], rev["repair_round"],
                      json.dumps(rev["dialogue"], ensure_ascii=False)[:2000], rev["dialogue_diff"], repair.get("change_summary", []),
                      *evaluation["scores"].values(), {name: entry["reason"] for name, entry in evaluation["judge_results"].items()},
                      evaluation["failed_factors"], {name: entry["evidence_turn_ids"] for name, entry in evaluation["judge_results"].items()},
                      evaluation["evaluation_status"], evaluation["quality_decision"],
                      f"{config['output']['results_jsonl']} | {result['input_id']} | {rev['revision_id']}"]
            version_rows.append((len(version_rows) + 2, values))
    _write_table(versions, version_headers, version_rows)
    versions.freeze_panes = "C2"
    versions.auto_filter.ref = versions.dimensions
    if source["format"] == "excel":
        sheet = book[source.get("sheet_name") or book.sheetnames[0]]
        first = sheet.max_column + 1
        existing = {cell.value for cell in sheet[1]}
        appended = []
        for header in headers:
            name, suffix = f"mtqc_{header}", 2
            while name in existing:
                name, suffix = f"mtqc_{header}（{suffix}）", suffix + 1
            existing.add(name)
            appended.append(name)
        _write_table(sheet, appended, [(result["source"]["row"], by_input[result["input_id"]]) for result in results], first)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.xlsx")
    try:
        book.save(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        book.close()
        temporary.unlink(missing_ok=True)


def export_all(engine) -> list:
    # Complete a candidate's recoverable two-file commit even in export-only mode.
    items = engine.records.select("original_cases")
    for item in items:
        engine._apply_saved_revisions(item["input_id"])
    results = [export_case_result(item, engine) for item in items]
    path = engine.config["output"]["results_jsonl"]
    atomic_write(path, "".join(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n" for result in results))
    if engine.config["output"].get("excel_path"):
        write_excel(engine.config, results)
    if engine.config["output"].get("report_path"):
        from .report import write_report
        write_report(engine.config["output"]["report_path"], results, engine.records)
    return results
