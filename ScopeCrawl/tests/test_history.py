from __future__ import annotations

import json

from scopecrawl.history import HISTORY_FILENAME, UrlHistory, reset_history


def test_history_roundtrip_merges_disk_entries(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    first = UrlHistory(path)
    first.record(["example.com/a", "example.com/b"])
    first.save()

    assert json.loads(path.read_text(encoding="utf-8")) == [
        "example.com/a",
        "example.com/b",
    ]

    second = UrlHistory(path)
    assert second.contains("example.com/a")
    assert second.contains("example.com/b")

    # 另一个进程在读取后写入新条目，保存时应合并而不是覆盖。
    second.record(["example.com/c"])
    second.save()
    assert UrlHistory(path).contains("example.com/a")
    assert UrlHistory(path).contains("example.com/c")


def test_reset_history_removes_file(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    path.write_text("{}", encoding="utf-8")
    assert reset_history(path) is True
    assert not path.exists()
    assert reset_history(path) is False


def test_corrupt_history_is_treated_as_empty(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    path.write_text("not json", encoding="utf-8")
    assert not UrlHistory(path).contains("example.com/a")
