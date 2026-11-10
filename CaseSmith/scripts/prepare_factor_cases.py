"""Prepare a pilot set of factor-constrained QA cases using Microsoft PICT."""

import argparse
import csv
import hashlib
import io
import itertools
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIMENSIONS = {
    "上下文跨度": ("Span", ("Short", "Medium", "MediumLong", "Long")),
    "上下文类型": ("Context", ("SameTopic", "OtherTask")),
    "修正类型": ("Correction", ("Correct", "Add", "Cancel")),
    "修正位置": ("Position", ("Immediate", "Delayed")),
    "修正方式": ("Method", ("Explicit", "Vague")),
    "修正对象": ("Object", ("OneParameter", "MultipleParameters", "EntireIntent")),
}
RULES = (
    'IF [Span] = "Short" THEN [Context] = "SameTopic" AND [Correction] = "NA";',
    'IF [Correction] = "NA" THEN [Position] = "NA" AND [Method] = "NA" AND [Object] = "NA";',
    'IF [Correction] <> "NA" THEN [Position] <> "NA" AND [Method] <> "NA" AND [Object] <> "NA";',
)
# Each pilot seed can support the corresponding special factor without changing
# the source question or reference answer stored in the output.
PILOT = (
    ("1524_english_oeq", "上下文指代理解", (0, 1, 2, 1)),
    ("1771_english_oeq", "模糊意图识别", (1, 0, 1)),
    ("1771_english_oeq", "依赖、转折多意图（多意图考虑）", (0, 1, None, 0, 1)),
    ("1771_english_oeq", "依赖、转折多意图（多意图考虑）", (1, None, 1, None, None)),
)
TRANSPORT = (
    ("r1979:i0", "上下文指代理解", (0, 0, 0, 0)),
    ("r1980:i0", "上下文指代理解", (1, 1, 1, 0)),
    ("r1981:i0", "上下文指代理解", (2, 1, 2, 1)),
    ("r1982:i0", "模糊意图识别", (0, 0, 0)),
    ("r1722:i0", "模糊意图识别", (2, 1, 1)),
    ("r1996:i0", "模糊意图识别", (1, 2, 1)),
    ("r1991:i0", "依赖、转折多意图（多意图考虑）", (0, 0, None, 0, 1)),
    ("r1984:i0", "依赖、转折多意图（多意图考虑）", (0, 1, None, 1, 1)),
    ("r1997:i0", "依赖、转折多意图（多意图考虑）", (0, 2, None, 0, 0)),
    ("r2002:i0", "依赖、转折多意图（多意图考虑）", (1, None, 0, None, None)),
    ("r1985:i0", "依赖、转折多意图（多意图考虑）", (1, None, 1, None, None)),
)
NA = "不适用"


def flatten(groups):
    return {group: {name: values for item in dimensions for name, values in item.items()}
            for entry in groups for group, dimensions in entry.items()}


def load_general(library):
    source = {name: values for group in flatten(library["通用因子"]).values()
              for name, values in group.items()}
    if set(source) != set(DIMENSIONS):
        raise ValueError("通用因子维度与 PICT 规则不匹配")
    labels = {}
    for name, (key, tokens) in DIMENSIONS.items():
        labels[key] = dict(zip(tokens, source[name], strict=True))
        if key not in ("Span", "Context"):
            labels[key]["NA"] = NA
    return labels


def legal(values):
    span, context, correction, position, method, obj = values
    if span == "Short" and (context != "SameTopic" or correction != "NA"):
        return False
    return all((value == "NA") == (correction == "NA")
               for value in (position, method, obj))


def pairs(rows):
    return {pair for row in rows for pair in itertools.combinations(enumerate(row), 2)}


def exclude_previous(path, columns, valid):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if not set(columns).issubset(reader.fieldnames or ()):
            raise ValueError("历史组合表缺少通用因子列")
        previous = {tuple(row[key] for key in columns) for row in reader}
    if previous - valid:
        raise ValueError("历史组合表包含与当前模型不兼容的组合")

    counts = Counter(pair for row in valid
                     for pair in itertools.combinations(enumerate(row), 2))
    excluded = set()
    # Exclude an old tuple only when every pair it covers still has a witness.
    for row in sorted(previous):
        row_pairs = list(itertools.combinations(enumerate(row), 2))
        if all(counts[pair] > 1 for pair in row_pairs):
            excluded.add(row)
            counts.subtract(row_pairs)
    return previous, excluded


