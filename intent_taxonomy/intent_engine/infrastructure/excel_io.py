from __future__ import annotations

import json
import csv
from pathlib import Path
from typing import List
import pandas as pd

from intent_engine.models import SemanticResult


INPUT_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def read_input_table(path: str | Path, sheet_name: str | None = None, nrows: int | None = None):
    """Use identical parsing rules for file previews and full classification input."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in INPUT_EXTENSIONS:
        raise ValueError("输入文件必须是 .xlsx、.csv 或 .xls")
    metadata = {"sheets": [], "sheet": None, "format": suffix[1:]}
    if suffix == ".csv":
        if sheet_name is not None:
            raise ValueError("CSV 文件不包含工作表，请清空工作表选择")
        with path.open("rb") as f:
            prefix = f.read(4)
        encodings = ["utf-16"] if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
        for encoding in encodings:
            try:
                with path.open("r", encoding=encoding, newline="") as f:
                    sample = f.read(8192)
                    f.seek(0)
                    try:
                        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                    except csv.Error:
                        delimiter = ","
                    df = pd.read_csv(f, sep=delimiter, dtype=str, keep_default_na=False, nrows=nrows)
                metadata["encoding"] = encoding
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法解码 CSV，请将文件另存为 UTF-8 编码后重试")
    else:
        try:
            with pd.ExcelFile(path) as workbook:
                sheets = workbook.sheet_names
                if not sheets:
                    raise ValueError("Excel 文件中没有可用工作表")
                selected = sheet_name if sheet_name is not None else sheets[0]
                if selected not in sheets:
                    raise ValueError(f"工作表 {selected!r} 不存在，可用工作表：{sheets}")
                df = pd.read_excel(workbook, sheet_name=selected, dtype=str, keep_default_na=False, nrows=nrows)
                metadata.update(sheets=sheets, sheet=selected)
        except ImportError as exc:
            raise ValueError("缺少 Excel 读取依赖；.xlsx 需要 openpyxl，旧版 .xls 需要 xlrd") from exc
    # Numeric headers must have the same string identity in HTML, API and CLI.
    columns = [str(column).strip() for column in df.columns]
    if len(columns) != len(set(columns)):
        raise ValueError("去除列名两端空格后出现重复列名，请先给这些列设置不同名称")
    df.columns = columns
    if not columns:
        raise ValueError("文件中没有可读取的列，请确认第一行为列名")
    return df, metadata


def inspect_input_file(path: str | Path, sheet_name: str | None = None) -> dict:
    df, metadata = read_input_table(path, sheet_name=sheet_name, nrows=5)
    return {**metadata, "columns": list(df.columns), "rows": df.to_dict(orient="records"),
            "has_rows": not df.empty}


def load_excel(path: str, text_column: str, sheet_name: str | None = None) -> pd.DataFrame:
    """Backward-compatible entry point; accepts Excel and CSV input."""
    df, _ = read_input_table(path, sheet_name=sheet_name)
    if text_column not in df.columns:
        raise ValueError(f"文件中不存在列 {text_column!r}，当前列={list(df.columns)}")
    if df.empty:
        raise ValueError("文件只有列名，没有可分类的数据行")
    return df


def _route_value(intent, field, default=""):
    route = intent.skill_route
    if route is None:
        return default
    return getattr(route, field, default)


def flatten_result(result: SemanticResult) -> dict:
    intents = result.intents
    return {
        "intent_count": len(intents),
        "classification_status": "|".join(i.status for i in intents),
        "domain": "|".join(i.domain for i in intents),
        "object": "|".join(i.object for i in intents),
        "action": "|".join(i.action for i in intents),
        "matched_task_id": "|".join(i.matched_task_id or "" for i in intents),
        "confidence": "|".join(f"{i.confidence:.4f}" for i in intents),
        "intent_span": "|".join(i.span for i in intents),
        "slots": "|".join(json.dumps(i.slots, ensure_ascii=False) for i in intents),
        "match_reason": "|".join(i.match_reason for i in intents),
        "match_error": "|".join(i.match_error for i in intents),
        "candidate_task_ids": "|".join(
            ",".join(i.candidate_task_ids) for i in intents
        ),
        "primary_skill_id": "|".join((_route_value(i, "primary_skill_id") or "") for i in intents),
        "skill_route_mode": "|".join((_route_value(i, "route_mode") or "") for i in intents),
        "skill_route_confidence": "|".join(
            f"{float(_route_value(i, 'confidence', 0.0)):.4f}" for i in intents
        ),
        "skill_candidates": "|".join(
            ",".join(_route_value(i, "candidate_skill_ids", []) or []) for i in intents
        ),
        "skill_reason": "|".join((_route_value(i, "reason") or "") for i in intents),
        "intent_json": json.dumps(result.model_dump(), ensure_ascii=False),
    }


def attach_results(df: pd.DataFrame, results: List[SemanticResult]) -> pd.DataFrame:
    out = df.copy()
    flat = pd.DataFrame([flatten_result(r) for r in results], index=out.index)
    for col in flat.columns:
        out[col] = flat[col]
    return out
