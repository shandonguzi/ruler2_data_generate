"""五维 CRITIC 权重与试验性二分类规则。"""

from __future__ import annotations

from math import sqrt


# 由 40 条开发集参考评分估计后冻结；顺序与 evaluate.DIMENSIONS 一致。
DIMENSION_KEYS = ("language", "clarity", "logic", "fidelity", "answer")
FROZEN_WEIGHTS = (
    0.2147945637,
    0.1184326548,
    0.1802983689,
    0.2646037728,
    0.2218706398,
)
THRESHOLD = 0.8


def fit_weights(vectors: list[list[int]]) -> tuple[float, ...]:
    """按 BHI 附录 D 的 CRITIC 公式从参考五维评分复算权重。

    本项目五维均为 1–3 分，使用固定量表端点 (score-1)/2 归一化。
    正式评测只使用上面的冻结权重，不从待评测样本重新拟合。
    """
    n = len(vectors)
    if n < 2:
        raise ValueError("CRITIC 估权至少需要 2 条参考评分")
    if any(len(row) != 5 or any(type(score) is not int or score not in (1, 2, 3) for score in row) for row in vectors):
        raise ValueError("CRITIC 估权要求每条参考评分恰好有五个 1–3 整数")

    columns = [[(row[j] - 1) / 2 for row in vectors] for j in range(5)]
    means = [sum(column) / n for column in columns]
    deviations = [[value - means[j] for value in column] for j, column in enumerate(columns)]
    standard_deviations = [sqrt(sum(value * value for value in column) / (n - 1)) for column in deviations]
    if any(value == 0 for value in standard_deviations):
        raise ValueError("至少一个维度的参考评分没有变化，Pearson 相关系数无法计算")

    information: list[float] = []
    for j in range(5):
        conflict = 0.0
        for k in range(5):
            covariance = sum(a * b for a, b in zip(deviations[j], deviations[k])) / (n - 1)
            correlation = covariance / (standard_deviations[j] * standard_deviations[k])
            conflict += 1 - min(1.0, max(-1.0, correlation))
        information.append(standard_deviations[j] * conflict)

    total_information = sum(information)
    if total_information <= 0:
        raise ValueError("五维没有足够的区分信息，无法计算 CRITIC 权重")
    return tuple(value / total_information for value in information)


def score_and_decide(scores: list[int | None]) -> tuple[float | None, str]:
    """返回 0–1 加权总分和二分类决定；任一维 1 分优先拒绝。"""
    if len(scores) != 5:
        raise ValueError("五维分数数量必须为 5")
    if any(score is not None and (type(score) is not int or score not in (1, 2, 3)) for score in scores):
        raise ValueError("五维分数必须为 1、2、3 或允许的无法判定")
    if scores[0] is None or scores[1] is None:
        raise ValueError("语言与清晰度必须为 1–3")
    if any(score is None for score in scores) and scores[0] != 1 and scores[1] != 1:
        raise ValueError("只有语言或清晰度为 1 时，内容维才允许无法判定")

    if any(score is None for score in scores):
        return None, "拒绝"
    total = sum(weight * (score - 1) / 2 for weight, score in zip(FROZEN_WEIGHTS, scores))
    if 1 in scores:
        return total, "拒绝"
    return total, "通过" if total >= THRESHOLD else "拒绝"
