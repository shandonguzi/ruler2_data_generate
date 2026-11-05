"""第三任务：审核表 -> LLM 五维盲评 -> 一致性 HTML 报告。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import secrets
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from openpyxl import load_workbook

from critic import FROZEN_WEIGHTS, THRESHOLD, score_and_decide
from report import render_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "voice_ruler2_test" / "第三任务_40条五维预评分.xlsx"
DEFAULT_OUTPUT = ROOT / "src" / "output"
DEFAULT_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")
RUBRIC_VERSION = "v0.1"
DIMENSIONS = (
    ("language", "语言自然", "language_score", "user_language_score"),
    ("clarity", "表述清晰", "clarity_score", "user_clarity_score"),
    ("logic", "内部逻辑", "logic_score", "user_logic_score"),
    ("fidelity", "原意忠实", "fidelity_score", "user_fidelity_score"),
    ("answer", "原答案匹配", "answer_score", "user_answer_score"),
)
DECISIONS = ("拒绝", "人工复核", "通过")
SHANGHAI = timezone(timedelta(hours=8))
CONFIG_FIELDS = {"api_url", "api_key", "model", "input", "out_dir"}

SYSTEM_PROMPT = """你是独立的问句改写评审员。只看原问题 Q0、原答案 A0、候选问题 Q1，分别给五维 1/2/3 分。不要查外部知识，不要评价文化事实真伪。原答案匹配只判断 A0 能否回答 Q1。

标准 v0.1：
1. language（语言自然）：1 严重语法/词序问题，难以直接理解；2 小语病但可理解；3 自然正确。
2. clarity（表述清晰）：1 无法确定所问或不是问句；2 需要结合 Q0 才能确定目标/范围；3 Q1 单独读清楚。
3. logic（内部逻辑）：1 直接矛盾或基本类别错配；2 有可调和的关系疑点；3 句内及与 Q0 明示条件自洽。
4. fidelity（原意忠实）：1 改变提问目标、关键条件增删/反转，或新增必须回答的子问；2 目标未变但非关键限定/等义关系存疑；3 原目标及关键人地时数否定等条件保留。
5. answer（原答案匹配）：1 A0 完全答不了 Q1 或答案类型不符；2 只能回答一部分；3 无需补充信息即可回答全部。

