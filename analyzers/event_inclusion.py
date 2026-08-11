from __future__ import annotations

from typing import Any


CLASSICAL_CATEGORY_ORDER = (
    "orchestral",
    "chamber_or_recital",
    "opera_or_operetta",
    "ballet_or_classical_dance",
    "choral_or_sacred",
    "contemporary_art_music",
    "soundtrack_game_or_crossover",
    "musical_with_classical_substance",
    "education_rehearsal_or_competition",
)
NONCLASSICAL_CATEGORY_ORDER = (
    "nonclassical_music",
    "commercial_musical_theatre",
    "theatre_dance_or_spoken_word",
    "nonperformance",
    "recording_only",
    "other",
)
NOT_EVENT_CATEGORY_ORDER = (
    "season_or_overview",
    "membership_course_or_rehearsal",
    "service_or_addon",
    "invalid_occurrence",
    "other_non_event",
)
UNCERTAIN_CATEGORY = "unclear"

INCLUSION_DECISIONS = ("classical", "nonclassical", "not_event", "uncertain")
CLASSICAL_CATEGORIES = frozenset(CLASSICAL_CATEGORY_ORDER)
NONCLASSICAL_CATEGORIES = frozenset(NONCLASSICAL_CATEGORY_ORDER)
NOT_EVENT_CATEGORIES = frozenset(NOT_EVENT_CATEGORY_ORDER)
ALL_CATEGORIES = [
    *CLASSICAL_CATEGORY_ORDER,
    *NONCLASSICAL_CATEGORY_ORDER,
    *NOT_EVENT_CATEGORY_ORDER,
    UNCERTAIN_CATEGORY,
]


def validate_decision_category(decision: str, category: str) -> None:
    compatible = {
        "classical": CLASSICAL_CATEGORIES,
        "nonclassical": NONCLASSICAL_CATEGORIES,
        "not_event": NOT_EVENT_CATEGORIES,
        "uncertain": frozenset((UNCERTAIN_CATEGORY,)),
    }
    if decision not in compatible:
        raise ValueError(f"unsupported inclusion decision {decision!r}")
    if category not in compatible[decision]:
        raise ValueError(
            f"{decision} decision has incompatible category {category!r}"
        )


def assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": list(INCLUSION_DECISIONS)},
            "category": {"type": "string", "enum": ALL_CATEGORIES},
            "rationale": {"type": "string"},
            "evidence_urls": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["decision", "category", "rationale", "evidence_urls"],
    }
