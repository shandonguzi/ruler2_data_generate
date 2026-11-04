import io
import logging
import re

import pytest
from rich.console import Console

from doc2qa.console import ProgressConsoleHandler, configure_logging
from doc2qa.progress import RunProgress, context_scope
from doc2qa.storage import read_jsonl


def screen(text):
    """Interpret the cursor/erase commands used by Rich's live line display."""
    rows, row, column = [[]], 0, 0
    for token in re.findall(r"\x1b\[[0-?]*[ -/]*[@-~]|[^\x1b]", text):
        if token.startswith("\x1b["):
            if token.endswith("A"):
                row = max(0, row - int(token[2:-1] or 1))
            elif token == "\x1b[2K":
                rows[row] = []
        elif token == "\r":
            column = 0
        elif token == "\n":
            row += 1
            column = 0
            if row == len(rows):
                rows.append([])
        else:
            while len(rows[row]) <= column:
                rows[row].append(" ")
            rows[row][column] = token
            column += 1
    return "\n".join("".join(line).rstrip() for line in rows).rstrip()


@pytest.fixture
def terminal():
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=160)
    handler = ProgressConsoleHandler(console)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    yield handler, output
    handler.close()


def emit(handler, message, operation_id=None, status=None, **details):
    record = logging.LogRecord("doc2qa.test", logging.INFO, "", 1, message, (), None)
    record.progress_event = dict(operation_id=operation_id, status=status, **details)
    handler.handle(record)


@pytest.mark.parametrize(
    ("status", "marker"),
    [
        ("completed", "✅"),
        ("failed", "❌"),
        ("interrupted", "❌"),
    ],
)
def test_operation_start_is_visible_and_end_updates_the_same_line(terminal, status, marker):
    handler, output = terminal
    emit(handler, "[ctx_test] 最终质量审核 70/75：开始", "a", "started")
    assert screen(output.getvalue()) == "INFO [ctx_test] 最终质量审核 70/75：开始"
    emit(handler, "Unused completion line", "a", status, elapsed_seconds=0.1)
    assert screen(output.getvalue()) == f"INFO [ctx_test] 最终质量审核 70/75：开始 {marker} (0.1s)"
    assert handler.active == {}
    assert handler.live is None


def test_concurrent_operations_and_intervening_logs_keep_one_line_per_operation(terminal):
    handler, output = terminal
    emit(handler, "Operation A：开始", "a", "started")
    emit(handler, "Operation B：开始", "b", "started")
    emit(handler, "Important warning")
    emit(handler, "B completed", "b", "completed", elapsed_seconds=0.2)
    assert handler.active == {"a": "INFO Operation A：开始"}
    emit(
        handler,
        "A failed",
        "a",
        "failed",
        elapsed_seconds=0.3,
        error_type="TimeoutError",
        error_message="Timed out",
    )
    visible = screen(output.getvalue())
    assert visible.count("Operation A") == 1
    assert visible.count("Operation B") == 1
    assert visible.count("Important warning") == 1
    assert "Operation A：开始 ❌ (0.3s) TimeoutError: Timed out" in visible
    assert "Operation B：开始 ✅ (0.2s)" in visible


def test_persistent_logs_keep_both_events_and_full_failure(terminal, tmp_path):
    handler, output = terminal
    logger = logging.getLogger("doc2qa")
    logger.addHandler(handler)
    try:
        with RunProgress(tmp_path) as progress, context_scope("ctx_test"):
            with progress.operation("final_validation", "审核 1/2"):
                pass
            with pytest.raises(RuntimeError, match="Service unavailable"):
                with progress.operation("final_validation", "审核 2/2"):
                    raise RuntimeError("Service unavailable")
    finally:
        logger.removeHandler(handler)
    visible = screen(output.getvalue())
    assert visible.count("审核 1/2") == 1
    assert visible.count("审核 2/2") == 1
    assert "✅" in visible and "❌" in visible
    persisted = (tmp_path / "run.log").read_text()
    assert "审核 1/2：开始" in persisted
    assert "审核 1/2：完成" in persisted
    assert "RuntimeError" in persisted
    assert "Service unavailable" in persisted
    assert any(
        e.get("error_message") == "Service unavailable"
        for e in read_jsonl(tmp_path / "events.jsonl")
    )
    assert "\x1b" not in persisted


def test_redirected_output_has_no_cursor_control_sequences():
    output = io.StringIO()
    handler = ProgressConsoleHandler(Console(file=output, force_terminal=False))
    emit(handler, "Operation：开始", "a", "started")
    emit(handler, "Operation：完成", "a", "completed", elapsed_seconds=0.1)
    handler.close()
    assert "\x1b" not in output.getvalue()
    assert "✅" in output.getvalue()


def test_http_request_info_logs_are_suppressed_but_warnings_remain(monkeypatch, caplog):
    names = ("httpx", "httpcore", "httpx2", "httpcore2")
    for name in names:
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    configure_logging()
    with caplog.at_level(logging.INFO):
        for name in names:
            logging.getLogger(name).info("HTTP Request: POST example 200 OK")
            logging.getLogger(name).warning("Transport warning from %s", name)
    assert "HTTP Request:" not in caplog.text
    assert sum("Transport warning" in record.message for record in caplog.records) == 4
