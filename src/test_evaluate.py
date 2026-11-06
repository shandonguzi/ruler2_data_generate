"""Excel 评分的离线校验：所有模型响应均为 mock，不发起网络请求。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from copy import copy
from pathlib import Path
from unittest.mock import patch

import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

sys.path.insert(0, str(Path(__file__).resolve().parent))

from critic import FROZEN_WEIGHTS, THRESHOLD, score_and_decide  # noqa: E402
from evaluate import (  # noqa: E402
    DIMENSIONS, load_config, parse_model_content, response_format_for_model,
    resolve_settings, run, user_payload,
)
from excel_io import DEFAULT_COLUMNS, read_input_sheet, write_scored_excel  # noqa: E402


SCORE_HEADERS = ["语言自然分", "表述清晰分", "内部逻辑分", "原意忠实分", "原答案匹配分"]
CUSTOM_COLUMNS = {"original_query": "原问题", "original_answer": "答案", "new_query": "改写"}


def prediction(scores: list[int | None], reason: str = "符合评分标准。") -> dict:
    return {"scores": scores, "reasons": {dimension[0]: reason for dimension in DIMENSIONS}}


def api_response(scores: list[int | None]) -> str:
    content = {
        dimension[0]: {"score": score, "reason": "按同一标准判断。"}
        for dimension, score in zip(DIMENSIONS, scores)
    }
    return json.dumps({
        "model": "fake-model",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)}}],
    }, ensure_ascii=False)


def make_input(path: Path, *, complex_sheet: bool = False) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "输入"
    if complex_sheet:
        sheet.append(["编号", "改写", "备注", "答案", "原问题", "计算列", "评测结果"])
        sheet.append(["id-2", "good?", "原备注", "Answer", "Original?", "=1+2", "旧结果"])
        sheet.append([None] * 7)
        sheet.append(["id-4", "veto?", "", "Answer", "Original?", 42, "保持"])
        sheet.append(["id-5", "incomplete?", "缺少答案", None, "Original?", 0, "原值"])
        sheet.append(["id-6", "unassessable?", "最后一条", "Answer", "Original?", True, "末行"])
        sheet["C2"].font = Font(bold=True, color="FF123456")
        sheet["C2"].fill = PatternFill("solid", fgColor="FFFFFF00")
        sheet.column_dimensions["C"].width = 30
        sheet.row_dimensions[2].height = 38
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = "A1:G6"
        extra = book.create_sheet("说明")
        extra["A1"] = "请保留其他工作表"
        extra["B2"] = "=SUM(1,2)"
    else:
        sheet.append(list(DEFAULT_COLUMNS.values()))
        sheet.append(["Original?", "Answer", "Rewrite?"])
    book.save(path)
    book.close()


def make_config(path: Path, **changes) -> Path:
    config = {
        "api_url": "https://example.invalid/v1/chat/completions", "api_key": "test-only-key",
        "model": "fake-model", "input": "input.xlsx", "output": "output/评分结果.xlsx",
        "columns": dict(DEFAULT_COLUMNS),
    }
    config.update(changes)
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def args_for(config: Path, **changes) -> Namespace:
    values = {"command": "run", "config": config, "input": None, "output": None, "model": None, "api_url": None}
    values.update(changes)
    return Namespace(**values)


class ExcelInputTests(unittest.TestCase):
    def test_custom_columns_and_noncontiguous_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.xlsx"
            make_input(source, complex_sheet=True)
            rows, selected = read_input_sheet(source, CUSTOM_COLUMNS)
            self.assertEqual(selected, "输入")
            self.assertEqual([row["excel_row"] for row in rows], [2, 4, 5, 6])
            self.assertEqual([row["candidate_id"] for row in rows], ["R000002", "R000004", "R000005", "R000006"])
            self.assertEqual((rows[0]["q0"], rows[0]["a0"], rows[0]["q1"]), ("Original?", "Answer", "good?"))
            self.assertTrue(rows[2]["input_error"])
            self.assertEqual(rows[0]["input_error"], "")

    def test_selected_sheet_and_bad_headers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.xlsx"
            book = Workbook()
            book.active.title = "非数据"
            sheet = book.create_sheet("数据")
            sheet.append(list(DEFAULT_COLUMNS.values()))
            sheet.append(["Q", "A", "Q1"])
            book.save(source)
            book.close()
            rows, selected = read_input_sheet(source, DEFAULT_COLUMNS, "数据")
            self.assertEqual(selected, "数据")
            self.assertEqual(rows[0]["q0"], "Q")
            with self.assertRaises(ValueError):
                read_input_sheet(source, DEFAULT_COLUMNS, "不存在")
            with self.assertRaises(ValueError):
                read_input_sheet(source, DEFAULT_COLUMNS)
            book = load_workbook(source)
            book["数据"]["D1"] = DEFAULT_COLUMNS["original_query"]
            book.save(source)
            book.close()
            with self.assertRaises(ValueError):
                read_input_sheet(source, DEFAULT_COLUMNS, "数据")

    def test_duplicate_mapping_and_uncached_input_formula(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.xlsx"
            make_input(source)
            duplicate = {**DEFAULT_COLUMNS, "new_query": DEFAULT_COLUMNS["original_query"]}
            with self.assertRaises(ValueError):
                read_input_sheet(source, duplicate)
            book = load_workbook(source)
            book.active["A2"] = '=CONCAT("Orig", "inal?")'
            book.save(source)
            book.close()
            rows, _ = read_input_sheet(source, DEFAULT_COLUMNS)
            self.assertTrue(rows[0]["input_error"])


class ExcelOutputTests(unittest.TestCase):
    def test_original_workbook_survives_appending_scores(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, output = base / "input.xlsx", base / "result.xlsx"
            make_input(source, complex_sheet=True)
            original_bytes = source.read_bytes()
            rows, sheet_name = read_input_sheet(source, CUSTOM_COLUMNS)
            predictions = {
                "R000002": prediction([3, 3, 3, 2, 3]),
                "R000004": prediction([3, 1, 3, 3, 3]),
                "R000006": prediction([1, 2, None, None, None], "=1+1"),
            }
            write_scored_excel(source, output, rows, predictions, sheet_name)
            self.assertEqual(source.read_bytes(), original_bytes)
            old, new = load_workbook(source), load_workbook(output)
            self.assertEqual(old.sheetnames, new.sheetnames)
            for name in old.sheetnames:
                for row in old[name]:
                    for cell in row:
                        actual = new[name][cell.coordinate]
                        self.assertEqual(actual.value, cell.value)
                        if cell.value is not None:
                            self.assertEqual(actual.data_type, cell.data_type)
                        self.assertEqual(copy(actual.font), copy(cell.font))
                        self.assertEqual(copy(actual.fill), copy(cell.fill))
            sheet = new["输入"]
            self.assertEqual(sheet.freeze_panes, "B2")
            self.assertEqual(sheet.auto_filter.ref, "A1:G6")
            self.assertEqual(sheet.column_dimensions["C"].width, 30)
            self.assertEqual(sheet.row_dimensions[2].height, 38)
            self.assertEqual(sheet.max_column, 7 + 8)
            appended = [cell.value for cell in sheet[1]][7:]
            self.assertEqual(appended[:5], SCORE_HEADERS)
            self.assertEqual(appended[5:], ["CRITIC总分", "评测结果（2）", "评分说明"])
            self.assertAlmostEqual(sheet.cell(2, 13).value, 1 - FROZEN_WEIGHTS[3] / 2)
            self.assertEqual(sheet.cell(2, 14).value, "通过")
            self.assertEqual(sheet.cell(4, 14).value, "拒绝")
            self.assertEqual(sheet.cell(5, 14).value, "输入不完整")
            self.assertEqual(sheet.cell(6, 14).value, "拒绝")
            self.assertIn("无法判定", [sheet.cell(6, column).value for column in range(8, 13)])
            self.assertIsNone(sheet.cell(6, 13).value)
            self.assertTrue(all(sheet.cell(3, column).value is None for column in range(8, 16)))
            self.assertNotEqual(sheet.cell(6, 15).data_type, "f")
            old.close()
            new.close()

    def test_unscored_and_failed_rows_are_not_quality_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, output = base / "input.xlsx", base / "result.xlsx"
            make_input(source)
            rows, sheet_name = read_input_sheet(source, DEFAULT_COLUMNS)
            write_scored_excel(source, output, rows, {}, sheet_name)
            book = load_workbook(output)
            self.assertEqual(book.active["J2"].value, "未评分")
            book.close()
            write_scored_excel(source, output, rows, {"R000002": {"error": "请求失败"}}, sheet_name)
            book = load_workbook(output)
            self.assertEqual(book.active["J2"].value, "评分失败")
            self.assertIn("请求失败", book.active["K2"].value)
            self.assertIsNone(book.active["I2"].value)
            book.close()
            with self.assertRaises(ValueError):
                write_scored_excel(source, source, rows, {}, sheet_name)


class ScoringContractTests(unittest.TestCase):
    def test_payload_only_contains_three_text_fields(self) -> None:
        payload = user_payload({"candidate_id": "R000002", "q0": "Q", "a0": "A", "q1": "Q1", "other": "private"})
        self.assertEqual(json.loads(payload), {"Q0": "Q", "A0": "A", "Q1": "Q1"})
        self.assertEqual(response_format_for_model("deepseek-v4.1-flash-1017"), {"type": "json_object"})
        self.assertEqual(response_format_for_model("other-model")["type"], "json_schema")

    def test_model_response_must_obey_score_and_null_rules(self) -> None:
        content = {dimension[0]: {"score": 3, "reason": "正常。"} for dimension in DIMENSIONS}
        self.assertEqual(parse_model_content(json.dumps(content))[0], [3] * 5)
        for invalid in (4, True, "3", None):
            with self.subTest(invalid=invalid):
                content["language"]["score"] = invalid
                with self.assertRaises(ValueError):
                    parse_model_content(json.dumps(content))
        content["language"]["score"] = 3
        content["logic"]["score"] = None
        with self.assertRaises(ValueError):
            parse_model_content(json.dumps(content))
        content["language"]["score"] = 1
        self.assertEqual(parse_model_content(json.dumps(content))[0], [1, 3, None, 3, 3])
        content["answer"]["reason"] = ""
        with self.assertRaises(ValueError):
            parse_model_content(json.dumps(content))

    def test_fixed_threshold_and_one_point_veto(self) -> None:
        self.assertEqual(THRESHOLD, 0.8)
        self.assertEqual(score_and_decide([3, 3, 3, 2, 3])[1], "通过")
        self.assertEqual(score_and_decide([3, 3, 3, 2, 2])[1], "拒绝")
        total, decision = score_and_decide([3, 1, 3, 3, 3])
        self.assertGreater(total, THRESHOLD)
        self.assertEqual(decision, "拒绝")


class ConfigTests(unittest.TestCase):
    def test_relative_paths_mapping_and_cli_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = make_config(base / "config.yaml", columns=CUSTOM_COLUMNS, sheet_name="指定工作表")
            settings = resolve_settings(args_for(config))
            self.assertEqual(settings.input, base / "input.xlsx")
            self.assertEqual(settings.output, base / "output" / "评分结果.xlsx")
            self.assertEqual(settings.columns, CUSTOM_COLUMNS)
            self.assertEqual(settings.sheet_name, "指定工作表")
            overrides = args_for(config, input=base / "another.xlsx", output=base / "another-result.xlsx", model="cli-model", api_url="https://cli.invalid/chat/completions")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-key"}):
                settings = resolve_settings(overrides)
            self.assertEqual(settings.input, base / "another.xlsx")
            self.assertEqual(settings.output, base / "another-result.xlsx")
            self.assertEqual(settings.model, "cli-model")
            self.assertEqual(settings.api_url, "https://cli.invalid/chat/completions")
            self.assertEqual(settings.api_key, "test-only-key")

    def test_empty_key_uses_environment_and_invalid_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = make_config(Path(folder) / "config.yaml", api_key="")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-key"}):
                self.assertEqual(resolve_settings(args_for(config)).api_key, "environment-key")
            for content in ('api_key: [broken\n', 'columns: "不是映射"\n', 'columns: {original_query: Q, original_answer: Q, new_query: Q1}\n'):
                with self.subTest(content=content):
                    config.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(config)


class EndToEndTests(unittest.TestCase):
    def test_three_columns_to_excel_without_reference_scores_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "input.xlsx"
            make_input(source, complex_sheet=True)
            original_bytes = source.read_bytes()
            config = make_config(base / "config.yaml", columns=CUSTOM_COLUMNS)
            scores_by_query = {"good?": [3, 3, 3, 2, 3], "veto?": [3, 1, 3, 3, 3], "unassessable?": [1, 2, None, None, None]}

            def fake_api(_url, _key, _model, row):
                return api_response(scores_by_query[row["q1"]])

            with patch("evaluate.post_completion", side_effect=fake_api) as mock_api:
                run(args_for(config))
            self.assertEqual(mock_api.call_count, 3)
            self.assertEqual(source.read_bytes(), original_bytes)
            output = base / "output" / "评分结果.xlsx"
            self.assertTrue(output.is_file())
            book = load_workbook(output)
            sheet = book["输入"]
            self.assertEqual([sheet.cell(row, 14).value for row in (2, 4, 5, 6)], ["通过", "拒绝", "输入不完整", "拒绝"])
            self.assertIsNone(sheet.cell(3, 14).value)
            self.assertEqual(sheet["F2"].value, "=1+2")
            book.close()
            with patch("evaluate.post_completion") as mock_api:
                run(args_for(config))
            mock_api.assert_not_called()
            records = output.parent / (output.stem + "_records")
            self.assertEqual(len((records / "model_scores.jsonl").read_text(encoding="utf-8").splitlines()), 3)
            for log in records.glob("*.json*"):
                self.assertNotIn("test-only-key", log.read_text(encoding="utf-8"))

    def test_preview_and_same_path_preflight_do_not_call_api(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "input.xlsx"
            make_input(source)
            config = make_config(base / "config.yaml", api_key="", model="")
            output = base / "output" / "评分结果.xlsx"
            output.parent.mkdir(parents=True)
            completed = Workbook()
            completed.active.append(["已有正式结果"])
            completed.active.append(["通过"])
            completed.save(output)
            completed.close()
            completed_bytes = output.read_bytes()
            with patch("evaluate.post_completion") as mock_api:
                run(args_for(config, command="preview"))
            mock_api.assert_not_called()
            self.assertEqual(output.read_bytes(), completed_bytes)
            preview = output.with_name(output.stem + "_预览.xlsx")
            book = load_workbook(preview)
            self.assertEqual(book.active["J2"].value, "未评分")
            book.close()
            original_bytes = source.read_bytes()
            with patch("evaluate.post_completion") as mock_api:
                with self.assertRaises(ValueError):
                    run(args_for(config, output=source))
            mock_api.assert_not_called()
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_changed_column_mapping_cannot_reuse_previous_scores(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "input.xlsx"
            make_input(source)
            book = load_workbook(source)
            book.active["D1"] = "另一个原query"
            book.active["D2"] = "Other original?"
            book.save(source)
            book.close()
            config = make_config(base / "config.yaml")
            with patch("evaluate.post_completion", return_value=api_response([3] * 5)):
                run(args_for(config))
            make_config(config, columns={**DEFAULT_COLUMNS, "original_query": "另一个原query"})
            with patch("evaluate.post_completion") as mock_api:
                with self.assertRaises(ValueError):
                    run(args_for(config))
            mock_api.assert_not_called()

    def test_failed_run_saves_partial_excel_and_resume_retries_only_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "input.xlsx"
            make_input(source)
            book = load_workbook(source)
            book.active.append(["Original 2?", "Answer 2", "Rewrite 2?"])
            book.active.append(["Original 3?", "Answer 3", "Rewrite 3?"])
            book.save(source)
            book.close()
            config = make_config(base / "config.yaml")

            def failing_api(_url, _key, _model, row):
                if row["excel_row"] == 2:
                    return api_response([3] * 5)
                raise ValueError("模拟接口失败")

            with patch("evaluate.post_completion", side_effect=failing_api), patch("evaluate.time.sleep"):
                with self.assertRaises(ValueError):
                    run(args_for(config))
            output = base / "output" / "评分结果.xlsx"
            book = load_workbook(output)
            self.assertEqual([book.active.cell(row, 10).value for row in (2, 3, 4)], ["通过", "评分失败", "未评分"])
            book.close()
            with patch("evaluate.post_completion", return_value=api_response([3] * 5)) as mock_api:
                run(args_for(config))
            self.assertEqual(mock_api.call_count, 2)
            book = load_workbook(output)
            self.assertEqual([book.active.cell(row, 10).value for row in (2, 3, 4)], ["通过"] * 3)
            book.close()


if __name__ == "__main__":
    unittest.main()
