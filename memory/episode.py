"""
Mitigation Episode – Persistent action record
==============================================

One ``MitigationEpisode`` per executed mutating action. The schema is fixed
here in Phase 3 even though the persistent store wires in Phase 5 — keeping
the shape in one place means the store, the reconciler, and the playbook all
agree on field names from day one.

Outcome lifecycle:

  pending     → action executed, observation window not yet elapsed
  succeeded   → target fingerprint absent from a later scan's issue set
  ineffective → target fingerprint still present after the observation window
  regressed   → target fingerprint absent BUT a new high-severity fingerprint
                appeared on the same component
  ambiguous   → same component, different-category fingerprint (e.g.
                crashloop → readiness-fail). Both fingerprints stored in
                ``outcome_evidence`` for downstream weighting.
  blocked-by-review → set by the Phase 4 reviewer when execution was denied.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

# Schema version is incremented when fields are renamed / removed. Used by the
# store to drop records produced by an incompatible older agent.
EPISODE_SCHEMA_VERSION = 1

Outcome = Literal[
    "pending",
    "succeeded",
    "ineffective",
    "regressed",
    "ambiguous",
    "blocked-by-review",
]


@dataclass
class ReviewVerdict:
    """One review pass's verdict — both passes share this shape."""

    verdict: Literal["APPROVED", "BLOCKED"] = "BLOCKED"
    reasoning: str = ""
    evidence_used: List[str] = field(default_factory=list)
    iteration: int = 0
    framing: Literal["justify", "challenge"] = "justify"
    model: str = ""
    # When True, the reviewer ran in degraded mode (reviewer_model_alias empty,
    # fell back to model_alias). Recorded so audit can distinguish.
    degraded: bool = False


@dataclass
class MitigationEpisode:
    """One executed mitigation action and its lifecycle."""

    scan_id: str
    ts: float  # epoch seconds; record creation time
    scope_key: str  # e.g. "namespace:foo" or "cluster:" — disambiguates scope
    tool: str
    args: Dict[str, Any]
    action_class: str  # "soft" | "hard"
    symptom_fingerprint: Optional[str] = None
    normalized_component: Optional[str] = None  # target component (post-normalization)
    review_iterations: List[Dict[str, Any]] = field(default_factory=list)
    min_observe_until_ts: float = 0.0
    outcome: Outcome = "pending"
    outcome_evidence: Optional[Dict[str, Any]] = None
    mcp_url: str = ""
    schema_version: int = EPISODE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_jsonl_line(self) -> str:
        return json.dumps(self.to_dict(), default=str, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MitigationEpisode":
        # Drop any unknown fields so older / newer schemas don't crash __init__.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known}
        # Coerce review_iterations to plain dicts (already serialised).
        return cls(**clean)
