"""JSONL and Excel input adapters. Invalid rows remain individually deliverable."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook

from .validators import normalize_case, parse_json


def read_inputs(config: dict) -> list:
    source = config["input"]
    path = Path(source["path"])
    items = []
    if source["format"] == "jsonl":
        for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw, error = parse_json(line), None
            except ValueError as exc:
                raw, error = line, str(exc)
            items.append({"input_id": f"line:{line_no}", "source": {"path": str(path), "line": line_no}, "raw_input": raw, "input_errors": [error] if error else []})
    else:
        book = load_workbook(path, data_only=True)
        formulas = load_workbook(path, data_only=False)
        try:
            name = source.get("sheet_name") or book.sheetnames[0]
            if name not in book.sheetnames:
                raise ValueError(f"Excel sheet not found: {name}")
            sheet = book[name]
            headers = [cell.value for cell in sheet[1]]
            columns = source["columns"]
            if any(headers.count(label) != 1 for label in columns.values()):
                raise ValueError("Each configured column must occur exactly once in the Excel header")
            positions = {key: headers.index(label) + 1 for key, label in columns.items()}
            for row in range(2, sheet.max_row + 1):
                if all(formulas[name].cell(row, col).value is None for col in range(1, sheet.max_column + 1)):
                    continue
                raw = {key: sheet.cell(row, col).value for key, col in positions.items()}
                raw = {key: value.isoformat() if isinstance(value, (date, datetime, time)) else value
                       for key, value in raw.items()}
                errors = []
                try:
                    if set(raw) == {"case"}:
                        raw = parse_json(raw["case"])
                    else:
                        raw = {key: value if key == "case_id" else parse_json(value) for key, value in raw.items()}
                except (ValueError, TypeError) as exc:
                    errors.append(f"Invalid JSON/missing cached formula value: {exc}")
                items.append({"input_id": f"sheet:{name}:row:{row}", "source": {"path": str(path), "sheet": name, "row": row}, "raw_input": raw, "input_errors": errors})
        finally:
            book.close()
            formulas.close()
    ids = Counter(item["raw_input"].get("case_id") for item in items
                  if isinstance(item["raw_input"], dict) and isinstance(item["raw_input"].get("case_id"), str))
    for item in items:
        raw = item["raw_input"]
        case_id = raw.get("case_id") if isinstance(raw, dict) else None
        item["case_id"] = case_id if isinstance(case_id, str) and case_id.strip() else None
        item["original_case"] = None
        if item["case_id"] and ids[item["case_id"]] > 1:
            item["input_errors"].append(f"Duplicate case_id: {item['case_id']}")
        if not item["input_errors"]:
            try:
                item["original_case"] = normalize_case(raw)
            except (ValueError, TypeError) as exc:
                item["input_errors"].append(str(exc))
    return items
