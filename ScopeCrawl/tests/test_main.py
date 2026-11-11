from __future__ import annotations

import pytest

from main import main
from scopecrawl.config import load_settings
from scopecrawl.history import HISTORY_FILENAME
from tests.conftest import make_settings


def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_invalid_language_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        main(["--language", "fr"])
    assert excinfo.value.code == 2


def test_missing_llm_model_returns_one(capsys, monkeypatch):
    # 使用空的 .env 内容，避免依赖本机是否存在真实 .env（否则会发起真实调用）。
    monkeypatch.setattr("main.load_settings", lambda **kwargs: load_settings(env={}))
    assert main([]) == 1
    assert "配置错误" in capsys.readouterr().err


def test_restart_clears_history_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("main.DEFAULT_HISTORY_PATH", tmp_path / HISTORY_FILENAME)
    monkeypatch.setattr("main.load_settings", lambda **kwargs: make_settings())
    monkeypatch.setattr("main.run_pipeline", lambda settings: 0)
    (tmp_path / HISTORY_FILENAME).write_text('["example.com/a"]')

    assert main(["--restart"]) == 0
    assert not (tmp_path / HISTORY_FILENAME).exists()
    assert "已清空 URL 历史表" in capsys.readouterr().out
