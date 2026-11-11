from itertools import product

import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.decision import decide, summarize_results
from rubric.mtqc.orchestrator import empty_judge


def results_for(scores):
    return {name: {**empty_judge(name, "ERROR" if score is None else "COMPLETED", "reason"), "score": score,
                   "details": {"failed_factors": []} if name == JUDGES[0] and score is not None else {}}
            for name, score in zip(JUDGES, scores)}


@pytest.mark.parametrize("scores,decision", [
    ([3, 3, 3, 3], "PASS"), ([3, 3, 2, 2], "REPAIR"), ([3, 2, 3, 3], "REVIEW"),
    ([3, 3, 2, 3], "REPAIR"), ([3, 3, 3, 2], "REPAIR"),
    ([3, 1, 3, 3], "REJECT"), ([1, 3, 3, 3], "REPAIR"), ([1, None, None, None], "REPAIR"),
    ([3, 1, None, None], "REJECT"), ([3, 3, None, 3], "REVIEW"),
])
def test_decisions(scores, decision):
    assert decide(results_for(scores)) == decision


def test_strict_thresholds_and_unknown_failed_factors():
    assert decide(results_for([3, 3, 2, 2]), 3, 3) == "REPAIR"
    with pytest.raises(ValueError, match="thresholds must be 3"):
        decide(results_for([3, 3, 2, 2]), 2, 2)
    result = summarize_results(results_for([None, 3, 3, 3]), {"min_coherence_score": 3, "min_persona_score": 3})
    assert result["failed_factors"] is None
    assert result["evaluation_status"] == "ERROR"


def test_incomplete_evaluation_never_passes():
    for scores in product([None, 1, 3], [None, 1, 2, 3], [None, 1, 2, 3], [None, 1, 2, 3]):
        if None in scores:
            assert decide(results_for(scores)) != "PASS"
