"""Tests for the playbook builder (Phase 7)."""

from __future__ import annotations

import time

from llm.playbook import PlaybookBuilder
from memory.episode import MitigationEpisode
from memory.fingerprint import fingerprint_issue, fingerprint_tool_call
from memory.store import InMemoryStore


SCOPE = "namespace:alpha"


def _store(base_cfg) -> InMemoryStore:
    base_cfg.memory_path = ""
    return InMemoryStore(base_cfg)


def _ep(*, tool: str, fp: str, outcome: str, scan_id: str = "s") -> MitigationEpisode:
    return MitigationEpisode(
        scan_id=scan_id,
        ts=time.time(),
        scope_key=SCOPE,
        tool=tool,
        args={"namespace": "alpha"},
        action_class="hard",
        symptom_fingerprint=fp,
        normalized_component="api",
        outcome=outcome,  # type: ignore[arg-type]
    )


def test_summarize_for_returns_empty_when_no_history(base_cfg) -> None:
    pb = PlaybookBuilder(_store(base_cfg))
    assert pb.summarize_for("missing-fp") == ""


def test_summarize_for_groups_and_counts(base_cfg) -> None:
    store = _store(base_cfg)
    fp = "abc123"
    store.append(_ep(tool="scale_dep", fp=fp, outcome="succeeded"))
    store.append(_ep(tool="scale_dep", fp=fp, outcome="succeeded"))
    store.append(_ep(tool="scale_dep", fp=fp, outcome="ineffective"))
    pb = PlaybookBuilder(store)
    text = pb.summarize_for(fp)
    assert "scale_dep" in text
    assert "2/3" in text
    assert "ineffective=1" in text


def test_render_prompt_block_includes_section(base_cfg) -> None:
    store = _store(base_cfg)
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    fp = fingerprint_issue(issue, SCOPE)
    store.append(_ep(tool="restart_pod", fp=fp, outcome="succeeded"))
    pb = PlaybookBuilder(store)
    block = pb.render_prompt_block(SCOPE, [issue])
    assert "Playbook" in block
    assert "crashloop" in block
    assert "restart_pod" in block


def test_render_prompt_block_empty_when_no_match(base_cfg) -> None:
    store = _store(base_cfg)
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    pb = PlaybookBuilder(store)
    assert pb.render_prompt_block(SCOPE, [issue]) == ""


def test_summarize_for_review_finds_tool_call_history(base_cfg) -> None:
    store = _store(base_cfg)
    args = {"namespace": "alpha", "pod": "api-7f8d"}
    # Episode argues for same shape (different pod hash) — should still match.
    store.append(
        MitigationEpisode(
            scan_id="s1",
            ts=time.time(),
            scope_key=SCOPE,
            tool="restart_pod",
            args={"namespace": "alpha", "pod": "api-9a2c"},
            action_class="hard",
            symptom_fingerprint=None,
            normalized_component="api",
            outcome="ineffective",
        )
    )
    pb = PlaybookBuilder(store)
    out = pb.summarize_for_review("restart_pod", args, SCOPE)
    assert "Prior outcomes" in out
    assert "ineffective" in out
