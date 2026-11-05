"""五维评测最关键的本地校验；不发起模型请求。"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent))

from critic import FROZEN_WEIGHTS, THRESHOLD, fit_weights, score_and_decide  # noqa: E402
from evaluate import (  # noqa: E402
    DIMENSIONS,
    agreement,
    approved_rows,
    composite,
    load_config,
    parse_model_content,
    read_review_sheet,
    response_format_for_model,
    resolve_settings,
    run,
    user_payload,
)
from report import render_report  # noqa: E402


class CompositeTests(unittest.TestCase):
    def test_all_decision_branches_and_unassessable_content(self) -> None:
        examples = [
            ([3, 3, 3, 3, 3], (5, "通过")),
            ([2, 3, 3, 3, 3], (4, "通过")),
            ([3, 2, 3, 3, 3], (3, "人工复核")),
            ([3, 3, 1, 3, 3], (2, "拒绝")),
            ([1, 2, None, None, None], (1, "拒绝")),
        ]
        for scores, expected in examples:
            with self.subTest(scores=scores):
                self.assertEqual(composite(scores), expected)
        with self.assertRaisesRegex(ValueError, "只有语言或清晰度为 1"):
            composite([3, 3, None, 3, 3])


class CriticTests(unittest.TestCase):
    def test_frozen_weights_reproduce_accepted_development_scores(self) -> None:
        source = Path(__file__).resolve().parents[1] / "voice_ruler2_test" / "第三任务_40条五维预评分.csv"
        with source.open(encoding="utf-8-sig", newline="") as handle:
            vectors = [
                [int(row[dimension[2]]) for dimension in DIMENSIONS]
                for row in csv.DictReader(handle)
            ]
        self.assertEqual(len(vectors), 40)
        weights = fit_weights(vectors)
        self.assertAlmostEqual(sum(weights), 1.0)
        for observed, frozen in zip(weights, FROZEN_WEIGHTS):
            self.assertAlmostEqual(observed, frozen, places=6)

    def test_fit_requires_enough_variation_to_estimate_correlations(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少需要 2 条"):
            fit_weights([[3, 3, 3, 3, 3]])
        with self.assertRaisesRegex(ValueError, "没有变化"):
            fit_weights([[3, 3, 3, 3, 3], [3, 3, 3, 3, 3]])

    def test_one_point_veto_overrides_high_weighted_total(self) -> None:
        self.assertEqual(THRESHOLD, 0.8)
        for scores, expected_total in (
            ([3, 1, 3, 3, 3], 1 - FROZEN_WEIGHTS[1]),
            ([3, 3, 1, 3, 3], 1 - FROZEN_WEIGHTS[2]),
        ):
            with self.subTest(scores=scores):
                total, decision = score_and_decide(scores)
                self.assertAlmostEqual(total, expected_total)
                self.assertGreater(total, THRESHOLD)
                self.assertEqual(decision, "拒绝")

    def test_threshold_and_unassessable_content(self) -> None:
        pass_total, pass_decision = score_and_decide([3, 3, 3, 2, 3])
        reject_total, reject_decision = score_and_decide([3, 3, 3, 2, 2])
        self.assertGreater(pass_total, THRESHOLD)
        self.assertEqual(pass_decision, "通过")
        self.assertLess(reject_total, THRESHOLD)
        self.assertEqual(reject_decision, "拒绝")
        self.assertEqual(score_and_decide([1, 2, None, None, None]), (None, "拒绝"))
        with self.assertRaisesRegex(ValueError, "无法判定|只有语言或清晰度为 1"):
            score_and_decide([3, 3, None, 3, 3])


class MetricTests(unittest.TestCase):
    def test_kappa_exact_and_systematic_shift(self) -> None:
        perfect = agreement([1, 2, 3], [1, 2, 3])
        self.assertEqual(perfect["exact"], 1)
        self.assertEqual(perfect["mae"], 0)
        self.assertEqual(perfect["kappa"], 1)
        self.assertEqual(perfect["matrix"], [[1, 0, 0], [0, 1, 0], [0, 0, 1]])

        shifted = agreement([1, 2], [2, 3])
        self.assertEqual(shifted["exact"], 0)
        self.assertEqual(shifted["mae"], 1)
        self.assertAlmostEqual(shifted["kappa"], 0)

        constant = agreement([3, 3], [3, 3])
        self.assertIsNone(constant["kappa"])
        self.assertEqual(constant["exact"], 1)


class ReviewSheetTests(unittest.TestCase):
    @staticmethod
    def make_workbook(path: Path, statuses: list[str]) -> None:
        headers = [
            "candidate_id", "seed_id", "open_question", "open_answer",
            "candidate_question", "intended_label", "user_final_label", "user_review_status",
        ]
        headers += [dimension[2] for dimension in DIMENSIONS]
        headers += [dimension[3] for dimension in DIMENSIONS]
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(headers)
        ids = ["S001_G", "S002_B", "S003_G"]
        for index, status in enumerate(statuses):
            data = {
                "candidate_id": ids[index], "seed_id": f"S{index + 1:03d}",
                "open_question": "What is X?", "open_answer": "X is Y.",
                "candidate_question": "What exactly is X?", "intended_label": "good",
                "user_final_label": "bad" if index == 1 and status == "修改" else None,
                "user_review_status": status,
                **{dimension[2]: 3 for dimension in DIMENSIONS},
                "user_fidelity_score": 1 if index == 1 and status == "修改" else None,
            }
            sheet.append([data.get(key) for key in headers])
        workbook.save(path)

    def test_unreviewed_row_blocks_gold(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "review.xlsx"
            self.make_workbook(path, ["", "存疑"])
            rows, counts = read_review_sheet(path)
            self.assertEqual(counts["待审核"], 1)
            with self.assertRaisesRegex(ValueError, "待审核"):
                approved_rows(rows)

    def test_accepted_modified_and_uncertain_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "review.xlsx"
            self.make_workbook(path, ["同意", "修改", "存疑"])
            rows, counts = read_review_sheet(path)
            self.assertEqual(dict(counts), {"同意": 1, "修改": 1, "存疑": 1})
            approved = approved_rows(rows)
            self.assertEqual(len(approved), 2)
            self.assertEqual(approved[0]["human"], [3, 3, 3, 3, 3])
            self.assertEqual(approved[0]["human_label"], "good")
            self.assertEqual(approved[1]["human"], [3, 3, 3, 1, 3])
            self.assertEqual(approved[1]["human_label"], "bad")
            self.assertEqual(approved[1]["human_total"], 2)


class BlindScoringTests(unittest.TestCase):
    def test_deepseek_uses_json_object_with_local_validation(self) -> None:
        self.assertEqual(response_format_for_model("deepseek-v4-pro"), {"type": "json_object"})
        self.assertEqual(response_format_for_model("deepseek-v4.1-flash-1017"), {"type": "json_object"})
        self.assertEqual(response_format_for_model("qwen3.7-plus")["type"], "json_schema")

    def test_only_three_text_fields_reach_model(self) -> None:
        payload = user_payload({
            "candidate_id": "S001_B", "intended_label": "bad", "human": [1, 2, 3, 3, 3],
            "q0": "Original?", "a0": "Answer.", "q1": "Rewrite?",
        })
        self.assertEqual(json.loads(payload), {"Q0": "Original?", "A0": "Answer.", "Q1": "Rewrite?"})
        self.assertNotIn("S001_B", payload)
        self.assertNotIn("bad", payload)

    def test_invalid_model_scores_are_rejected(self) -> None:
        base = {dimension[0]: {"score": 3, "reason": "Reason."} for dimension in DIMENSIONS}
        self.assertEqual(parse_model_content(json.dumps(base))[0], [3] * 5)
        base["logic"]["score"] = 4
        with self.assertRaisesRegex(ValueError, "超出"):
            parse_model_content(json.dumps(base))
        base["logic"]["score"] = None
        with self.assertRaisesRegex(ValueError, "只有语言或清晰度为 1"):
            parse_model_content(json.dumps(base))
        base["language"]["score"] = 1
        self.assertEqual(parse_model_content(json.dumps(base))[0], [1, 3, None, 3, 3])


class YamlConfigTests(unittest.TestCase):
    def test_load_config_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / "config.yaml"
            config.write_text(
                'api_url: "https://example.invalid/v1/chat/completions"\n'
                'api_key: "test-file-key"\n'
                'model: "yaml-model"\n'
                'input: "review.xlsx"\n'
                'out_dir: "results"\n',
                encoding="utf-8",
            )
            self.assertEqual(load_config(config)["model"], "yaml-model")
            args = Namespace(command="run", config=config, model=None, api_url=None, input=None, out_dir=None)
            settings = resolve_settings(args)
            self.assertEqual(settings.model, "yaml-model")
            self.assertEqual(settings.api_url, "https://example.invalid/v1/chat/completions")
            self.assertEqual(settings.api_key, "test-file-key")
            self.assertEqual(settings.input, base / "review.xlsx")
            self.assertEqual(settings.out_dir, base / "results")
            self.assertIsNone(args.model)  # 解析配置不修改原命令行对象

    def test_cli_overrides_yaml_but_yaml_key_takes_priority(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / "config.yaml"
            config.write_text(
                'api_url: "https://yaml.invalid/chat/completions"\n'
                'api_key: "yaml-test-key"\n'
                'model: "yaml-model"\n'
                'input: "review.xlsx"\n'
                'out_dir: "results"\n',
                encoding="utf-8",
            )
            cli_input = base / "cli.xlsx"
            cli_output = base / "cli-results"
            args = Namespace(command="run", config=config, model="cli-model", api_url="https://cli.invalid/chat/completions", input=cli_input, out_dir=cli_output)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "env-test-key"}):
                settings = resolve_settings(args)
            self.assertEqual(settings.model, "cli-model")
            self.assertEqual(settings.api_url, "https://cli.invalid/chat/completions")
            self.assertEqual(settings.api_key, "yaml-test-key")
            self.assertEqual(settings.input, cli_input)
            self.assertEqual(settings.out_dir, cli_output)

    def test_empty_yaml_key_uses_environment_and_bad_yaml_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.yaml"
            config.write_text('api_key: ""\nmodel: "yaml-model"\n', encoding="utf-8")
            args = Namespace(command="run", config=config, model=None, api_url=None, input=None, out_dir=None)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "env-test-key"}):
                self.assertEqual(resolve_settings(args).api_key, "env-test-key")
            config.write_text('api_key: [broken\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config)
            with self.assertRaises(ValueError):
                load_config(Path(folder) / "missing.yaml")

    def test_unreviewed_sheet_stops_before_missing_key_or_model(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            config = base / "config.yaml"
            config.write_text('input: "review.xlsx"\napi_key: ""\nmodel: ""\n', encoding="utf-8")
            ReviewSheetTests.make_workbook(base / "review.xlsx", [""])
            args = Namespace(command="run", config=config, model=None, api_url=None, input=None, out_dir=None)
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                with patch("evaluate.post_completion") as mock_api:
                    with self.assertRaisesRegex(ValueError, "待审核"):
                        run(args)
            mock_api.assert_not_called()


class EndToEndMockTests(unittest.TestCase):
    def test_review_to_html_report_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            source = temp / "模拟样本.xlsx"
            output = temp / "output"
            config = temp / "config.yaml"
            config.write_text('api_key: "test-only-key"\n', encoding="utf-8")
            ReviewSheetTests.make_workbook(source, ["同意", "修改", "存疑"])
            five_scores = {dimension[0]: {"score": 3, "reason": "同一目标。"} for dimension in DIMENSIONS}
            fake_response = {
                "model": "fake-model",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(five_scores, ensure_ascii=False)}}],
            }
            args = Namespace(
                command="run", config=config, input=source, out_dir=output,
                model="fake-model", api_url="https://example.invalid/chat/completions",
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "env-should-not-be-used"}):
                with patch("evaluate.post_completion", return_value=json.dumps(fake_response, ensure_ascii=False)) as mock_api:
                    run(args)
            self.assertEqual(mock_api.call_count, 2)  # 存疑行不进入盲评
            self.assertEqual(mock_api.call_args.args[1], "test-only-key")
            html = (output / "评测报告.html").read_text(encoding="utf-8")
            self.assertIn("模拟样本.xlsx", html)
            self.assertIn("五维一致性", html)
            self.assertIn("综合分", html)
            self.assertIn("CRITIC", html)
            csv_path = output / "逐条评分对照.csv"
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                comparison = {row["candidate_id"]: row for row in csv.DictReader(handle)}
            self.assertEqual(set(comparison), {"S001_G", "S002_B"})
            self.assertEqual(comparison["S001_G"]["critic_reference_decision"], "通过")
            self.assertEqual(comparison["S002_B"]["critic_reference_decision"], "拒绝")
            self.assertEqual(comparison["S002_B"]["critic_model_decision"], "通过")
            self.assertAlmostEqual(float(comparison["S002_B"]["critic_reference_score"]), 1 - FROZEN_WEIGHTS[3], places=6)
            self.assertAlmostEqual(float(comparison["S002_B"]["critic_model_score"]), 1.0)
            self.assertEqual(len((output / "model_scores.jsonl").read_text(encoding="utf-8").splitlines()), 2)
            for name in ("manifest.json", "model_scores.jsonl", "attempts.jsonl", "评测报告.html"):
                self.assertNotIn("test-only-key", (output / name).read_text(encoding="utf-8"))


class HtmlSafetyTests(unittest.TestCase):
    def test_dynamic_text_is_escaped(self) -> None:
        html = render_report({
            "status": "pending", "title": "<script>alert(1)</script>",
            "note": "<img src=x onerror=alert(1)>", "review_counts": {},
        })
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
