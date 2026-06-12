"""
Reconciler – Outcome attribution for pending episodes
========================================================

At the start of each scan, the reconciler reads the *previous* scan's
fingerprint snapshot for this scope and decides whether each pending episode
has succeeded, failed, regressed, or remains pending.

Three signals:

  1. Window-elapsed?  ``now < episode.min_observe_until_ts`` → leave pending.
                      The action hasn't had time to take effect.

  2. Target component fingerprint stability across scans:
     - target fingerprint absent          → succeeded
     - target fingerprint still present   → ineffective
     - target absent, NEW high-severity fingerprint on the *same* normalized
       component → regressed
     - target absent, different-category fingerprint on the *same* component
       → ambiguous (crashloop → readiness-fail style)

  3. Unrelated components: a new fingerprint on a *different* component is
     NOT attributed to this episode. Causal attribution is scoped.

Outcomes are persisted back through ``store.update(...)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from memory.episode import MitigationEpisode
from memory.fingerprint import fingerprint_issue, normalize_component
from memory.store import MemoryStore

logger = logging.getLogger("flash-agent")

# Severity weighting used when deciding ``regressed``.
_HIGH_SEVERITIES = {"critical", "warning"}


@dataclass
class ReconcileResult:
    """Per-episode result returned to the caller."""

    episode: MitigationEpisode
    transition: Literal["pending", "succeeded", "ineffective", "regressed", "ambiguous"]
    rule: str


def _index_issues_by_component(
    issues: List[Dict[str, Any]],
    scope_key: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Group issues by normalized component name.

    Returns ``{normalized_component: [issue, ...]}``. Issues lacking a
    component are bucketed under the empty string and ignored by attribution.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        comp = normalize_component(str(issue.get("component") or "")) or ""
        out.setdefault(comp, []).append(issue)
    return out


def _issue_category(issue: Dict[str, Any]) -> str:
    return (issue.get("category") or "").strip().lower()


def reconcile_pending_episodes(
    store: MemoryStore,
    current_issues: List[Dict[str, Any]],
    scope_key: str,
    now: float,
) -> List[ReconcileResult]:
    """
    For each pending episode in ``scope_key``, compute its outcome against
    ``current_issues``.

    The store is updated in place for each non-pending transition; pending
    episodes are left untouched and re-evaluated on a future scan.
    """
    pending = store.find_pending(scope_key)
    if not pending:
        return []

    by_component = _index_issues_by_component(current_issues, scope_key)
    results: List[ReconcileResult] = []

    for episode in pending:
        if now < episode.min_observe_until_ts:
            # Window not elapsed — leave as pending.
            results.append(
                ReconcileResult(
                    episode=episode, transition="pending", rule="window-not-elapsed"
                )
            )
            continue

        component = episode.normalized_component or ""
        if not component:
            # No target component → can only check by symptom_fingerprint
            # presence in the latest scan's fingerprint set.
            results.append(_reconcile_by_fingerprint_only(episode, current_issues, scope_key, store))
            continue

        same_comp_issues = by_component.get(component, [])

        if episode.symptom_fingerprint:
            # Targeted fingerprint set — look it up among same-component issues.
            target_fp = episode.symptom_fingerprint
            same_fp_present = any(
                fingerprint_issue(i, scope_key) == target_fp for i in same_comp_issues
            )
            if same_fp_present:
                results.append(_finalize(store, episode, "ineffective", "target-fp-still-present"))
                continue

            # Target fingerprint gone. Check for regression / ambiguity on same component.
            if not same_comp_issues:
                results.append(_finalize(store, episode, "succeeded", "target-fp-absent-no-residual"))
                continue

            # There ARE other issues on the same component. Classify them.
            high_sev = [i for i in same_comp_issues if (i.get("severity") or "").lower() in _HIGH_SEVERITIES]
            if high_sev:
                results.append(
                    _finalize(
                        store,
                        episode,
                        "regressed",
                        "target-fp-absent-new-high-severity-on-same-component",
                        evidence={
                            "new_issues": [
                                {
                                    "severity": i.get("severity"),
                                    "category": _issue_category(i),
                                    "summary": i.get("summary"),
                                }
                                for i in high_sev[:5]
                            ],
                        },
                    )
                )
                continue

            # Different category, lower severity on the same component → ambiguous.
            results.append(
                _finalize(
                    store,
                    episode,
                    "ambiguous",
                    "target-fp-absent-different-category-on-same-component",
                    evidence={
                        "new_issues": [
                            {
                                "severity": i.get("severity"),
                                "category": _issue_category(i),
                                "summary": i.get("summary"),
                            }
                            for i in same_comp_issues[:5]
                        ],
                    },
                )
            )
            continue

        # No symptom_fingerprint on the episode — fall back to component-only check.
        if not same_comp_issues:
            results.append(_finalize(store, episode, "succeeded", "no-issues-on-component"))
        else:
            high_sev = [i for i in same_comp_issues if (i.get("severity") or "").lower() in _HIGH_SEVERITIES]
            if high_sev:
                results.append(
                    _finalize(store, episode, "regressed", "high-severity-issue-remains-on-component")
                )
            else:
                results.append(
                    _finalize(store, episode, "ambiguous", "non-critical-issue-remains-on-component")
                )

    return results


def _reconcile_by_fingerprint_only(
    episode: MitigationEpisode,
    current_issues: List[Dict[str, Any]],
    scope_key: str,
    store: MemoryStore,
) -> ReconcileResult:
    if not episode.symptom_fingerprint:
        # No component, no fingerprint — best we can do is mark succeeded if
        # the current scan has no high-severity issues.
        any_high = any(
            (i.get("severity") or "").lower() in _HIGH_SEVERITIES for i in current_issues
        )
        if any_high:
            return _finalize(store, episode, "ineffective", "no-target-still-high-severity-issues")
        return _finalize(store, episode, "succeeded", "no-target-no-high-severity-issues")

    fps = {fingerprint_issue(i, scope_key) for i in current_issues}
    if episode.symptom_fingerprint in fps:
        return _finalize(store, episode, "ineffective", "target-fp-still-present-no-component")
    return _finalize(store, episode, "succeeded", "target-fp-absent-no-component")


def _finalize(
    store: MemoryStore,
    episode: MitigationEpisode,
    transition: Literal["succeeded", "ineffective", "regressed", "ambiguous"],
    rule: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> ReconcileResult:
    """Persist the transition and return the result tuple."""
    episode.outcome = transition  # type: ignore[assignment]
    if evidence is not None:
        episode.outcome_evidence = evidence
    try:
        store.update(episode)
    except Exception as exc:
        logger.warning("Reconciler store.update failed: %s", exc)
    logger.info(
        "Reconciled episode tool=%s comp=%s → %s (rule=%s)",
        episode.tool,
        episode.normalized_component,
        transition,
        rule,
    )
    return ReconcileResult(episode=episode, transition=transition, rule=rule)
