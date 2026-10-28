#!/usr/bin/env python3
"""Domain rules shared by validation, training and reporting.

Standard library only. Nothing in this module estimates probabilities.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MIN_QUOTE_CHARS = 8


@dataclass(frozen=True)
class FeatureSpec:
    """A pre-race feature the skill is allowed to learn from."""

    name: str
    unit: str
    source: str
    definition: str


SPECS = [
    FeatureSpec(
        "intent_centered", "score -1..1", "computed",
        "Shrunk supporter-minus-opponent evidence score; see intent-model.md.",
    ),
    FeatureSpec("rating", "HK rating", "card.rating", "Official handicap rating carried by the card."),
    FeatureSpec("weight_lb", "lb", "card.weight_lb", "Declared carried weight."),
    FeatureSpec("draw", "stall number", "card.draw", "Barrier number; widen with venue/distance bias before use."),
    FeatureSpec("career_place_rate", "rate 0-1", "analyst", "Career places / career starts, with sample size."),
    FeatureSpec("cd_place_rate", "rate 0-1", "analyst", "Course-and-distance places / starts, with sample size."),
    FeatureSpec("recent_finish_mean", "places", "analyst", "Mean finishing position over the last N starts."),
    FeatureSpec("days_since_run", "days", "analyst", "Days since the previous start."),
    FeatureSpec("speed_rating", "lengths or index", "analyst", "Going/class/pace-adjusted speed figure, same scale every race."),
    FeatureSpec("jockey_win_rate", "rate 0-1", "analyst", "Jockey win rate over a stated window, with sample size."),
    FeatureSpec("trainer_win_rate", "rate 0-1", "analyst", "Trainer win rate over a stated window, with sample size."),
]

FEATURE_REGISTRY: dict[str, FeatureSpec] = {spec.name: spec for spec in SPECS}
AUTO_FEATURES = frozenset({"intent_centered"})


def registered(name: str) -> bool:
    return name in FEATURE_REGISTRY


def describe(names: list[str]) -> dict[str, dict[str, str]]:
    return {
        name: {
            "unit": FEATURE_REGISTRY[name].unit,
            "source": FEATURE_REGISTRY[name].source,
            "definition": FEATURE_REGISTRY[name].definition,
        }
        for name in names
        if name in FEATURE_REGISTRY
    }


def derive_place_slots(declared_starters: int) -> int:
    """Return model place slots using HKJC local-race declared-starter terms.

    HKJC pays two places for 4-6 declared starters and three for 7 or more.
    Below four, one slot is a model-only fallback, not an official Place pool.
    """
    if declared_starters >= 7:
        return 3
    if declared_starters >= 4:
        return 2
    return 1


_QUOTE_NOISE = re.compile(
    r"[\s\u3000，。、；：！？「」『』（）()\[\]【】,.;:!?'\"“”‘’\-—_/·]+"
)


def normalise_text(value: str) -> str:
    """Fold width, case and punctuation so quotes survive copy/paste noise."""
    folded = unicodedata.normalize("NFKC", str(value)).casefold()
    return _QUOTE_NOISE.sub("", folded)


def quote_supported(quote: str, excerpt: str) -> bool:
    """True when the quoted words really appear in the stored source excerpt."""
    needle = normalise_text(quote)
    haystack = normalise_text(excerpt)
    return len(needle) >= MIN_QUOTE_CHARS and needle in haystack
