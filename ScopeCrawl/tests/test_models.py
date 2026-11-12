from __future__ import annotations

from typing import get_args

from scopecrawl.models import STAGE_LABELS, FailureStage


def test_stage_labels_match_failure_stages():
    assert set(STAGE_LABELS) == set(get_args(FailureStage))
