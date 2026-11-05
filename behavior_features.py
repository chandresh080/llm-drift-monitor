"""Behavioral feature extraction from raw response text.

These features are rule based signals about what a response *does*, not what
it means: refusals, repetition, formatting, emptiness. They deliberately stay
separate from the semantic embeddings used by the MMD test, because a response
can be semantically different without being worse.

The refusal patterns are a documented baseline, not a production classifier:
phrases like "I cannot help with that" can appear inside a quoted explanation.
Any production refusal detector should be validated against a hand labelled
sample before its output is trusted.
"""

import re
from typing import Any, Dict, List

REFUSAL_PATTERNS = [
    r"\bi (?:cannot|can't|can not) help with\b",
    r"\bi (?:cannot|can't|can not) (?:assist|provide|share)\b",
    r"\bi'm (?:unable|not able) to\b",
    r"\bi (?:won't|will not)\b",
    r"\bagainst my (?:principles|guidelines|policies)\b",
    r"\bi must (?:decline|refuse)\b",
]

_WORD = re.compile(r"\b\w+\b", re.UNICODE)


def extract_behavior_features(text: str) -> Dict[str, Any]:
    """Extract rule based behavioral features from one response."""
    text = text or ""
    tokens = _WORD.findall(text.lower())
    unique = len(set(tokens))
    repetition_ratio = round(1.0 - unique / len(tokens), 4) if tokens else 0.0
    lowered = text.lower()
    return {
        "token_count": len(tokens),
        "is_empty": int(not text.strip()),
        "is_refusal": int(any(re.search(p, lowered) for p in REFUSAL_PATTERNS)),
        "has_code_block": int("```" in text),
        "repetition_ratio": repetition_ratio,
    }


def is_valid_json(text: str) -> bool:
    """Whether a response parses as JSON (for prompts that expect JSON)."""
    import json
    try:
        json.loads(text or "")
        return True
    except (ValueError, TypeError):
        return False


def refusal_rate(flags: List[int]) -> float:
    """Share of responses flagged as refusals; input is a list of 0/1."""
    return sum(flags) / len(flags) if flags else 0.0
