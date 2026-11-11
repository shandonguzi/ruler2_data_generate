"""Convert atomic intent seed cases to the CaseSmith QA CSV format."""

import argparse
import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "input/seed_case/atomic_s4_case_transport_20261110_zh.csv"
QA_COLUMNS = ("id", "dialect", "open_question", "open_answer", "language")


def convert(source: Path, target: Path, dialect: str, language: str = "") -> int:
    if source.resolve() == target.resolve():
        raise ValueError("Input and output must be different files")

    cases = []
    seen_ids = set()
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = {"intent_id", "text"} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Missing input columns: {', '.join(sorted(missing))}")
        for row in reader:
            intent_id = (row["intent_id"] or "").strip()
            question = (row["text"] or "").strip()
            if not intent_id or not question:
                raise ValueError(f"Empty intent_id or text at line {reader.line_num}")
            if intent_id in seen_ids:
                raise ValueError(f"Duplicate intent_id at line {reader.line_num}: {intent_id}")
            seen_ids.add(intent_id)
            cases.append((intent_id, dialect, question, row.get("open_answer") or "", language))

    if not cases:
        raise ValueError("Input CSV contains no seed cases")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(QA_COLUMNS)
        writer.writerows(cases)
    return len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, help="Defaults to CaseSmith/output/<input_stem>_qa.csv")
    parser.add_argument("--dialect", default="zh")
    parser.add_argument("--language", help="Output language, defaults to the dialect label")
    args = parser.parse_args()
    target = args.output or ROOT / "output" / f"{args.input.stem}_qa.csv"
    try:
        count = convert(args.input, target, args.dialect, args.language or args.dialect)
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))
    print(f"Converted {count} seed cases to {target}")


if __name__ == "__main__":
    main()
