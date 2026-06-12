"""
Playbook Builder – Distilled prior-outcome evidence
=====================================================

Rolls up the episode store by *symptom fingerprint* into compact human
readable evidence strings that get injected into:

  1. The main agent's system prompt (so it sees "last time you tried X for
     this symptom shape, it failed"); and

  2. The hard-action reviewer's ``prior_evidence`` field (so the reviewer
     can weight historical success against the current proposal).

Compaction rules:
  - Group episodes by ``(tool, action_class)``.
  - For each group: success rate, last outcome, last evidence snippet.
  - Trim to ``max_episodes`` per fingerprint to bound prompt size.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from memory.episode import MitigationEpisode
from memory.fingerprint import fingerprint_issue, fingerprint_tool_call
from memory.store import MemoryStore

logger = logging.getLogger("flash-agent")


class PlaybookBuilder:
    """Builds compact prior-evidence summaries from the episode store."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def summarize_for(
        self,
        symptom_fp: str,
        max_episodes: int = 10,
    ) -> str:
        """
        Roll up prior episodes for ``symptom_fp`` into a compact bullet list.

        Returns an empty string if there is no relevant evidence.
        """
        episodes = self.store.find_by_fingerprint(symptom_fp, limit=max_episodes)
        if not episodes:
            return ""

        grouped: Dict[str, List[MitigationEpisode]] = defaultdict(list)
        for ep in episodes:
            key = f"{ep.tool}|{ep.action_class}"
            grouped[key].append(ep)

        lines: List[str] = []
        for key, group in grouped.items():
            tool, action_class = key.split("|", 1)
            total = len(group)
            succeeded = sum(1 for g in group if g.outcome == "succeeded")
            ineffective = sum(1 for g in group if g.outcome == "ineffective")
            regressed = sum(1 for g in group if g.outcome == "regressed")
            ambiguous = sum(1 for g in group if g.outcome == "ambiguous")
            blocked = sum(1 for g in group if g.outcome == "blocked-by-review")
            success_rate = (succeeded / total) if total else 0.0
            last_ep = group[-1]
            lines.append(
                f"- tool={tool} ({action_class}): "
                f"{succeeded}/{total} succeeded ({success_rate:.0%}), "
                f"ineffective={ineffective}, regressed={regressed}, "
                f"ambiguous={ambiguous}, blocked_by_review={blocked}. "
                f"Last outcome: {last_ep.outcome}"
            )

        return "\n".join(lines)

    def summarize_for_review(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        scope_key: str,
        max_episodes: int = 10,
    ) -> str:
        """
        Build the ``prior_evidence`` string for the hard-action reviewer.

        Filters episodes by *tool-call fingerprint* (so the reviewer sees the
        history of THIS specific call shape, not unrelated tool history).
        """
        target_fp = fingerprint_tool_call(tool_name, tool_args, scope_key)
        all_eps = self.store.all_for_scope(scope_key)
        matches = [
            ep
            for ep in all_eps
            if fingerprint_tool_call(ep.tool, ep.args, ep.scope_key) == target_fp
        ]
        if not matches:
            return ""
        matches = matches[-max_episodes:]
        lines: List[str] = ["## Prior outcomes for this same action shape:"]
        for ep in matches:
            evidence = ""
            if ep.outcome_evidence:
                ev = ep.outcome_evidence
                if isinstance(ev, dict) and "new_issues" in ev:
                    evidence = f" — new_issues={ev['new_issues'][:2]}"
            lines.append(
                f"- scan={ep.scan_id} outcome={ep.outcome}{evidence}"
            )
        return "\n".join(lines)

    def render_prompt_block(
        self,
        scope_key: str,
        current_issues: List[Dict[str, Any]],
        max_per_issue: int = 5,
    ) -> str:
        """
        Build the playbook prompt block injected into the main agent's system
        prompt. One section per current issue, keyed by its fingerprint.

        Returns empty string if no current issues have matching prior evidence.
        """
        if not current_issues:
            return ""

        sections: List[str] = []
        seen_fps: set[str] = set()
        for issue in current_issues:
            fp = fingerprint_issue(issue, scope_key)
            if fp in seen_fps:
                continue
            seen_fps.add(fp)
            roll_up = self.summarize_for(fp, max_episodes=max_per_issue)
            if not roll_up:
                continue
            component = issue.get("component", "?")
            category = issue.get("category", "?")
            sections.append(
                f"### {category} on {component} (fp={fp[:8]})\n{roll_up}"
            )

        if not sections:
            return ""

        return (
            "## Playbook (Prior Mitigation Outcomes)\n"
            "For some of the issues you may detect, prior scans have already tried "
            "mitigations. Use this evidence to weight your choices — prefer actions "
            "with high prior success rates; avoid actions that previously caused "
            "regression. If your proposed action contradicts the recorded outcome, "
            "you must explicitly justify why this case is different.\n\n"
            + "\n\n".join(sections)
        )
