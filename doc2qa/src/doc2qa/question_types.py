"""Content labels for individual questions, independent of wording style."""

from typing import Literal

QuestionType = Literal["factual", "numeric_fact", "list", "conditional", "comparison"]

QUESTION_TYPES = {
    "factual": "A directly stated fact, such as an identity, location, date, explanation or documented procedure; use when no more specific label fits.",
    "numeric_fact": "A quantity, count, amount, rate, duration or simple evidence-supported calculation, including numeric values found in tables.",
    "list": "An enumeration of multiple requested items, names, steps or categories, rather than a single fact or an actual comparison.",
    "conditional": "A fact or rule whose applicability depends on a substantive stated condition, eligibility criterion or case; routine source attribution alone is not a condition.",
    "comparison": "An explicit comparison of two or more entities, options, periods or cases, not merely asking for several values.",
}

_TYPE_EXAMPLES = {
    "factual": "Which Cairo Metro station should I use for the Egyptian Museum?",
    "numeric_fact": "How much is a monthly Cairo LRT pass for seven stations?",
    "list": "Where can I buy train tickets from Cairo?",
    "conditional": "Can passengers aged 70 and over get a free Cairo Metro subscription?",
    "comparison": "Is a first-class train ticket from Cairo to Alexandria more expensive than second class?",
}


def question_type_rules() -> str:
    sections = [
        "## Question_types\n"
        "- Consider ALL configured types for EACH Context and produce a useful, varied mix supported by its evidence.\n"
        "- Counts may be unequal and unsupported types may be absent; do not force equal quotas or invent facts to cover a type.\n"
        "- These are information-need labels, not grammatical formats. Vary who/what/when/where, why/how, yes/no questions and directives where evidence permits."
    ]
    sections.extend(
        f"### {name}\n- {description}\n- Example query: {_TYPE_EXAMPLES[name]}"
        for name, description in QUESTION_TYPES.items()
    )
    return "\n\n".join(sections)
