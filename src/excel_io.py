"""读取三列问答输入，并在原 Excel 右侧追加五维评分。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill

from critic import score_and_decide


DEFAULT_COLUMNS = {
    "original_query": "原query",
    "original_answer": "原answer",
    "new_query": "新query",
}
OUTPUT_HEADERS = (
    "语言自然分",
    "表述清晰分",
    "内部逻辑分",
    "原意忠实分",
    "原答案匹配分",
    "CRITIC总分",
    "评测结果",
    "评分说明",
)
DIMENSION_LABELS = (
    ("language", "语言自然"),
    ("clarity", "表述清晰"),
    ("logic", "内部逻辑"),
    ("fidelity", "原意忠实"),
    ("answer", "原答案匹配"),
)


def _check_source(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".xlsx":
        raise ValueError("输入文件仅支持 .xlsx 格式")
    if not path.is_file():
        raise ValueError(f"找不到输入 Excel：{path}")
    return path


def _selected_sheet(book: Any, sheet_name: str) -> Any:
    if sheet_name:
        if sheet_name not in book.sheetnames:
            raise ValueError(f"找不到工作表：{sheet_name}")
        return book[sheet_name]
    return book.worksheets[0]


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def read_input_sheet(
    path: Path, columns: dict[str, str], sheet_name: str = ""
) -> tuple[list[dict[str, Any]], str]:
    """第一行作表头；返回原 Excel 行号及可发送给模型的三个文本字段。"""
    path = _check_source(path)
    if not isinstance(columns, dict) or set(columns) != set(DEFAULT_COLUMNS):
        raise ValueError("columns 必须包含 original_query、original_answer、new_query 三个映射")
    if any(not isinstance(value, str) or not value.strip() for value in columns.values()):
        raise ValueError("columns 中的列名必须是非空字符串")
    column_names = {key: value.strip() for key, value in columns.items()}
    if len(set(column_names.values())) != 3:
        raise ValueError("原 query、原 answer、新 query 必须映射到三个不同的列名")

    formula_book = load_workbook(path, read_only=True, data_only=False)
    cached_book = None
    try:
        sheet = _selected_sheet(formula_book, sheet_name)
        chosen_name = sheet.title
        first_row = next(sheet.iter_rows(min_row=1, max_row=1), None)
        if first_row is None or all(_empty(cell.value) for cell in first_row):
            raise ValueError(f"工作表“{chosen_name}”没有表头，请在第一行填写配置列名")
        headers = [
            str(cell.value).strip() if cell.value is not None else ""
            for cell in first_row
        ]
        positions: dict[str, int] = {}
        for key, name in column_names.items():
            matches = [index for index, header in enumerate(headers) if header == name]
            if not matches:
                raise ValueError(f"工作表“{chosen_name}”缺少配置列：{name}")
            if len(matches) != 1:
                raise ValueError(f"工作表“{chosen_name}”的配置列名重复：{name}")
            positions[key] = matches[0]

        # 单独读取缓存值；写出时始终重新打开原始公式工作簿。
        cached_book = load_workbook(path, read_only=True, data_only=True)
        cached_sheet = cached_book[chosen_name]
        rows: list[dict[str, Any]] = []
        cached_rows = cached_sheet.iter_rows(min_row=2, max_col=sheet.max_column)
        formula_rows = sheet.iter_rows(min_row=2, max_col=sheet.max_column)
        for excel_row, (cells, cached_cells) in enumerate(zip(formula_rows, cached_rows), 2):
            if all(_empty(cell.value) for cell in cells):
                continue
            texts: dict[str, str] = {}
            errors: list[str] = []
            for key, position in positions.items():
                cell = cells[position]
                cached_cell = cached_cells[position]
                value = cached_cell.value if cell.data_type == "f" else cell.value
                name = column_names[key]
                if cell.data_type == "f" and value is None:
                    errors.append(f"列“{name}”是公式且没有缓存结果，请先用 Excel 计算并保存")
                elif cell.data_type == "e" or cached_cell.data_type == "e":
                    errors.append(f"列“{name}”包含 Excel 错误值：{value}")
                elif _empty(value):
                    errors.append(f"列“{name}”为空")
                texts[key] = "" if value is None else str(value).strip()
            rows.append(
                {
                    "candidate_id": f"R{excel_row:06d}",
                    "excel_row": excel_row,
                    "q0": texts["original_query"],
                    "a0": texts["original_answer"],
                    "q1": texts["new_query"],
                    "input_error": "；".join(errors),
                }
            )
        return rows, chosen_name
    finally:
        formula_book.close()
        if cached_book is not None:
            cached_book.close()


def _new_headers(existing: list[Any]) -> list[str]:
    used = {str(value).strip() for value in existing if value is not None}
    result: list[str] = []
    for header in OUTPUT_HEADERS:
        chosen = header
        suffix = 2
        while chosen in used:
            chosen = f"{header}（{suffix}）"
            suffix += 1
        used.add(chosen)
        result.append(chosen)
    return result


def _set_value(cell: Any, value: Any) -> None:
    cell.value = value
    if isinstance(value, str):
        # 新增的模型文本是说明内容，不能被 Excel 当作公式执行。
        cell.data_type = "s"


def write_scored_excel(
    source: Path,
    destination: Path,
    rows: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
    sheet_name: str,
) -> None:
    """保留源工作簿，追加评分列，以原子替换方式写入另一个 .xlsx 文件。"""
    source = _check_source(source)
    destination = Path(destination).expanduser().resolve()
    if destination.suffix.lower() != ".xlsx":
        raise ValueError("输出文件必须是 .xlsx 格式")
    if source == destination or (destination.exists() and source.samefile(destination)):
        raise ValueError("输入和输出不能是同一个文件，请为 output 配置新的 Excel 路径")

    book = load_workbook(source, data_only=False)
    temporary_path: Path | None = None
    try:
        sheet = _selected_sheet(book, sheet_name)
        original_column_count = sheet.max_column
        if original_column_count + len(OUTPUT_HEADERS) > 16384:
            raise ValueError("工作表右侧没有足够的列用于追加评分")
        headers = _new_headers([cell.value for cell in sheet[1]])
        for column, header in enumerate(headers, original_column_count + 1):
            cell = sheet.cell(row=1, column=column)
            _set_value(cell, header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(fill_type="solid", fgColor="DCEAF7")

        for row in rows:
            result: list[Any] = [None] * len(OUTPUT_HEADERS)
            prediction = predictions.get(row["candidate_id"])
            if row.get("input_error"):
                result[6:] = ["输入不完整", row["input_error"]]
            elif prediction is None:
                result[6] = "未评分"
            elif "error" in prediction:
                result[6:] = ["评分失败", str(prediction["error"])]
            else:
                scores = prediction["scores"]
                total, decision = score_and_decide(scores)
                reasons = prediction["reasons"]
                result[:5] = ["无法判定" if score is None else score for score in scores]
                result[5:7] = [total, decision]
                result[7] = "；".join(
                    f"{label}：{reasons[key]}" for key, label in DIMENSION_LABELS
                )
            for column, value in enumerate(result, original_column_count + 1):
                cell = sheet.cell(row=row["excel_row"], column=column)
                _set_value(cell, value)
                if column == original_column_count + 6:
                    cell.number_format = "0.0000"

        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=".xlsx", dir=destination.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        book.save(temporary_path)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        book.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
