from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .config import Config, apply_model_environment, load_config, load_environment, require_models
from .console import configure_logging
from .pipeline import generate, prepare, rededuplicate, revalidate
from .question_types import QUESTION_TYPES
from .remote import Remote


def boolean(value: str) -> bool:
    if value.lower() in {"true", "1", "yes"}:
        return True
    if value.lower() in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("Expected true or false")


def _review_snapshot(path: Path) -> Config:
    """Existing scored batches can still be reviewed after retiring generation labels."""
    data = json.loads(path.read_text(encoding="utf-8"))
    synthesis = data.get("synthesis", {})
    retired = {"multi_hop", "table_lookup", "source_limitation"}
    types = synthesis.get("question_types", [])
    if retired.intersection(types):
        synthesis["question_types"] = [name for name in types if name not in retired] or list(
            QUESTION_TYPES
        )
        synthesis["type_weights"] = {
            name: weight
            for name, weight in synthesis.get("type_weights", {}).items()
            if name not in retired
        }
    return apply_model_environment(Config.model_validate(data))


def main(argv=None) -> int:
    load_environment()
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    parser = argparse.ArgumentParser(prog="doc2qa", description="DeepEval 文档 QA 自动合成工程")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "generate", "validate", "dedup"):
        p = sub.add_parser(command)
        p.add_argument(
            "--config", type=Path, help="YAML config; paths are relative to the working directory"
        )
        p.add_argument("--input", type=Path)
        p.add_argument("--output", type=Path)
        if command in {"prepare", "generate"}:
            p.add_argument("--cross-document", type=boolean)
            p.add_argument("--context-strategy", choices=["whole_document", "chunks"])
        if command == "generate":
            p.add_argument("--mode", choices=["docs", "contexts"])
            p.add_argument(
                "--smoke",
                action="store_true",
                help="Offline extractive fixtures; never auto-passed",
            )
            p.add_argument("--limit-contexts", type=int)
            p.add_argument("--resume", type=Path)
        if command in {"validate", "dedup"}:
            p.add_argument(
                "--run-dir",
                type=Path,
                required=command == "dedup",
                help="Directory containing document/chunk/context snapshots",
            )
        if command == "dedup":
            p.add_argument(
                "--exact-only",
                action="store_true",
                help="Only exact deduplication; no remote calls",
            )
        if command == "validate":
            p.add_argument(
                "--offline", action="store_true", help="Deterministic validation only; no auto-pass"
            )
    args = parser.parse_args(argv)
    configure_logging()
    try:
        if args.command == "generate" and args.resume and not args.config:
            saved = json.loads((args.resume / "manifest.json").read_text(encoding="utf-8"))
            config = Config.model_validate(saved["config"])
            args.smoke = saved["smoke"]
            if args.limit_contexts is None:
                args.limit_contexts = saved["limit_contexts"]
        else:
            config = load_config(args.config)
        if args.command in {"prepare", "generate"} and args.input:
            config.input = str(args.input)
        if args.output and args.command in {"prepare", "generate"}:
            config.output = str(args.output)
        if getattr(args, "cross_document", None) is not None:
            config.context.cross_document = args.cross_document
        if getattr(args, "context_strategy", None):
            config.context.strategy = args.context_strategy
        if args.command == "prepare":
            directory = args.output or Path("data/prepared")
            needs_embedding = (
                config.context.strategy == "chunks" and config.context.embedding == "openai"
            )
            require_models(config, embedding=needs_embedding)
            remote = Remote(config.synthesis) if needs_embedding else None
            *_, summary = prepare(config, directory, remote)
            print(
                json.dumps(
                    {"directory": str(directory.resolve()), **summary}, ensure_ascii=False, indent=2
                )
            )
        elif args.command == "generate":
            if args.mode:
                config.mode = args.mode
            if args.limit_contexts is not None and args.limit_contexts < 1:
                raise ValueError("--limit-contexts must be positive")
            directory, manifest = generate(config, args.smoke, args.limit_contexts, args.resume)
            print(
                json.dumps(
                    {
                        "directory": str(directory.resolve()),
                        "status": manifest["status"],
                        "smoke": manifest["smoke"],
                        **manifest["summary"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2 if manifest["status"] != "completed" else 0
        elif args.command == "dedup":
            if not args.config:
                config = _review_snapshot(args.run_dir / "config.json")
            output = args.output or args.run_dir / "deduplicated"
            summary = rededuplicate(
                args.input, output, args.run_dir, config, exact_only=args.exact_only
            )
            print(
                json.dumps(
                    {"directory": str(output.resolve()), **summary}, ensure_ascii=False, indent=2
                )
            )
        else:
            if not args.input:
                raise ValueError("validate requires --input candidates.jsonl")
            bundle = args.run_dir or args.input.parent
            if not args.config and (bundle / "config.json").exists():
                config = _review_snapshot(bundle / "config.json")
            output = args.output or args.input.parent / "validated"
            requested = output if output.suffix == ".jsonl" else None
            output_dir = output.parent / (output.stem + "_artifacts") if requested else output
            summary = revalidate(args.input, output_dir, bundle, config, args.offline)
            if requested:
                from .storage import atomic_text

                atomic_text(requested, (output_dir / "dataset.jsonl").read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {"directory": str(output_dir.resolve()), **summary},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    except KeyboardInterrupt:
        logging.info("Interrupted; completed checkpoints are saved for resume")
        return 130
    except (ValueError, OSError, ImportError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
