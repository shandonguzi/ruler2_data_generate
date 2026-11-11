"""Multi-turn QA quality control, independent of the legacy five-dimensional rubric."""

JUDGES = (
    "factor_compliance", "final_qa_validation", "dialogue_coherence", "persona_naturalness",
)
SCHEMA_VERSION = "mtqc-records-v1"
