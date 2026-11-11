"""Isolate CLI runs while keeping resume and export tied to explicit records."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path


RUN_NAME = re.compile(r"(\d{8}_\d{6}_\d{6})(?:_(\d+))?")


def _in_directory(config: dict, directory: Path) -> dict:
    result = deepcopy(config)
    output = {key: str(directory / Path(value).name) for key, value in config["output"].items()}
    if len(set(output.values())) != len(output):
        raise ValueError("Output file and records names must be distinct within the run directory")
    result["output"] = output
    return result


def new_run_config(config: dict) -> dict:
    """Reserve a fresh timestamp directory without overwriting an earlier run."""
    base = Path(config["output"]["results_jsonl"]).parent
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = 0
    while True:
        directory = base / (timestamp if not suffix else f"{timestamp}_{suffix}")
        result = _in_directory(config, directory)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        return result


def existing_run_config(config: dict, run_dir=None) -> dict:
    """Use a selected run, or the latest timestamped run for export."""
    records_name = Path(config["output"]["records_dir"]).name
    if run_dir is not None:
        directory = Path(run_dir).expanduser().resolve()
        if not (directory / records_name / "manifest.json").is_file():
            raise ValueError(f"No run records found in {directory}")
        return _in_directory(config, directory)

    base = Path(config["output"]["results_jsonl"]).parent
    candidates = []
    if base.is_dir():
        for directory in base.iterdir():
            match = RUN_NAME.fullmatch(directory.name)
            if match and (directory / records_name / "manifest.json").is_file():
                candidates.append(((match[1], int(match[2] or 0)), directory))
    if candidates:
        return _in_directory(config, max(candidates)[1])
    # Keep runs created before timestamped output available for export.
    if (Path(config["output"]["records_dir"]) / "manifest.json").is_file():
        return deepcopy(config)
    raise ValueError(f"No completed or interrupted run records found under {base}")