def read_seeds(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        seeds = {}
        for row in reader:
            if row["id"] in seeds:
                raise ValueError(f'重复 QA ID {row["id"]}')
            seeds[row["id"]] = row
        return reader.fieldnames, seeds


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def make_plan(combo, general, special):
    span, context, correction, position, method, obj = combo
    distance = {"Short": 0, "Medium": 2, "MediumLong": 6, "Long": 11}[span]
    probe = distance + 1
    required_turns = probe
    events = []
    if distance:
        events.append({"turn": 1, "event": "引入后续要引用的种子主题和信息"})
    if correction != "NA":
        # Keep the correction setup separate from the information used for the
        # context probe, so an immediate correction can still be placed later.
        gap = 1 if position == "Immediate" else 3
        correction_turn = max(probe, gap + 1)
        setup = correction_turn - gap
        required_turns = correction_turn
        events.append({"turn": setup, "event": "引入稍后修正的独立信息",
                       "object": general["修正对象"]})
        events.append({"turn": correction_turn, "event": "修正该信息",
                       "type": general["修正类型"], "method": general["修正方式"],
                       "object": general["修正对象"], "setup_turn": setup})
    if context == "OtherTask":
        events.append({"turn": 2, "event": "插入其他任务；与该轮已有事件并存时分开表达"})
    events.append({"turn": required_turns, "event": "提出种子相关任务", "special_factors": special})
    expected = []
    if distance:
        expected.append("使用第 1 轮中与当前任务相关的信息")
    if correction != "NA":
        expected.append("按用户修正更新相关信息，并保留其余有效信息")
    if "指代确认" in special:
        expected.append(f'指代澄清要求为「{special["指代确认"]}」')
    if "澄清需求" in special:
        expected.append(f'处理模糊意图时「{special["澄清需求"]}」')
    if "逻辑关系" in special:
        expected.append(f'按「{special["逻辑关系"]}」处理多个意图')
    return {"required_turns": required_turns,
            "events": sorted(events, key=lambda e: e["turn"])}, expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factors", type=Path,
                        default=ROOT.parent / "initial_data/factor_library_test.json")
    parser.add_argument("--qa", type=Path, default=ROOT / "QA_case.csv")
    parser.add_argument("--profile", choices=("pilot", "transport"), default="pilot")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/factor_cases")
    parser.add_argument("--pict", default="pict", help="PICT binary name or path")
    parser.add_argument("--avoid-combinations", type=Path, metavar="TSV",
                        help="Prefer combinations different from a previous scenario")
    parser.add_argument("--model-only", action="store_true", help="Write only the PICT model")
    args = parser.parse_args()

    data = args.factors.read_bytes()
    library = json.loads(data)
    labels = load_general(library)
    valid = {row for row in itertools.product(*labels.values()) if legal(row)}
    previous, excluded = set(), set()
    if args.avoid_combinations:
        if args.avoid_combinations.resolve() == (
            args.output_dir / "general_combinations.tsv"
        ).resolve():
            parser.error("请为新场景指定不同的 --output-dir")
        previous, excluded = exclude_previous(args.avoid_combinations, list(labels), valid)
    exclusions = [
        "NOT (" + " AND ".join(f'[{key}] = "{value}"'
                              for key, value in zip(labels, row)) + ");"
        for row in sorted(excluded)
    ]
    special_groups = flatten(library["特殊因子"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model = args.output_dir / "general_factors.pict"
    model.write_text("\n".join([*(f'{key}: {", ".join(values)}' for key, values in labels.items()),
                                "", *RULES, *exclusions, ""]), encoding="utf-8")
    if args.model_only:
        print(model)
        return
    pict = shutil.which(args.pict)
    if pict is None:
        parser.error("找不到 PICT，请先安装，或用 --pict 指定路径")
    result = subprocess.run([pict, str(model), "/o:2"], check=True,
                            capture_output=True, encoding="utf-8")
    reader = csv.reader(io.StringIO(result.stdout), delimiter="\t")
    if next(reader, None) != list(labels):
        raise ValueError("PICT 输出表头与模型不匹配")
    combos = [tuple(row) for row in reader if row]
    if set(combos) - (valid - excluded) or len(set(combos)) != len(combos):
        raise ValueError("PICT 输出包含非法或重复组合")
    required, covered = pairs(valid), pairs(combos)
    if required != covered:
        raise ValueError(f"PICT 输出缺少 {len(required - covered)} 个合法取值对")

    fields, seeds = read_seeds(args.qa)
    special_universe = {(group, name, value)
                        for group, dimensions in special_groups.items()
                        for name, values in dimensions.items() for value in values
                        if "不考虑" not in value}
    if args.profile == "transport":
        reserved = {seed_id for seed_id, _, _ in TRANSPORT}
        if reserved - seeds.keys():
            parser.error(f"交通种子表缺少用例: {', '.join(sorted(reserved - seeds.keys()))}")
        if len(seeds) < len(combos) or sum(combo[0] != "Short" for combo in combos) < len(TRANSPORT):
            parser.error("种子或可分配特殊因子的 PICT 组合不足")
        general_seeds = iter(seed_id for seed_id in seeds if seed_id not in reserved)
    added_fields = ["seed_id", "combo_id", "case_factors", "dialogue_plan_json",
                    "expected_behavior_json", "factor_library_version"]
    if len(set([*fields, *added_fields])) != len(fields) + len(added_fields):
        raise ValueError("输入 CSV 或因子库包含冲突的表头")
    version = "sha256:" + hashlib.sha256(data).hexdigest()
    cases = []
    special_counts = Counter()
    special_values = set()
    eligible_index = 0
    for index, combo in enumerate(combos, 1):
        combo_id = f"G{index:03d}"
        general = {name: labels[key][value]
                   for (name, (key, _)), value in zip(DIMENSIONS.items(), combo)}
        seed_id, group, special = "1524_english_oeq", "", {}
        if args.profile == "transport":
            if combo[0] != "Short" and eligible_index < len(TRANSPORT):
                seed_id, group, choices = TRANSPORT[eligible_index]
                eligible_index += 1
            else:
                seed_id = next(general_seeds)
        elif combo[0] != "Short":
            seed_id, group, choices = PILOT[eligible_index % len(PILOT)]
            eligible_index += 1
        if group:
            dimensions = special_groups[group]
            special = {name: values[choice] if choice is not None else NA
                       for (name, values), choice in zip(dimensions.items(), choices, strict=True)}
            special_counts[group] += 1
            special_values.update((group, name, value) for name, value in special.items()
                                  if value != NA)
        plan, expected = make_plan(combo, general, special)
        cases.append({
            **seeds[seed_id], "id": f"{seed_id}__{combo_id}", "seed_id": seed_id,
            "combo_id": combo_id,
            "case_factors": encode({"通用因子": general, "特殊因子": {group: special} if group else {}}),
            "dialogue_plan_json": encode(plan), "expected_behavior_json": encode(expected),
            "factor_library_version": version,
        })

    if args.profile == "transport" and special_values != special_universe:
        raise ValueError(f"交通用例特殊因子取值未全覆盖: {special_universe - special_values}")

    with (args.output_dir / "general_combinations.tsv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["combo_id", *labels])
        writer.writerows((case["combo_id"], *combo) for case, combo in zip(cases, combos))
    with (args.output_dir / "QA_case_constrained.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=[*fields, *added_fields])
        writer.writeheader()
        writer.writerows(cases)

    report = [
        "# CaseSmith 离线因子覆盖报告", "",
        "通用组合由 Microsoft PICT 以 `/o:2` 生成。",
        f"- 合法完整组合数为 {len(valid)}。",
        f"- 通用组合和计划用例均为 {len(cases)} 条。",
        f"- 合法取值对覆盖 {len(covered)}/{len(required)}，含适用性值 NA。",
        f"- 特殊因子值覆盖 {len(special_values)}/{len(special_universe)}。",
        f"- 无特殊因子的用例有 {len(cases) - sum(special_counts.values())} 条。",
        f"- 因子库版本为 `{version}`。",
        "",
        (f"从 {len(seeds)} 条交通种子中选取 {len(cases)} 条不同的种子，每个通用组合分配一条。"
         if args.profile == "transport"
         else "特殊因子由 Socotra 词源和博物馆票价两条种子承载。"),
        "",
        *[f"- {group}共有 {special_counts[group]} 条。" for group in special_groups],
        "",
        *[f"- {group} / {name} / {value}" for group, name, value in
          sorted(special_universe - special_values)],
        "",
        "本报告统计离线计划。实际对话需要逐轮验证事件和预期行为。",
    ]
    if args.avoid_combinations:
        overlap = set(combos) & previous
        report.extend([
            "", "## 与上一场景的组合对比", "",
            f"- 历史组合 {len(previous)} 条，排除其中 {len(excluded)} 条。",
            f"- 本场景新增完整组合 {len(combos) - len(overlap)} 条，"
            f"复用 {len(overlap)} 条以维持合法取值对覆盖。",
        ])
    (args.output_dir / "coverage_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"{len(cases)} cases, {len(covered)}/{len(required)} legal pairs covered")
    print(args.output_dir)


if __name__ == "__main__":
    main()
