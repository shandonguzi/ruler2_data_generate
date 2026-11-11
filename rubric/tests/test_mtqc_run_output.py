import json
from datetime import datetime
from pathlib import Path

import pytest

from rubric.mtqc import cli, run_output
from rubric.mtqc.llm_client import LLMClient
from conftest import FakeAPI


def test_cli_runs_are_isolated_and_can_be_resumed_or_exported(settings, tmp_path, monkeypatch):
    config = settings()
    base = Path(config["output"]["results_jsonl"]).parent
    api = FakeAPI(config)
    monkeypatch.setattr(cli, "LLMClient", lambda config, records, transport=None: LLMClient(config, records, api))
    command = ["--config", str(tmp_path / "config.yaml")]

    assert cli.main(["preview", *command]) == 0
    assert not base.exists()
    assert cli.main(["run", *command]) == 0
    first = next(path for path in base.iterdir() if run_output.RUN_NAME.fullmatch(path.name))
    original_files = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    assert cli.main(["run", *command]) == 0
    directories = sorted(path for path in base.iterdir() if run_output.RUN_NAME.fullmatch(path.name))
    assert len(directories) == 2
    second = directories[-1]
    assert second != first and len(api.calls) == 8
    for directory in directories:
        assert {"results.jsonl", "results.xlsx", "report.html", "records"} <= {path.name for path in directory.iterdir()}
    assert original_files == {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    manifests = [json.loads((directory / "records" / "manifest.json").read_text()) for directory in directories]
    assert manifests[0]["run_id"] != manifests[1]["run_id"]

    # Latest-run export is offline; explicit run-dir reuses the existing request budget and results.
    assert cli.main(["export", *command]) == 0
    assert cli.main(["export", *command, "--run-dir", str(first)]) == 0
    assert cli.main(["run", *command, "--run-dir", str(first)]) == 0
    assert len(api.calls) == 8
    assert len([path for path in base.iterdir() if run_output.RUN_NAME.fullmatch(path.name)]) == 2


def test_timestamp_collision_creates_distinct_directories(settings, monkeypatch):
    class FixedClock:
        @staticmethod
        def now():
            return datetime(2026, 11, 10, 20, 30, 40, 123456)

    monkeypatch.setattr(run_output, "datetime", FixedClock)
    config = settings()
    first = run_output.new_run_config(config)
    second = run_output.new_run_config(config)
    assert Path(first["output"]["results_jsonl"]).parent.name == "20261110_203040_123456"
    assert Path(second["output"]["results_jsonl"]).parent.name == "20261110_203040_123456_1"
    assert Path(config["output"]["results_jsonl"]).parent == Path(first["output"]["results_jsonl"]).parent.parent


def test_latest_export_ignores_empty_directories_and_orders_collision_suffixes(settings):
    config = settings()
    base = Path(config["output"]["results_jsonl"]).parent
    for name in ("20261110_203040_123456_2", "20261110_203040_123456_10"):
        records = base / name / "records"
        records.mkdir(parents=True)
        (records / "manifest.json").write_text("{}")
    (base / "20261111_203040_123456").mkdir()
    selected = run_output.existing_run_config(config)
    assert Path(selected["output"]["records_dir"]).parent.name == "20261110_203040_123456_10"


def test_missing_run_directory_never_creates_records(settings, tmp_path):
    config = settings()
    with pytest.raises(ValueError, match="No.*run records"):
        run_output.existing_run_config(config)
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="No run records"):
        run_output.existing_run_config(config, missing)
    assert not missing.exists()


@pytest.mark.parametrize("absolute", [False, True])
def test_single_output_directory_resolves_relative_to_config(settings, tmp_path, absolute):
    root = tmp_path / "custom" / "nested"
    config = settings(output=str(root) if absolute else "custom/nested")
    assert config["output"] == {
        "results_jsonl": str(root / "results.jsonl"),
        "excel_path": str(root / "results.xlsx"),
        "report_path": str(root / "report.html"),
        "records_dir": str(root / "records"),
    }
    assert not root.exists()