五维尽量独立。严重病句若借 Q0 还能辨认目标，内容维照常评分。只有在 language 或 clarity 为 1 且目标完全无法确定时，logic/fidelity/answer 可写 null，理由说明无法判定。新增必答子问通常 fidelity=1、answer=2。
请只输出一个 JSON 对象，字段必须恰好是 language、clarity、logic、fidelity、answer；每个字段都是 {"score": 1或2或3或null, "reason": "一句简短理由"}。language 和 clarity 不允许 null。不要输出综合分、good/bad 或处理决定。"""


def score_schema() -> dict[str, Any]:
    properties = {}
    for key, _, _, _ in DIMENSIONS:
        score_type = "integer" if key in ("language", "clarity") else ["integer", "null"]
        properties[key] = {
            "type": "object",
            "properties": {
                "score": {"type": score_type, "enum": [1, 2, 3] if isinstance(score_type, str) else [1, 2, 3, None]},
                "reason": {"type": "string"},
            },
            "required": ["score", "reason"],
            "additionalProperties": False,
        }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def response_format_for_model(model: str) -> dict[str, Any]:
    # 阿里云 DeepSeek 系列使用 JSON Object；程序仍按相同五维结构逐项校验并重试。
    if model.startswith("deepseek-"):
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": "mos_five_dimensions", "strict": True, "schema": score_schema()}}


def now_text() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def load_config(path: Path) -> dict[str, str]:
    path = path.resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取配置文件：{path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"配置文件 YAML 格式错误：{path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是键值映射")
    unknown = set(raw) - CONFIG_FIELDS
    if unknown:
        raise ValueError(f"配置文件包含未知字段：{', '.join(sorted(map(str, unknown)))}")
    for key, value in raw.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"配置项 {key} 必须是字符串；密钥等值请加引号")
    return {key: (value or "").strip() for key, value in raw.items()}


def resolve_settings(args: argparse.Namespace) -> argparse.Namespace:
    config_path = Path(getattr(args, "config", DEFAULT_CONFIG)).resolve()
    config = load_config(config_path)

    def configured_path(cli_name: str, yaml_name: str, fallback: Path) -> Path:
        cli_value = getattr(args, cli_name, None)
        if cli_value is not None:
            return Path(cli_value).expanduser().resolve()
        yaml_value = config.get(yaml_name)
        if yaml_value:
            return (config_path.parent / yaml_value).expanduser().resolve()
        return fallback

    return argparse.Namespace(
        command=args.command,
        input=configured_path("input", "input", DEFAULT_INPUT),
        out_dir=configured_path("out_dir", "out_dir", DEFAULT_OUTPUT),
        model=getattr(args, "model", None) or config.get("model", ""),
        api_url=getattr(args, "api_url", None) or config.get("api_url") or DEFAULT_API_URL,
        api_key=config.get("api_key") or os.environ.get("OPENAI_API_KEY", "").strip(),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_int_score(value: Any, field: str, candidate_id: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{candidate_id}: {field} 必须是 1、2、3")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if str(value).strip() not in ("1", "2", "3"):
        raise ValueError(f"{candidate_id}: {field} 必须是 1、2、3")
    return int(value)


def composite(scores: list[int | None]) -> tuple[int, str]:
    if len(scores) != 5:
        raise ValueError("五维分数数量必须为 5")
    language, clarity, logic, fidelity, answer = scores
    if language not in (1, 2, 3) or clarity not in (1, 2, 3):
        raise ValueError("语言与清晰度必须为 1–3")
    if any(score not in (1, 2, 3, None) for score in (logic, fidelity, answer)):
        raise ValueError("内容维必须为 1–3 或无法判定")
    if language == 1 or clarity == 1:
        return 1, "拒绝"
    if any(s not in (1, 2, 3) for s in (logic, fidelity, answer)):
        raise ValueError("只有语言或清晰度为 1 时，内容维才允许无法判定")
    if 1 in (logic, fidelity, answer):
        return 2, "拒绝"
    if scores == [3, 3, 3, 3, 3]:
        return 5, "通过"
    if scores == [2, 3, 3, 3, 3]:
        return 4, "通过"
    return 3, "人工复核"


def read_review_sheet(path: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    if not path.is_file():
        raise ValueError(f"找不到审核表：{path}")
    book = load_workbook(path, read_only=True, data_only=True)
    sheet = book.worksheets[0]
    values = sheet.values
    headers = [str(value).strip() if value is not None else "" for value in next(values)]
    required = {"candidate_id", "seed_id", "open_question", "open_answer", "candidate_question", "intended_label", "user_final_label", "user_review_status"}
    required.update(item[2] for item in DIMENSIONS)
    required.update(item[3] for item in DIMENSIONS)
    missing = sorted(required - set(headers))
    if missing:
        raise ValueError(f"审核表缺少列：{', '.join(missing)}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for row_values in values:
        row = dict(zip(headers, row_values))
        candidate_id = str(row.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        if candidate_id in seen:
            raise ValueError(f"candidate_id 重复：{candidate_id}")
        seen.add(candidate_id)
        status = str(row.get("user_review_status") or "").strip()
        if status not in ("", "同意", "修改", "存疑"):
            raise ValueError(f"{candidate_id}: 审核状态无效：{status}")
        counts[status or "待审核"] += 1
        row["candidate_id"] = candidate_id
        row["user_review_status"] = status
        rows.append(row)
    book.close()
    if not rows:
        raise ValueError("审核表没有候选题")
    return rows, counts


def approved_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = [str(row["candidate_id"]) for row in rows if not row["user_review_status"]]
    if pending:
        raise ValueError(f"还有 {len(pending)} 条待审核（如 {', '.join(pending[:4])}）；请先填写 Excel 的 user_review_status 列")
    result = []
    for row in rows:
        candidate_id = row["candidate_id"]
        status = row["user_review_status"]
        if status == "存疑":
            continue
        if any(not str(row.get(k) or "").strip() for k in ("open_question", "open_answer", "candidate_question")):
            raise ValueError(f"{candidate_id}: Q0/A0/Q1 不能为空")
        if status == "同意" and (any(row.get(dim[3]) not in (None, "") for dim in DIMENSIONS) or row.get("user_final_label") not in (None, "")):
            raise ValueError(f"{candidate_id}: 标为“同意”却填写了改分/最终标签；请改用“修改”")
        scores = []
        for _, _, original, override in DIMENSIONS:
            value = row.get(override) if status == "修改" and row.get(override) not in (None, "") else row.get(original)
            scores.append(parse_int_score(value, override if status == "修改" else original, candidate_id))
        label = row.get("intended_label") if status == "同意" else row.get("user_final_label")
        label = str(label or "").strip().lower()
        if label not in ("good", "bad", "uncertain"):
            raise ValueError(f"{candidate_id}: 最终标签必须是 good/bad/uncertain；修改行请填写 user_final_label")
        total, decision = composite(scores)
        result.append({
            "candidate_id": candidate_id,
            "seed_id": str(row.get("seed_id") or "").strip(),
            "q0": str(row["open_question"]),
            "a0": str(row["open_answer"]),
            "q1": str(row["candidate_question"]),
            "human": scores,
            "human_total": total,
            "human_decision": decision,
            "human_label": label,
        })
    if not result:
        raise ValueError("没有可评测的已审核样本（全部为存疑）")
    return result


def user_payload(row: dict[str, Any]) -> str:
    """模型只看到这三个文本字段，不看到候选 ID、设计标签或人工评分。"""
    return json.dumps({"Q0": row["q0"], "A0": row["a0"], "Q1": row["q1"]}, ensure_ascii=False)


def parse_model_content(content: str) -> tuple[list[int | None], dict[str, str]]:
    obj = json.loads(content)
    if not isinstance(obj, dict) or set(obj) != {dim[0] for dim in DIMENSIONS}:
        raise ValueError("模型 JSON 的五维字段不完整")
    scores: list[int | None] = []
    reasons: dict[str, str] = {}
    for key, _, _, _ in DIMENSIONS:
        item = obj[key]
        if not isinstance(item, dict) or set(item) != {"score", "reason"}:
            raise ValueError(f"{key}: 应包含 score 和 reason")
        score = item["score"]
        if score is not None and (type(score) is not int or score not in (1, 2, 3)):
            raise ValueError(f"{key}: score 超出 1–3")
        if key in ("language", "clarity") and score is None:
            raise ValueError(f"{key}: 不允许 null")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValueError(f"{key}: 缺少评分理由")
        scores.append(score)
        reasons[key] = item["reason"].strip()
    composite(scores)
    return scores, reasons


def post_completion(api_url: str, api_key: str, model: str, row: dict[str, Any]) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_payload(row)},
        ],
        "response_format": response_format_for_model(model),
    }
    request = Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def log_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def prepare_manifest(out_dir: Path, rows: list[dict[str, Any]], source: Path, model: str, api_url: str) -> dict[str, Any]:
    manifest_path = out_dir / "manifest.json"
    identity = {
        "input_sha256": sha256_bytes(source.read_bytes()),
        "prompt_sha256": sha256_bytes(SYSTEM_PROMPT.encode("utf-8")),
        "rubric_version": RUBRIC_VERSION,
        "model": model,
        "api_url": api_url,
        "response_format": response_format_for_model(model)["type"],
        "candidate_ids": sorted(row["candidate_id"] for row in rows),
    }
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise ValueError("输出目录已有不同输入、规则或模型的记录；请换一个 --out-dir")
        return manifest
    order = [row["candidate_id"] for row in rows]
    secrets.SystemRandom().shuffle(order)
    manifest = {**identity, "created_at": now_text(), "order": order, "blind_ids": {cid: f"B{index:03d}" for index, cid in enumerate(order, 1)}}
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def score_with_retries(api_url: str, api_key: str, model: str, row: dict[str, Any], blind_id: str, attempts_path: Path) -> dict[str, Any]:
    for attempt in range(1, 4):
        log = {"candidate_id": row["candidate_id"], "blind_id": blind_id, "attempt": attempt, "time": now_text()}
        try:
            raw_response = post_completion(api_url, api_key, model, row)
            log["raw_response"] = raw_response
            response = json.loads(raw_response)
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"模型输出未正常结束：{choice.get('finish_reason')}")
            message = choice["message"]
            if message.get("refusal"):
                raise ValueError("模型拒绝评分")
            scores, reasons = parse_model_content(message["content"])
            log["ok"] = True
            log_jsonl(attempts_path, log)
            return {"candidate_id": row["candidate_id"], "blind_id": blind_id, "scores": scores, "reasons": reasons, "response_model": response.get("model", ""), "system_fingerprint": response.get("system_fingerprint", "")}
        except HTTPError as exc:
            body = exc.read(2000).decode("utf-8", errors="replace").replace(api_key, "[REDACTED]")
            log.update({"ok": False, "http_status": exc.code, "error": body})
            log_jsonl(attempts_path, log)
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise ValueError(f"{blind_id}: API HTTP {exc.code}：{body[:300]}") from exc
        except (URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.update({"ok": False, "error": str(exc).replace(api_key, "[REDACTED]")})
            log_jsonl(attempts_path, log)
        if attempt < 3:
            time.sleep(attempt * 2)
    raise ValueError(f"{blind_id}: 连续 3 次调用/解析失败；原始记录已保存到 {attempts_path}")


def agreement(human: list[int], model: list[int | None]) -> dict[str, Any]:
    if len(human) != len(model):
        raise ValueError("人工与模型分数条数不一致")
    pairs = [(h, m) for h, m in zip(human, model) if m is not None]
    n = len(pairs)
    matrix = [[0] * 3 for _ in range(3)]
    for h, m in pairs:
        matrix[h - 1][m - 1] += 1
    if not n:
        return {"n": 0, "exact": None, "mae": None, "kappa": None, "matrix": matrix}
    exact = sum(matrix[i][i] for i in range(3)) / n
    mae = sum(abs(h - m) for h, m in pairs) / n
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(matrix[i][j] for i in range(3)) for j in range(3)]
    observed = sum(abs(i - j) / 2 * matrix[i][j] for i in range(3) for j in range(3))
    expected = sum(abs(i - j) / 2 * row_totals[i] * col_totals[j] / n for i in range(3) for j in range(3))
    kappa = None if expected == 0 else 1 - observed / expected
    return {"n": n, "exact": exact, "mae": mae, "kappa": kappa, "matrix": matrix}


def build_report(rows: list[dict[str, Any]], model_scores: dict[str, dict[str, Any]], *, model: str, input_name: str, review_counts: Counter[str]) -> dict[str, Any]:
    cases = []
    for row in rows:
        prediction = model_scores[row["candidate_id"]]
        model_vector = prediction["scores"]
        model_total, model_decision = composite(model_vector)
        critic_human_score, critic_human_decision = score_and_decide(row["human"])
        critic_model_score, critic_model_decision = score_and_decide(model_vector)
        cases.append({
            "candidate_id": row["candidate_id"], "q0": row["q0"], "a0": row["a0"], "q1": row["q1"],
            "human": row["human"], "model": model_vector,
            "human_total": row["human_total"], "model_total": model_total,
            "human_decision": row["human_decision"], "model_decision": model_decision,
            "critic_human_score": critic_human_score, "critic_model_score": critic_model_score,
            "critic_human_decision": critic_human_decision, "critic_model_decision": critic_model_decision,
            "human_label": row["human_label"], "reasons": prediction["reasons"],
        })
    dimensions = []
    for index, (key, label, _, _) in enumerate(DIMENSIONS):
        result = agreement([case["human"][index] for case in cases], [case["model"][index] for case in cases])
        dimensions.append({"key": key, "label": label, **result})
    n = len(cases)
    total_abs = sum(abs(case["human_total"] - case["model_total"]) for case in cases)
    composite_result = {"n": n, "exact": sum(case["human_total"] == case["model_total"] for case in cases) / n, "mae": total_abs / n}
    decision_matrix = [[0] * 3 for _ in range(3)]
    for case in cases:
        decision_matrix[DECISIONS.index(case["human_decision"])][DECISIONS.index(case["model_decision"])] += 1
    critic_decisions = ("拒绝", "通过")
    critic_matrix = [[0] * 2 for _ in range(2)]
    for case in cases:
        critic_matrix[critic_decisions.index(case["critic_human_decision"])][critic_decisions.index(case["critic_model_decision"])] += 1
    critic_score_pairs = [
        (case["critic_human_score"], case["critic_model_score"])
        for case in cases
        if case["critic_human_score"] is not None and case["critic_model_score"] is not None
    ]
    critic_result = {
        "weights": list(FROZEN_WEIGHTS), "threshold": THRESHOLD, "n": n,
        "score_n": len(critic_score_pairs),
        "score_mae": sum(abs(human - model_score) for human, model_score in critic_score_pairs) / len(critic_score_pairs) if critic_score_pairs else None,
        "binary_exact": sum(case["critic_human_decision"] == case["critic_model_decision"] for case in cases) / n,
        "matrix": critic_matrix,
    }
    bad_cases = [case for case in cases if case["human_label"] == "bad"]
    good_cases = [case for case in cases if case["human_label"] == "good"]
    safety = {
        "bad_total": len(bad_cases), "bad_false_pass": sum(case["critic_model_decision"] == "通过" for case in bad_cases),
        "good_total": len(good_cases), "good_false_reject": sum(case["critic_model_decision"] == "拒绝" for case in good_cases),
    }
    cases.sort(
        key=lambda case: (
            case["human_label"] == "bad" and case["critic_model_decision"] == "通过",
            case["critic_human_decision"] != case["critic_model_decision"],
            abs((case["critic_human_score"] or 0) - (case["critic_model_score"] or 0)),
        ),
        reverse=True,
    )
    dataset_label = "开发集" if input_name == DEFAULT_INPUT.name else "已审核样本"
    note = (
        "参考分数由 Codex 起草，用户整体接受；未进行独立人工逐条打分。当前 40 条开发候选只用于校准，结果不能当作独立测试集结论。"
        if dataset_label == "开发集"
        else f"本报告比较输入表中 {n} 条已确认参考评分；结论仅适用于本次样本。"
    )
    if review_counts.get("存疑", 0):
        note += f" 另有 {review_counts['存疑']} 条标为存疑，未参与对照。"
    return {
        "title": "第三任务：五维与 CRITIC 判定评测报告", "status": "ready", "note": note,
        "dataset_label": dataset_label,
        "generated_at": now_text(), "model": model, "rubric_version": RUBRIC_VERSION, "input_name": input_name,
        "review_counts": dict(review_counts), "sample_count": n, "dimensions": dimensions,
        "critic": critic_result, "composite": composite_result, "decisions": {"matrix": decision_matrix},
        "safety": safety, "cases": cases,
    }


def write_scores_csv(path: Path, cases: list[dict[str, Any]]) -> None:
    headers = [
        "candidate_id", "reference_label",
        "critic_reference_score", "critic_model_score", "critic_reference_decision", "critic_model_decision",
        "reference_total", "model_total", "reference_decision", "model_decision",
    ]
    headers += [f"reference_{dim[0]}" for dim in DIMENSIONS] + [f"model_{dim[0]}" for dim in DIMENSIONS]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for case in cases:
            line = {
                "candidate_id": case["candidate_id"], "reference_label": case["human_label"],
                "critic_reference_score": "" if case["critic_human_score"] is None else f"{case['critic_human_score']:.6f}",
                "critic_model_score": "" if case["critic_model_score"] is None else f"{case['critic_model_score']:.6f}",
                "critic_reference_decision": case["critic_human_decision"],
                "critic_model_decision": case["critic_model_decision"],
                "reference_total": case["human_total"], "model_total": case["model_total"],
                "reference_decision": case["human_decision"], "model_decision": case["model_decision"],
            }
            for index, dim in enumerate(DIMENSIONS):
                line[f"reference_{dim[0]}"] = case["human"][index]
                line[f"model_{dim[0]}"] = case["model"][index]
            writer.writerow(line)


def run(args: argparse.Namespace) -> None:
    settings = resolve_settings(args)
    rows, counts = read_review_sheet(settings.input)
    if settings.command == "preview":
        report = {
            "title": "第三任务：五维与 CRITIC 判定评测报告", "status": "pending", "note": "当前只是报告版式预览：未调用 LLM，也没有拟合指标。",
            "generated_at": now_text(), "model": "未运行", "rubric_version": RUBRIC_VERSION,
            "input_name": settings.input.name, "review_counts": dict(counts), "sample_count": 0,
        }
        settings.out_dir.mkdir(parents=True, exist_ok=True)
        path = settings.out_dir / "报告预览_未评测.html"
        path.write_text(render_report(report), encoding="utf-8")
        print(f"已生成预览：{path}")
        return
    approved = approved_rows(rows)
    if not settings.api_key:
        raise ValueError("缺少 API Key；请在 config.yaml 填写 api_key 或设置 OPENAI_API_KEY")
    if not settings.model:
        raise ValueError("缺少模型名；请在 config.yaml 填写 model 或通过 --model 指定")
    manifest = prepare_manifest(settings.out_dir, approved, settings.input, settings.model, settings.api_url)
    by_id = {row["candidate_id"]: row for row in approved}
    scores_path = settings.out_dir / "model_scores.jsonl"
    existing = load_jsonl(scores_path)
    predictions = {record["candidate_id"]: record for record in existing}
    if len(predictions) != len(existing) or any(cid not in by_id for cid in predictions):
        raise ValueError("已有模型评分记录包含重复/未知候选 ID；请核对输出目录")
    attempts_path = settings.out_dir / "attempts.jsonl"
    for index, candidate_id in enumerate(manifest["order"], 1):
        if candidate_id in predictions:
            continue
        print(f"[{index}/{len(approved)}] 评分 {manifest['blind_ids'][candidate_id]} ...", flush=True)
        record = score_with_retries(settings.api_url, settings.api_key, settings.model, by_id[candidate_id], manifest["blind_ids"][candidate_id], attempts_path)
        log_jsonl(scores_path, record)
        predictions[candidate_id] = record
    report = build_report(approved, predictions, model=settings.model, input_name=settings.input.name, review_counts=counts)
    report_path = settings.out_dir / "评测报告.html"
    report_path.write_text(render_report(report), encoding="utf-8")
    write_scores_csv(settings.out_dir / "逐条评分对照.csv", report["cases"])
    print(f"评测完成：{report_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="第三任务五维评分评测")
    parser.add_argument("command", choices=("preview", "run"), help="preview 只看 HTML 版式；run 在审核后调用 LLM 并生成正式报告")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="YAML 配置文件；默认 src/config.yaml")
    parser.add_argument("--input", type=Path, help="覆盖 YAML 中的审核表路径")
    parser.add_argument("--out-dir", type=Path, help="覆盖 YAML 中的报告输出目录")
    parser.add_argument("--model", help="覆盖 YAML 中的模型名")
    parser.add_argument("--api-url", help="覆盖 YAML 中的 Chat Completions 接口 URL")
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except ValueError as exc:
        print(f"评测停止：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
