from __future__ import annotations

import json

from scopecrawl.scenarios import find_coverage_points, load_scenarios

TREE = {
    "智能对话": {
        "联网搜索": {
            "日常生活服务": [
                {"s4": "地点信息查询", "coverage_points": ["消费水平", "安全性"]},
                {"s4": "市场物价查询", "coverage_points": []},
            ]
        }
    }
}


def test_find_coverage_points_hits_nested_entry():
    assert find_coverage_points(TREE, "日常生活服务", "地点信息查询") == ["消费水平", "安全性"]


def test_find_coverage_points_returns_empty_list_for_entry_without_points():
    assert find_coverage_points(TREE, "日常生活服务", "市场物价查询") == []


def test_find_coverage_points_returns_none_when_not_listed():
    assert find_coverage_points(TREE, "天气查询", "气象灾害查询") is None
    assert find_coverage_points(TREE, "日常生活服务", "不存在的S4") is None


def test_find_coverage_points_strips_surrounding_whitespace():
    assert find_coverage_points(TREE, " 日常生活服务 ", " 地点信息查询 ") == ["消费水平", "安全性"]


def test_load_scenarios_reads_tree(tmp_path):
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(TREE, ensure_ascii=False), encoding="utf-8")
    assert load_scenarios(path) == TREE


def test_load_scenarios_missing_file_returns_empty_tree(tmp_path):
    assert load_scenarios(tmp_path / "missing.json") == {}


def test_load_scenarios_invalid_json_returns_empty_tree(tmp_path):
    path = tmp_path / "scenarios.json"
    path.write_text("not json", encoding="utf-8")
    assert load_scenarios(path) == {}


def test_load_scenarios_non_object_returns_empty_tree(tmp_path):
    path = tmp_path / "scenarios.json"
    path.write_text("[]", encoding="utf-8")
    assert load_scenarios(path) == {}
