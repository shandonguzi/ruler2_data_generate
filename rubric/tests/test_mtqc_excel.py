import json
from pathlib import Path
from copy import copy

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from rubric.mtqc.cli import run
from rubric.mtqc.export import write_excel
from conftest import FakeAPI


def test_excel_preserves_original_workbook_and_appends(settings, case, tmp_path):
    config = settings()
    source = tmp_path / "source.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "输入"
    sheet.append(["样本", "公式", "备注"])
    sheet.append([json.dumps(case, ensure_ascii=False), "=1+2", "original"])
    sheet["C2"].font = Font(bold=True, color="FF0000")
    sheet.freeze_panes = "B2"
    sheet.column_dimensions["C"].width = 40
    sheet.auto_filter.ref = "A1:C2"
    other = book.create_sheet("其他")
    other["A1"] = "=SUM(1,2)"
    book.save(source)
    book.close()
    original_bytes = source.read_bytes()
    config["input"] = {"format": "excel", "path": str(source), "columns": {"case": "样本"}, "sheet_name": "输入"}
    results = run(config, FakeAPI(config))
    # Even malicious feedback stays a string when written to Excel/HTML.
    results[0]["case_id"] = "=HYPERLINK(\"https://example.com\")"
    write_excel(config, results)
    assert source.read_bytes() == original_bytes
    before, after = load_workbook(source), load_workbook(config["output"]["excel_path"])
    try:
        for name in before.sheetnames:
            for row in before[name]:
                for cell in row:
                    actual = after[name][cell.coordinate]
                    assert actual.value == cell.value
                    assert actual.data_type == cell.data_type
                    assert copy(actual.font) == copy(cell.font)
        assert after["输入"].freeze_panes == "B2"
        assert after["输入"].column_dimensions["C"].width == 40
        assert after["输入"].auto_filter.ref == "A1:C2"
        assert after["输入"].max_column > 3
        assert {"Case汇总", "版本明细"} <= set(after.sheetnames)
        assert after["Case汇总"]["A2"].data_type == "s"
    finally:
        before.close()
        after.close()
    assert "完整版本与失败因子详情" in Path(config["output"]["report_path"]).read_text()


def test_excel_six_columns_and_uncached_formula_row(settings, case, tmp_path):
    from rubric.mtqc.adapters import read_inputs
    config = settings()
    source = tmp_path / "six.xlsx"
    book = Workbook()
    sheet = book.active
    fields = list(case)
    sheet.append(fields)
    sheet.append([case[key] if key == "case_id" else json.dumps(case[key], ensure_ascii=False) for key in fields])
    sheet.append(['="uncached-formula"'])
    book.save(source)
    book.close()
    config["input"] = {"format": "excel", "path": str(source), "columns": dict(zip(fields, fields))}
    items = read_inputs(config)
    assert len(items) == 2
    assert items[0]["original_case"] is not None
    assert items[1]["input_errors"]


def test_excel_datetime_cell_is_an_input_error_without_breaking_export(settings, tmp_path):
    from datetime import datetime
    config = settings()
    source = tmp_path / "dates.xlsx"
    book = Workbook()
    book.active.append(["case_json"])
    book.active.append([datetime(2026, 11, 10)])
    book.save(source)
    book.close()
    config["input"] = {"format": "excel", "path": str(source), "columns": {"case": "case_json"}}
    api = FakeAPI(config)
    result = run(config, api)[0]
    assert result["overall_decision"] == "INPUT_ERROR"
    assert not api.calls
