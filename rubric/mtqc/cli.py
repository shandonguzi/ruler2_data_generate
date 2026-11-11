"""Multi-turn QA quality control: preview / run / export."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import JUDGES
from .adapters import read_inputs
from .config import load_config
from .export import export_all, export_case_result
from .llm_client import LLMClient
from .orchestrator import Orchestrator
from .records import Records, digest
from .run_output import existing_run_config, new_run_config
from .schemas import REPAIR_SCHEMA, judge_schema


def validate_output_paths(config: dict) -> None:
    for key, value in config["output"].items():
        path = Path(value)
        ancestor = path.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ValueError(f"Output parent is not a directory: {ancestor}")
        if path.exists() and ((key == "records_dir") != path.is_dir()):
            raise ValueError(f"Invalid output path: {path}")


def preview(config: dict) -> dict:
    items = read_inputs(config)
    valid = sum(item["original_case"] is not None for item in items)
    rounds = config["repair"]["max_rounds"] if config["repair"]["enabled"] else 0
    validate_output_paths(config)
    return {
        "cases": len(items), "valid_cases": valid, "input_errors": len(items) - valid,
        "max_judge_tasks": valid * len(JUDGES) * (rounds + 1), "max_repair_tasks": valid * rounds,
        "max_api_requests": valid * (len(JUDGES) * (rounds + 1) + rounds) * config["llm"]["max_attempts"],
        "invalid_inputs": [{"case_id": item["case_id"], "source": item["source"], "errors": item["input_errors"]} for item in items if item["input_errors"]],
    }


def format_case_summary(result: dict) -> str:
    revisions = result["revisions"]
    initial = revisions[0] if revisions else None
    final = next((revision for revision in revisions if revision["revision_id"] == result["final_revision_id"]), None)

    def score_label(revision, name):
        if revision is None:
            return "—"
        judge = revision["evaluation"]["judge_results"][name]
        if judge["score"] is not None:
            return str(judge["score"])
        return {"ERROR": "错误", "SKIPPED": "跳过"}.get(judge["status"], "—")

    fields = []
    for name, label in zip(JUDGES, ("因子", "问答", "连贯", "画像")):
        before, after = score_label(initial, name), score_label(final, name)
        fields.append(f"{label} {after if before == after else before + '→' + after}")
    if result["eligible_for_ingestion"]:
        outcome = "✓ 可入库"
    elif result["stop_reason"] in ("JUDGE_ERROR", "REPAIR_ERROR"):
        outcome = "× 评估错误" if result["stop_reason"] == "JUDGE_ERROR" else "× 修复错误"
    elif result["stop_reason"] == "INTERRUPTED":
        outcome = "… 已中断"
    else:
        outcome = {"REJECT": "× 不通过", "REPAIR": "! 待修复", "INPUT_ERROR": "× 输入错误"}.get(
            result["overall_decision"], "? 待复核")
    if result["repair_attempt_count"]:
        outcome += f" · 修复 {result['repair_attempt_count']} 轮"
    return "      " + "  │  ".join([*fields, outcome])


def run(config: dict, transport=None) -> list:
    validate_output_paths(config)
    input_hash = hashlib.sha256(Path(config["input"]["path"]).read_bytes()).hexdigest()
    items = read_inputs(config)
    if hashlib.sha256(Path(config["input"]["path"]).read_bytes()).hexdigest() != input_hash:
        raise ValueError("Input changed while being read; retry after the producer finishes writing")
    schema_hashes = {item["input_id"]: {name: digest(judge_schema(name, item["original_case"])) for name in JUDGES}
                     for item in items if item["original_case"] is not None}
    schema_hashes["dialogue_repair"] = digest(REPAIR_SCHEMA)
    with Records(config, input_hash, schema_hashes=schema_hashes) as records:
        saved = {item["input_id"] for item in records.select("original_cases")}
        for item in items:
            if item["input_id"] not in saved:
                records.append("original_cases", item)
        engine = Orchestrator(config, records, LLMClient(config, records, transport))
        try:
            for index, item in enumerate(items, 1):
                print(f"[{index}/{len(items)}] {item['case_id'] or item['input_id']}", flush=True)
                try:
                    engine.run_case(item)
                finally:
                    print(format_case_summary(export_case_result(item, engine)), flush=True)
        finally:
            # Includes interrupted versions and successful dimensions; all data come from disk events.
            results = export_all(engine)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "run", "export"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path,
                        help="Resume a selected run or export it; defaults to a new run or the latest export")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "preview":
            if args.run_dir is not None:
                raise ValueError("--run-dir is only supported for run and export")
            print(json.dumps(preview(config), ensure_ascii=False, indent=2))
        elif args.command == "run":
            validate_output_paths(config)
            config = new_run_config(config) if args.run_dir is None else existing_run_config(config, args.run_dir)
            print(f"输出目录：{Path(config['output']['results_jsonl']).parent}", flush=True)
            results = run(config)
            print(f"\n已导出 {len(results)} 个 Case，可入库 {sum(result['eligible_for_ingestion'] for result in results)} 个。")
            if any(result["stop_reason"] in ("JUDGE_ERROR", "REPAIR_ERROR") for result in results):
                return 1
        else:
            config = existing_run_config(config, args.run_dir)
            print(f"输出目录：{Path(config['output']['results_jsonl']).parent}", flush=True)
            with Records(config, create=False) as records:
                engine = Orchestrator(config, records, None)
                results = export_all(engine)
            for index, result in enumerate(results, 1):
                print(f"[{index}/{len(results)}] {result['case_id'] or result['input_id']}")
                print(format_case_summary(result))
            print(f"\n已从记录导出 {len(results)} 个 Case；未调用模型。")
    except KeyboardInterrupt:
        print("已中断；使用 --run-dir 指定本次输出目录可恢复，尝试预算不会重置。", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
