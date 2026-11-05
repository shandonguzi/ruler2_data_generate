#!/usr/bin/env python3
"""Validate the CaseSmith turn-level CSV export.

Usage:
    python scripts/verify_export.py output/QA_case_improved.csv \
        --state output/state/query_assignments.json \
        --interactions-dir output/interactions
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.csv_exporter import OUTPUT_COLUMNS, QUERY_LEVEL_COLUMNS  # noqa: E402
from src.query_generator import VALID_DOMAINS, VALID_SCENARIOS  # noqa: E402


def load_csv(path: Path):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def load_json(path: Path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def conversation(interaction):
    messages = interaction.get('messages')
    if isinstance(messages, list):
        return messages
    return interaction.get('full_conversation') or []


def expected_rows(interaction):
    """Recreate (turn, improved, noisy, answer) tuples from an interaction."""
    answers = {}
    for msg in conversation(interaction):
        if str(msg.get('role', '')).lower() == 'assistant':
            turn = (msg.get('metadata') or {}).get('turn')
            if isinstance(turn, int):
                answers.setdefault(turn, msg.get('content', ''))

    rows = []
    for msg in conversation(interaction):
        if str(msg.get('role', '')).lower() != 'user':
            continue
        meta = msg.get('metadata') or {}
        turn = meta.get('turn')
        if not isinstance(turn, int) or turn not in answers:
            continue
        rows.append({
            'turn': turn + 1,
            'improved_query': meta.get('clean_version', msg.get('content', '')),
            'noisy_query': msg.get('content', ''),
            'answer': answers[turn],
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path', type=Path)
    parser.add_argument('--state', type=Path, default=None)
    parser.add_argument('--interactions-dir', type=Path, default=None)
    parser.add_argument('--max-report', type=int, default=20)
    args = parser.parse_args()

    errors = []
    warnings = []
    header, rows = load_csv(args.csv_path)

    # 1. Header
    if header != OUTPUT_COLUMNS:
        errors.append(f"Header mismatch:\n  expected: {OUTPUT_COLUMNS}\n  actual:   {header}")

    # 2/3/4/7. Row-level checks. A new query starts on the row that carries
    # query_id; subsequent rows only fill the per-turn columns.
    groups = defaultdict(list)
    order = []
    current = None
    for index, row in enumerate(rows, start=2):
        query_id = (row.get('query_id') or '').strip()
        if query_id:
            if query_id in groups:
                errors.append(f"line {index}: duplicate query_id {query_id!r}")
            else:
                order.append(query_id)
            current = query_id
        elif current is None:
            errors.append(f"line {index}: row before any query_id")
            continue
        groups[current].append((index, row))

    for query_id in order:
        group = groups[query_id]
        for position, (index, row) in enumerate(group):
            for col in ('improved_query', 'noisy_query', 'answer'):
                if not row.get(col, '').strip():
                    errors.append(f"line {index}: empty {col}")
            if position == 0:
                if row.get('id') != query_id:
                    errors.append(
                        f"line {index}: query_id {query_id!r} != id {row.get('id')!r}"
                    )
                if not row.get('open_question', '').strip():
                    errors.append(f"line {index}: empty open_question on first turn")
            else:
                for col in QUERY_LEVEL_COLUMNS:
                    if (row.get(col) or '').strip():
                        errors.append(
                            f"line {index}: {col} must be empty after the first turn"
                        )
            if row.get('domain') and row['domain'] not in VALID_DOMAINS:
                warnings.append(f"line {index}: unknown domain {row['domain']!r}")
            if row.get('scenario') and row['scenario'] not in VALID_SCENARIOS:
                warnings.append(f"line {index}: unknown scenario {row['scenario']!r}")

        turns = []
        for _, row in group:
            try:
                turns.append(int(row.get('turn', '')))
            except ValueError:
                errors.append(f"query {query_id}: non-integer turn {row.get('turn')!r}")
        if turns != list(range(1, len(turns) + 1)):
            errors.append(f"query {query_id}: turns are not 1..n consecutive: {turns}")

    # 5. Cross-check with interaction JSON
    checked = 0
    if args.state and args.interactions_dir:
        state = load_json(args.state)
        by_query = {}
        for assignment in state.get('assignments', []):
            if assignment.get('interaction_id'):
                row_id = (assignment.get('row') or {}).get('id')
                by_query[row_id or assignment['query_id']] = assignment

        for query_id, group in groups.items():
            assignment = by_query.get(query_id)
            if not assignment:
                warnings.append(f"query {query_id}: not found in state")
                continue
            path = args.interactions_dir / f"{assignment['interaction_id']}.json"
            if not path.exists():
                warnings.append(f"query {query_id}: interaction file missing ({path})")
                continue
            expected = expected_rows(load_json(path))
            actual = [{k: row[k] for k in ('turn', 'improved_query', 'noisy_query', 'answer')} for _, row in group]
            actual_turns = [{**item, 'turn': int(item['turn'])} for item in actual]
            if actual_turns != expected:
                errors.append(f"query {query_id}: CSV rows do not match interaction JSON")
            checked += 1

    print(f"CSV: {args.csv_path}")
    print(f"  rows: {len(rows)}")
    print(f"  queries: {len(groups)}")
    if args.state and args.interactions_dir:
        print(f"  cross-checked with interaction JSON: {checked}")

    for warning in warnings[:args.max_report]:
        print(f"  [WARN] {warning}")
    if len(warnings) > args.max_report:
        print(f"  ... {len(warnings) - args.max_report} more warnings")

    for error in errors[:args.max_report]:
        print(f"  [ERROR] {error}")
    if len(errors) > args.max_report:
        print(f"  ... {len(errors) - args.max_report} more errors")

    if errors:
        print(f"\nFAILED with {len(errors)} error(s)")
        return 1

    print("\nOK")
    return 0


if __name__ == '__main__':
    sys.exit(main())
