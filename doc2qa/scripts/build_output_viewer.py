#!/usr/bin/env python3
"""Build a portable, offline HTML report from one doc2qa output directory."""

from __future__ import annotations

import argparse
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "manifest.json",
    "validation_manifest.json",
    "deduplication_manifest.json",
    "summary.json",
    "config.json",
    "preparation.json",
    "progress.json",
    "preview.json",
    "candidates.jsonl",
    "scored_candidates.jsonl",
    "dataset.jsonl",
    "dataset.json",
    "review_queue.jsonl",
    "rejected.jsonl",
    "duplicates.jsonl",
    "generated_qa.jsonl",
    "documents.jsonl",
    "chunks.jsonl",
    "contexts.jsonl",
    "events.jsonl",
    "run.log",
}
MARKERS = {
    "manifest.json",
    "validation_manifest.json",
    "deduplication_manifest.json",
    "summary.json",
    "preview.json",
    "candidates.jsonl",
    "scored_candidates.jsonl",
    "config.json",
}


def supported(path: Path) -> bool:
    parts = path.parts
    if len(parts) == 1:
        return path.name in ROOT_FILES
    return (
        parts[0] == "intermediate"
        and path.name in {"initial_questions.jsonl", "generated_qa.jsonl"}
    ) or (parts[0] == "checkpoints" and path.suffix == ".jsonl")


def is_run(path: Path) -> bool:
    return path.is_dir() and any((path / name).is_file() for name in MARKERS)


def run_timestamp(path: Path) -> float:
    names = ("manifest.json", "progress.json", "summary.json", "candidates.jsonl", "preview.json")
    return max(
        ((path / name).stat().st_mtime for name in names if (path / name).is_file()), default=0
    )


def select_run(path: Path) -> Path:
    if is_run(path):
        return path
    if not path.is_dir():
        raise ValueError(f"Output directory does not exist: {path}")
    runs = [p for p in path.iterdir() if is_run(p)]
    if not runs:
        raise ValueError(f"No doc2qa output directories found in {path}")
    return max(runs, key=run_timestamp)


def current_prompt_version() -> str:
    tree = ast.parse((PROJECT / "src/doc2qa/prompts.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PROMPT_VERSION"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    return "unknown"


def snapshot(directory: Path) -> dict:
    files, warnings = [], []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if not supported(relative) or not path.is_file() or path.is_symlink():
            continue
        if not path.resolve().is_relative_to(directory.resolve()):
            continue
        try:
            files.append(
                {
                    "path": relative.as_posix(),
                    "text": path.read_text(encoding="utf-8-sig"),
                    "modified": path.stat().st_mtime * 1000,
                }
            )
        except (OSError, UnicodeError) as exc:
            warnings.append(f"Cannot read {relative}: {exc}")
    return {
        "name": directory.name,
        "source": str(directory.resolve()),
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "currentPrompt": current_prompt_version(),
        "warnings": warnings,
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT / "data/output",
        help="A run directory, or its parent (selects the most recently updated run)",
    )
    parser.add_argument("--html", type=Path, default=PROJECT / "output_viewer.html")
    args = parser.parse_args()
    try:
        directory = select_run(args.output.resolve())
        data = json.dumps(snapshot(directory), ensure_ascii=False, separators=(",", ":"))
        # Prevent source text from closing the inert JSON script element.
        data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        template = (PROJECT / "tools/output_viewer.html").read_text(encoding="utf-8")
        marker = "__DOC2QA_SNAPSHOT__"
        if template.count(marker) != 1:
            raise ValueError("HTML template must contain exactly one snapshot marker")
        destination = args.html.resolve()
        if (
            destination.is_relative_to(directory)
            or destination == PROJECT / "tools/output_viewer.html"
        ):
            raise ValueError(
                "Write the viewer outside the source output directory and HTML template"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(template.replace(marker, data), encoding="utf-8")
        print(f"HTML: {destination}\nOutput: {directory}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
