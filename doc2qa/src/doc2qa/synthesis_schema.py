"""Local generation responses retain a required type on every query."""

from .question_types import QuestionType
from .schema import Record


class SyntheticData(Record):
    input: str
    qa_type: QuestionType
    used_source_files: list[str] | None = None


class SyntheticDataList(Record):
    data: list[SyntheticData]
