# examples/live/polymarket/sentinel_signal_models.py
from __future__ import annotations
import dataclasses

VALID_DIRECTIONS = frozenset({"YES", "NO"})
VALID_CATEGORIES = frozenset({"geopolitical", "financial", "election", "conflict", "sports", "other"})

def validate_direction(direction: str) -> str:
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {VALID_DIRECTIONS}, got {direction!r}")
    return direction

def validate_relevance_score(score: float) -> float:
    s = float(score)
    if not (0.0 <= s <= 1.0):
        raise ValueError(f"relevance_score must be between 0.0 and 1.0, got {s}")
    return s

@dataclasses.dataclass(frozen=True, slots=True)
class SentinelNewsSignal:
    event: str
    story_id: str
    headline: str
    category: str
    market_slug: str
    market_question: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    instrument_id: str
    direction: str
    relevance_score: float
    market_end_date_iso: str
    ts_ns: int

    def __post_init__(self) -> None:
        validate_direction(self.direction)
        validate_relevance_score(self.relevance_score)

    def to_jsonl_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_jsonl_dict(cls, d: dict) -> "SentinelNewsSignal":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})
