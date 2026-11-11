from __future__ import annotations

import json

import pytest

from scopecrawl.keywords import KeywordError, generate_keywords, parse_keywords
from scopecrawl.models import KeywordsSettings, RunInput
from tests.conftest import FakeLLM

RUN = RunInput(
    s3="天气查询",
    s4="气象灾害查询",
    location="埃及",
    language="zh",
)


def test_parse_plain_array():
    assert parse_keywords('["a", "b"]') == ["a", "b"]


def test_parse_fenced_array_with_noise():
    text = 'Here you go:\n```json\n["埃及 沙尘暴", "埃及 高温"]\n```\nDone.'
    assert parse_keywords(text) == ["埃及 沙尘暴", "埃及 高温"]


def test_parse_object_with_queries_key():
    assert parse_keywords('{"queries": ["a", "b"]}') == ["a", "b"]
    assert parse_keywords('prefix {"keywords": ["a"]} suffix') == ["a"]


def test_parse_skips_non_string_items():
    assert parse_keywords('["a", 1, null, "b"]') == ["a", "b"]


def test_parse_invalid_returns_none():
    assert parse_keywords("not json at all") is None
    assert parse_keywords("[]") is None


def test_generate_keywords_renders_prompt(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text(
        "S3={{s3}}|S4={{s4}}|LOC={{location}}|LANG={{language_name}}|MAX={{max_queries}}",
        encoding="utf-8",
    )
    client = FakeLLM(['["关键词一", "关键词二"]'])
    result = generate_keywords(
        RUN,
        client,
        KeywordsSettings(max_queries=5),
        prompt_path=prompt,
    )
    assert result == ["关键词一", "关键词二"]
    content = client.calls[0][0][0]["content"]
    assert "S3=天气查询" in content
    assert "LOC=埃及" in content
    assert "LANG=Simplified Chinese" in content
    assert "MAX=5" in content


def test_generate_keywords_uses_none_for_missing_location(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("LOC={{location}}", encoding="utf-8")
    run = RUN.model_copy(update={"location": None})
    client = FakeLLM(['["a"]'])
    generate_keywords(run, client, KeywordsSettings(), prompt_path=prompt)
    assert client.calls[0][0][0]["content"] == "LOC=none"


def test_generate_keywords_injects_coverage_points(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("COVER:\n{{coverage_points}}", encoding="utf-8")
    client = FakeLLM(['["a"]'])
    generate_keywords(
        RUN,
        client,
        KeywordsSettings(),
        prompt_path=prompt,
        coverage_points=["实时菜价", "实时汇率"],
    )
    assert client.calls[0][0][0]["content"] == "COVER:\n  - 实时菜价\n  - 实时汇率"


def test_generate_keywords_uses_none_for_missing_coverage_points(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("COVER:\n{{coverage_points}}", encoding="utf-8")
    client = FakeLLM(['["a"]'])
    generate_keywords(RUN, client, KeywordsSettings(), prompt_path=prompt)
    assert client.calls[0][0][0]["content"] == "COVER:\n  none"


def test_generate_keywords_retries_once(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    client = FakeLLM(["not json", '["a"]'])
    assert generate_keywords(RUN, client, KeywordsSettings(), prompt_path=prompt) == ["a"]
    assert len(client.calls) == 2
    assert client.calls[1][0][-1]["role"] == "user"


def test_generate_keywords_raises_after_two_failures(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    client = FakeLLM(["bad", "still bad"])
    with pytest.raises(KeywordError, match="解析"):
        generate_keywords(RUN, client, KeywordsSettings(), prompt_path=prompt)


def test_generate_keywords_cleans_dedupes_and_truncates(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    raw = json.dumps(
        ["埃及  沙尘暴", "埃及沙尘暴。", "埃及 高温", "", "   ", "埃及 暴雨"],
        ensure_ascii=False,
    )
    client = FakeLLM([raw])
    result = generate_keywords(RUN, client, KeywordsSettings(max_queries=2), prompt_path=prompt)
    assert result == ["埃及 沙尘暴", "埃及 高温"]


def test_generate_keywords_accepts_object_with_scene(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    payload = json.dumps(
        {
            "scene": {
                "question_types": ["recommendation"],
                "needed_doc_forms": ["listicle"],
                "location_relevant": False,
                "time_sensitive": False,
            },
            "queries": ["母亲节送妈妈什么礼物", "朋友生日礼物 100元以内"],
        },
        ensure_ascii=False,
    )
    client = FakeLLM([payload])

    result = generate_keywords(RUN, client, KeywordsSettings(), prompt_path=prompt)

    assert result == ["母亲节送妈妈什么礼物", "朋友生日礼物 100元以内"]
    assert len(client.calls) == 1


def test_generate_keywords_allows_real_s4_phrase(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    run = RUN.model_copy(
        update={"s3": "电商与消费", "s4": "礼品推荐", "location": "中国"}
    )
    client = FakeLLM(
        [json.dumps(["母亲节送妈妈什么礼物", "朋友生日礼物 100元以内"], ensure_ascii=False)]
    )

    result = generate_keywords(run, client, KeywordsSettings(), prompt_path=prompt)

    assert result == ["母亲节送妈妈什么礼物", "朋友生日礼物 100元以内"]
    assert len(client.calls) == 1


def test_generate_keywords_flags_identical_s3_s4_label(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    run = RUN.model_copy(
        update={"s3": "日常聊天", "s4": "日常聊天", "location": "中国"}
    )
    client = FakeLLM(
        [
            json.dumps(["日常聊天"], ensure_ascii=False),
            json.dumps(["时政 新闻"], ensure_ascii=False),
        ]
    )

    result = generate_keywords(run, client, KeywordsSettings(), prompt_path=prompt)

    assert result == ["时政 新闻"]
    assert len(client.calls) == 2


def test_generate_keywords_empty_array_raises(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    client = FakeLLM(["[]", "[]"])
    with pytest.raises(KeywordError):
        generate_keywords(RUN, client, KeywordsSettings(), prompt_path=prompt)


def test_generate_keywords_regenerates_copied_catalog_labels(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    run = RUN.model_copy(
        update={"s3": "日常生活服务", "s4": "地点信息查询", "location": "中国"}
    )
    bad = json.dumps(
        ["中国 日常生活服务 地点信息查询 导航 怎么用"], ensure_ascii=False
    )
    good = json.dumps(["中国 成都 位置", "中国 重庆 交通状况"], ensure_ascii=False)
    client = FakeLLM([bad, good])

    result = generate_keywords(
        run,
        client,
        KeywordsSettings(max_queries=3),
        prompt_path=prompt,
    )

    assert result == ["中国 成都 位置", "中国 重庆 交通状况"]
    assert len(client.calls) == 2
    assert client.calls[1][0][-1]["role"] == "user"


def test_generate_keywords_keeps_result_when_regeneration_still_bad(tmp_path):
    prompt = tmp_path / "keywords.md"
    prompt.write_text("generate", encoding="utf-8")
    run = RUN.model_copy(
        update={"s3": "日常生活服务", "s4": "地点信息查询", "location": "中国"}
    )
    bad = json.dumps(
        ["中国 日常生活服务 地点信息查询 导航 怎么用"], ensure_ascii=False
    )
    client = FakeLLM([bad, bad])

    result = generate_keywords(run, client, KeywordsSettings(), prompt_path=prompt)
    assert result == ["中国 日常生活服务 地点信息查询 导航 怎么用"]


def test_generate_keywords_missing_prompt_raises(tmp_path):
    with pytest.raises(KeywordError, match="prompt"):
        generate_keywords(
            RUN,
            FakeLLM(['["a"]']),
            KeywordsSettings(),
            prompt_path=tmp_path / "missing.md",
        )
