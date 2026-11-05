"""第三任务五维评测的独立 HTML 报告。"""

from __future__ import annotations

from html import escape


DIMENSION_LABELS = ["语言自然", "表述清晰", "内部逻辑", "原意忠实", "原答案匹配"]
DECISION_LABELS = ["拒绝", "人工复核", "通过"]
BINARY_DECISION_LABELS = ["拒绝", "通过"]
REASON_LABELS = {
    "language": "语言自然", "clarity": "表述清晰", "logic": "内部逻辑",
    "fidelity": "原意忠实", "answer": "原答案匹配",
}


def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _decimal(value: object, digits: int = 3) -> str:
    if value is None:
        return "未定义"
    return f"{float(value):.{digits}f}"


def _percentage(value: object) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _critic_value(value: object) -> str:
    return "—" if value is None else _decimal(value, 4)


def _pill(value: object) -> str:
    label = str(value or "—")
    css_class = {"通过": "pass", "人工复核": "review", "拒绝": "reject"}.get(label, "neutral")
    return f'<span class="pill {css_class}">{_e(label)}</span>'


def _score_list(values: object) -> str:
    if not isinstance(values, (list, tuple)):
        return "—"
    return " / ".join("—" if value is None else _e(value) for value in values)


def _matrix(matrix: object, labels: list[str], caption: str, axis_label: str = "参考分 ↓ / LLM →") -> str:
    rows = matrix if isinstance(matrix, list) else []
    head = "".join(f"<th scope=\"col\">{_e(label)}</th>" for label in labels)
    body = []
    for index, label in enumerate(labels):
        cells = rows[index] if index < len(rows) and isinstance(rows[index], list) else []
        values = "".join(
            f'<td class="{"diagonal" if index == column else ""}">{_e(cells[column] if column < len(cells) else 0)}</td>'
            for column in range(len(labels))
        )
        body.append(f'<tr><th scope="row">{_e(label)}</th>{values}</tr>')
    return (
        f'<div class="matrix-wrap"><table class="matrix"><caption>{_e(caption)}</caption>'
        f'<thead><tr><th scope="col">{_e(axis_label)}</th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _card(label: str, value: str, detail: str = "") -> str:
    return (
        '<div class="metric-card">'
        f'<div class="metric-label">{_e(label)}</div>'
        f'<div class="metric-value">{value}</div>'
        f'<div class="metric-detail">{_e(detail)}</div>'
        '</div>'
    )


def _case_details(case: dict) -> str:
    reasons = case.get("reasons") or {}
    if isinstance(reasons, dict) and reasons:
        reason_rows = "".join(
            f'<dt>{_e(REASON_LABELS.get(key, key))}</dt><dd>{_e(value)}</dd>'
            for key, value in reasons.items()
        )
        reason_html = f'<dl class="reasons">{reason_rows}</dl>'
    else:
        reason_html = '<p class="muted">未提供逐维理由。</p>'
    return (
        '<div class="case-text">'
        f'<p><strong>原问题 Q0：</strong>{_e(case.get("q0"))}</p>'
        f'<p><strong>原答案 A0：</strong>{_e(case.get("a0"))}</p>'
        f'<p><strong>候选问题 Q1：</strong>{_e(case.get("q1"))}</p>'
        f'{reason_html}</div>'
    )


def _case_row(case: dict) -> str:
    mismatch = case.get("critic_human_decision") != case.get("critic_model_decision")
    row_class = ' class="mismatch"' if mismatch else ""
    return (
        f'<tr{row_class}>'
        f'<td class="mono">{_e(case.get("candidate_id"))}</td>'
        f'<td>{_score_list(case.get("human"))}</td>'
        f'<td>{_score_list(case.get("model"))}</td>'
        f'<td>{_critic_value(case.get("critic_human_score"))} {_pill(case.get("critic_human_decision"))}</td>'
        f'<td>{_critic_value(case.get("critic_model_score"))} {_pill(case.get("critic_model_decision"))}</td>'
        f'<td>参考 {_e(case.get("human_total"))} {_pill(case.get("human_decision"))}<br>'
        f'LLM {_e(case.get("model_total"))} {_pill(case.get("model_decision"))}</td>'
        f'<td>{_e(case.get("human_label"))}</td>'
        f'<td><details><summary>查看原文与理由</summary>{_case_details(case)}</details></td>'
        '</tr>'
    )


def _error_case(case: dict) -> str:
    human_decision = case.get("critic_human_decision")
    model_decision = case.get("critic_model_decision")
    tag = "CRITIC 判定不一致"
    if case.get("human_label") == "bad" and model_decision == "通过":
        tag = "坏题误放行"
    elif case.get("human_label") == "good" and model_decision == "拒绝":
        tag = "好题误拒绝"
    return (
        '<article class="error-case">'
        f'<div class="error-heading"><strong>{_e(case.get("candidate_id"))}</strong>'
        f'<span class="error-tag">{_e(tag)}</span></div>'
        f'<p>{_e(case.get("q1"))}</p>'
        f'<div class="comparison"><span>参考 CRITIC {_critic_value(case.get("critic_human_score"))} {_pill(human_decision)}</span>'
        f'<span>LLM CRITIC {_critic_value(case.get("critic_model_score"))} {_pill(model_decision)}</span></div>'
        f'<details><summary>查看 Q0、A0 与评分理由</summary>{_case_details(case)}</details>'
        '</article>'
    )


def _pending(report: dict) -> str:
    counts = report.get("review_counts") or {}
    progress = " · ".join(f"{_e(key)} { _e(value) } 条" for key, value in counts.items())
    return (
        '<section class="panel pending-panel">'
        '<h2>评测尚未运行</h2>'
        f'<p>{_e(report.get("note") or "请确认参考评分，再运行 LLM 盲评。")}</p>'
        f'<p class="muted">审核进度：{progress or "暂无审核记录"}。未调用模型，暂无拟合指标。</p>'
        '</section>'
    )


def _ready(report: dict) -> str:
    dimensions = report.get("dimensions") or []
    critic = report.get("critic") or {}
    composite = report.get("composite") or {}
    safety = report.get("safety") or {}
    decisions = report.get("decisions") or {}
    cases = report.get("cases") or []
    bad_total = int(safety.get("bad_total") or 0)
    bad_false_pass = int(safety.get("bad_false_pass") or 0)
    good_total = int(safety.get("good_total") or 0)
    good_false_reject = int(safety.get("good_false_reject") or 0)
    bad_rate = _percentage(bad_false_pass / bad_total) if bad_total else "—"
    good_rate = _percentage(good_false_reject / good_total) if good_total else "—"

    summary = ''.join([
        _card("有效样本", _e(report.get("sample_count", len(cases))), "已确认且参与对照的候选数"),
        _card("CRITIC 判定一致率", _percentage(critic.get("binary_exact")), "参考与 LLM 的通过／拒绝决定相同"),
        _card("CRITIC 总分 MAE", _decimal(critic.get("score_mae"), 4), "0–1 总分平均相差多少；越小越好"),
        _card("坏题误放行", bad_rate, f"{bad_false_pass} / {bad_total} 条参考标签 bad 被 LLM 判为通过"),
    ])
    dimension_rows = []
    dimension_matrices = []
    for dimension in dimensions:
        label = str(dimension.get("label") or dimension.get("key") or "未命名维度")
        dimension_rows.append(
            '<tr>'
            f'<th scope="row">{_e(label)}</th>'
            f'<td>{_e(dimension.get("n", 0))}</td>'
            f'<td>{_percentage(dimension.get("exact"))}</td>'
            f'<td>{_decimal(dimension.get("mae"))}</td>'
            f'<td>{_decimal(dimension.get("kappa"))}</td>'
            '</tr>'
        )
        dimension_matrices.append(
            f'<details class="matrix-detail"><summary>{_e(label)}：参考 / LLM 分数对照</summary>'
            f'{_matrix(dimension.get("matrix"), ["1", "2", "3"], label + " 3×3 对照表")}'
            '</details>'
        )

    def is_critic_issue(case: dict) -> bool:
        return (
            case.get("critic_human_decision") != case.get("critic_model_decision")
            or (case.get("human_label") == "bad" and case.get("critic_model_decision") == "通过")
            or (case.get("human_label") == "good" and case.get("critic_model_decision") == "拒绝")
        )

    issues = [case for case in cases if is_critic_issue(case)]
    issues.sort(
        key=lambda case: (
            case.get("human_label") == "bad" and case.get("critic_model_decision") == "通过",
            case.get("human_label") == "good" and case.get("critic_model_decision") == "拒绝",
            case.get("critic_human_decision") != case.get("critic_model_decision"),
            abs((case.get("critic_human_score") or 0) - (case.get("critic_model_score") or 0)),
        ),
        reverse=True,
    )
    error_html = ''.join(_error_case(case) for case in issues[:10]) or '<p class="muted">本批样本没有 CRITIC 判定分歧或好坏标签错判。</p>'
    all_rows = ''.join(_case_row(case) for case in cases) or '<tr><td colspan="8">暂无逐条记录。</td></tr>'
    critic_matrix = _matrix(
        critic.get("matrix"), BINARY_DECISION_LABELS, "CRITIC 通过／拒绝 2×2 对照表", "参考决定 ↓ / LLM →",
    )
    legacy_matrix = _matrix(decisions.get("matrix"), DECISION_LABELS, "旧规则处置决定 3×3 对照表", "参考决定 ↓ / LLM →")
    weights = critic.get("weights") or []
    weight_rows = ''.join(
        f'<tr><th scope="row">{_e(label)}</th><td>{_decimal(weights[index], 4)}</td></tr>'
        for index, label in enumerate(DIMENSION_LABELS) if index < len(weights)
    )

    return (
        '<section class="metric-grid">' + summary + '</section>'
        '<section class="panel"><h2>CRITIC 总分与通过判定（试验）</h2>'
        '<p>五维先将 1／2／3 分换成 0／0.5／1，再按下表固定权重相加，得到 0–1 的总分。'
        f'任一维为 1 分直接拒绝；其余题总分达到 <strong>{_decimal(critic.get("threshold"), 2)}</strong> 通过，低于此线拒绝。'
        '内容维无法判定时不强填总分；若语言或清晰度为 1 分仍拒绝。</p>'
        '<div class="table-wrap"><table class="weights-table"><thead><tr><th>维度</th><th>冻结的 CRITIC 权重</th></tr></thead>'
        f'<tbody>{weight_rows}</tbody></table></div>'
        '<p class="muted">权重由 40 条开发候选的参考五维评分估计并冻结；LLM 和后续样本使用同一组权重。'
        'CRITIC 论文提供估权方法，1 分否决和 0.80 通过线是项目试验规则。阈值来自同一开发集，需用独立标注的新题验证。</p>'
        f'<p>参考／LLM 的 CRITIC 判定一致率 <strong>{_percentage(critic.get("binary_exact"))}</strong>；'
        f'总分 MAE <strong>{_decimal(critic.get("score_mae"), 4)}</strong>'
        f'（双方总分均可计算的 {_e(critic.get("score_n", 0))} 条）。</p>'
        f'{critic_matrix}'
        f'<p class="safety-line">坏题误放行：<strong>{bad_false_pass}/{bad_total}（{bad_rate}）</strong>　'
        f'好题误拒绝：<strong>{good_false_reject}/{good_total}（{good_rate}）</strong></p>'
        '<p class="muted">参考标签为 uncertain 的候选不进入 good / bad 比率分母，但参与判定一致性对照。</p>'
        '</section>'
        '<section class="panel"><h2>五维一致性</h2>'
        '<p class="muted">完全一致率越高越好；MAE 越低越好；线性加权 κ 扣除了按两边评分分布产生的偶然一致。内容维无法判定时，该维有效样本数可能少于总数。</p>'
        '<div class="table-wrap"><table><thead><tr><th>维度</th><th>有效数</th><th>完全一致率</th><th>MAE</th><th>加权 κ</th></tr></thead>'
        f'<tbody>{"".join(dimension_rows)}</tbody></table></div>'
        f'<div class="matrix-list">{"".join(dimension_matrices)}</div></section>'
        '<section class="panel"><h2>旧综合分规则对照</h2>'
        f'<p>旧规则的 1–5 分完全一致率 <strong>{_percentage(composite.get("exact"))}</strong>；'
        f'MAE <strong>{_decimal(composite.get("mae"))}</strong>。其“人工复核”保留在下表，供检查新二分类改变了哪些处理决定。</p>'
        f'{legacy_matrix}</section>'
        '<section class="panel"><h2>关键误判样例</h2>'
        f'<p class="muted">共 {len(issues)} 条 CRITIC 判定分歧或相对 good / bad 标签的错判，下面最多展示 10 条，优先展示坏题误放行。</p>'
        f'<div class="error-list">{error_html}</div></section>'
        '<section class="panel"><h2>全部候选明细</h2>'
        '<p class="muted">五维顺序：语言自然 / 表述清晰 / 内部逻辑 / 原意忠实 / 原答案匹配。CRITIC 总分范围为 0–1；“—”表示有内容维无法判定，因语言或清晰度为 1 分仍已拒绝。</p>'
        '<div class="table-wrap"><table class="cases-table"><thead><tr>'
        '<th>ID</th><th>参考五维</th><th>LLM 五维</th><th>参考 CRITIC</th><th>LLM CRITIC</th>'
        '<th>旧规则参考／LLM</th><th>参考标签</th><th>原文</th>'
        f'</tr></thead><tbody>{all_rows}</tbody></table></div></section>'
    )


def render_report(report: dict) -> str:
    """将评测结果或待审核状态渲染成可离线打开的单文件 HTML。"""
    status = report.get("status")
    if status not in {"ready", "pending"}:
        raise ValueError("report.status 必须是 ready 或 pending")
    title = report.get("title") or "第三任务：五维与 CRITIC 判定评测报告"
    dataset_label = report.get("dataset_label") or "已审核样本"
    state_label = f"{dataset_label}评测结果" if status == "ready" else "待审核预览"
    contents = _ready(report) if status == "ready" else _pending(report)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root {{ color-scheme: light; --ink:#1b2638; --muted:#617086; --line:#dce3ec; --paper:#fff; --bg:#f4f7fb; --accent:#245ea6; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:var(--bg); font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
.page {{ max-width:1180px; margin:auto; padding:36px 22px 60px; }}
.eyebrow {{ color:var(--accent); font-size:12px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }}
h1 {{ font-size:32px; line-height:1.25; margin:10px 0 12px; }}
h2 {{ font-size:20px; margin:0 0 14px; }}
p {{ margin:10px 0; }}
.intro {{ color:var(--muted); max-width:840px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:7px 20px; margin:20px 0 28px; color:var(--muted); font-size:13px; }}
.meta span {{ padding-right:20px; border-right:1px solid var(--line); }}
.meta span:last-child {{ border:0; }}
.panel,.metric-card {{ background:var(--paper); border:1px solid var(--line); border-radius:14px; box-shadow:0 2px 10px rgba(30,60,100,.035); }}
.panel {{ margin:18px 0; padding:25px 28px; }}
.pending-panel {{ border-left:5px solid #ca8d2e; }}
.metric-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; }}
.metric-card {{ padding:18px 20px; }}
.metric-label {{ color:var(--muted); font-size:13px; }}
.metric-value {{ font-size:29px; font-weight:750; line-height:1.3; margin:6px 0; }}
.metric-detail {{ color:var(--muted); font-size:12px; }}
.muted {{ color:var(--muted); }}
.table-wrap,.matrix-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; text-align:left; }}
th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
thead th {{ color:#32445d; background:#eef3f9; font-size:13px; white-space:nowrap; }}
tbody th {{ font-weight:600; }}
tbody tr:last-child td,tbody tr:last-child th {{ border-bottom:0; }}
.matrix-list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:11px; margin-top:20px; }}
.matrix-detail {{ padding:12px 15px; background:#f9fbfe; border:1px solid var(--line); border-radius:9px; }}
summary {{ cursor:pointer; color:var(--accent); font-weight:600; }}
.matrix {{ margin-top:12px; text-align:center; }}
.matrix caption {{ text-align:left; color:var(--muted); font-size:12px; padding-bottom:5px; }}
.matrix th,.matrix td {{ text-align:center; font-size:13px; }}
.diagonal {{ background:#e7f3ec; font-weight:700; }}
.weights-table {{ max-width:430px; }}
.safety-line {{ margin-top:18px; }}
.pill {{ display:inline-block; margin-left:4px; padding:1px 8px; border-radius:999px; font-size:12px; white-space:nowrap; }}
.pass {{ color:#17613c; background:#e5f4eb; }}
.review {{ color:#79510f; background:#fff2d8; }}
.reject {{ color:#9c3c3b; background:#fbe8e7; }}
.neutral {{ color:#506074; background:#ebeff4; }}
.error-list {{ display:grid; gap:11px; }}
.error-case {{ border:1px solid var(--line); border-radius:10px; padding:13px 16px; background:#fcfdff; }}
.error-case p {{ margin:6px 0; }}
.error-heading,.comparison {{ display:flex; flex-wrap:wrap; gap:9px 22px; align-items:center; }}
.error-tag {{ color:#a13e36; background:#fcebe9; border-radius:4px; padding:1px 6px; font-size:12px; }}
.comparison {{ color:var(--muted); font-size:13px; margin:7px 0; }}
.cases-table {{ min-width:1150px; font-size:13px; }}
.cases-table .mismatch {{ background:#fffaf3; }}
.cases-table details {{ min-width:90px; }}
.case-text {{ min-width:260px; max-width:700px; padding:10px 0; white-space:normal; }}
.case-text p {{ overflow-wrap:anywhere; }}
.reasons {{ display:grid; grid-template-columns:90px 1fr; gap:2px 9px; margin:10px 0 0; }}
.reasons dt {{ color:var(--muted); }}
.reasons dd {{ margin:0; }}
.mono {{ font-family:Consolas,"SFMono-Regular",monospace; white-space:nowrap; }}
footer {{ margin-top:25px; color:var(--muted); font-size:12px; }}
@media(max-width:850px) {{ .metric-grid,.matrix-list {{ grid-template-columns:1fr 1fr; }} }}
@media(max-width:600px) {{ .page {{ padding:24px 14px 40px; }} h1 {{ font-size:25px; }} .panel {{ padding:18px; }} .metric-grid,.matrix-list {{ grid-template-columns:1fr; }} }}
@media print {{ body {{ background:white; }} .page {{ max-width:none; padding:0; }} .panel,.metric-card {{ box-shadow:none; break-inside:avoid; }} summary {{ color:var(--ink); }} }}
</style>
</head>
<body>
<main class="page">
<div class="eyebrow">MOS QUERY EVALUATION · {_e(state_label)}</div>
<h1>{_e(title)}</h1>
<p class="intro">{_e(report.get("note"))}</p>
<div class="meta">
<span>生成时间：{_e(report.get("generated_at") or "—")}</span>
<span>模型：{_e(report.get("model") or "尚未运行")}</span>
<span>五维标准：{_e(report.get("rubric_version") or "—")}</span>
<span>判定规则：CRITIC＋1 分否决</span>
<span>审核文件：{_e(report.get("input_name") or "—")}</span>
</div>
{contents}
<footer>本报告按本项目规则比较已确认参考评分与 LLM 盲评分数；CRITIC 总分和旧综合分均不能称为标准 MOS。报告结论仅适用于本次输入样本。</footer>
</main>
</body>
</html>
'''
