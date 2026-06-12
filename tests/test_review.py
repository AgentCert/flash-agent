"""Tests for the hard action reviewer (Phase 4) — fully mocked LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List

import pytest

from llm.review import HardActionReviewer


@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Response:
    choices: List[_Choice]


class StubChatCompletions:
    """Returns canned responses in order; raises after exhaustion if asked."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = responses
        self.calls: List[dict] = []

    def create(self, **kwargs):  # noqa: D401, ANN003
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("no more canned responses")
        content = self._responses.pop(0)
        if isinstance(content, BaseException):
            raise content
        return _Response(choices=[_Choice(message=_Msg(content=content))])


class StubClient:
    def __init__(self, responses: List[str]) -> None:
        self.chat = type("Chat", (), {"completions": StubChatCompletions(responses)})()


def _tool() -> dict:
    return {"name": "scale_dep", "description": "Scale a deployment", "inputSchema": {}}


# ── happy path: both pass ───────────────────────────────────────────────────


def test_both_passes_approve_with_evidence(mitigate_cfg) -> None:
    reviewer = HardActionReviewer(mitigate_cfg)
    reviewer.set_client(
        StubClient(
            [
                json.dumps(
                    {
                        "verdict": "APPROVED",
                        "reasoning": "ok",
                        "evidence_used": ["pod foo restart_count=15"],
                    }
                ),
                json.dumps(
                    {
                        "verdict": "APPROVED",
                        "reasoning": "no worse-case scenario in trace",
                        "evidence_used": ["resource snapshot shows capacity"],
                    }
                ),
            ]
        )
    )
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={"namespace": "alpha", "replicas": 3},
        tool_def=_tool(),
        symptom_context="HPA stalled",
        prior_evidence="pod foo CPU=95%",
    )
    assert len(verdicts) == 2
    assert all(v.verdict == "APPROVED" for v in verdicts)
    assert verdicts[0].framing == "justify"
    assert verdicts[1].framing == "challenge"


# ── forced downgrade: justify approved but no evidence ──────────────────────


def test_justify_without_evidence_forced_downgrade(mitigate_cfg) -> None:
    reviewer = HardActionReviewer(mitigate_cfg)
    reviewer.set_client(
        StubClient(
            [
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "looks fine", "evidence_used": []}
                ),
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["x"]}
                ),
            ]
        )
    )
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={},
        tool_def=_tool(),
        symptom_context="",
        prior_evidence="",
    )
    # First pass was APPROVED with empty evidence_used → forcibly BLOCKED →
    # short-circuit means only one verdict.
    assert verdicts[0].verdict == "BLOCKED"
    assert "FORCED DOWNGRADE" in verdicts[0].reasoning


# ── challenge BLOCKED short-circuits after justify approves ────────────────


def test_challenge_blocked_short_circuits(mitigate_cfg) -> None:
    reviewer = HardActionReviewer(mitigate_cfg)
    reviewer.set_client(
        StubClient(
            [
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["x"]}
                ),
                json.dumps(
                    {"verdict": "BLOCKED", "reasoning": "could worsen state", "evidence_used": []}
                ),
            ]
        )
    )
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={},
        tool_def=_tool(),
        symptom_context="",
        prior_evidence="evidence here",
    )
    assert verdicts[0].verdict == "APPROVED"
    assert verdicts[1].verdict == "BLOCKED"


# ── reviewer LLM error → BLOCKED (fail closed) ──────────────────────────────


def test_reviewer_error_fails_closed(mitigate_cfg) -> None:
    reviewer = HardActionReviewer(mitigate_cfg)
    reviewer.set_client(StubClient([RuntimeError("boom")]))  # type: ignore[list-item]
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={},
        tool_def=_tool(),
        symptom_context="",
        prior_evidence="",
    )
    assert verdicts[0].verdict == "BLOCKED"
    assert "reviewer error" in verdicts[0].reasoning


# ── degraded mode is recorded ────────────────────────────────────────────────


def test_degraded_mode_records_flag(base_cfg) -> None:
    base_cfg.reviewer_model_alias = ""  # empty → degraded
    base_cfg.agent_mode = "mitigate"
    reviewer = HardActionReviewer(base_cfg)
    reviewer.set_client(
        StubClient(
            [
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["x"]}
                ),
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["y"]}
                ),
            ]
        )
    )
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={},
        tool_def=_tool(),
        symptom_context="",
        prior_evidence="",
    )
    assert all(v.degraded for v in verdicts)
    assert all(v.model == base_cfg.model_alias for v in verdicts)


# ── verdict model recorded in primary mode ──────────────────────────────────


def test_primary_mode_records_reviewer_model(mitigate_cfg) -> None:
    reviewer = HardActionReviewer(mitigate_cfg)
    reviewer.set_client(
        StubClient(
            [
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["x"]}
                ),
                json.dumps(
                    {"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["y"]}
                ),
            ]
        )
    )
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={},
        tool_def=_tool(),
        symptom_context="",
        prior_evidence="",
    )
    assert all(v.model == "reviewer-model" for v in verdicts)
    assert not any(v.degraded for v in verdicts)


# ── JSON in code fences is parsed ───────────────────────────────────────────


def test_json_in_code_fences_parsed(mitigate_cfg) -> None:
    reviewer = HardActionReviewer(mitigate_cfg)
    payload = '```json\n{"verdict":"APPROVED","reasoning":"ok","evidence_used":["x"]}\n```'
    reviewer.set_client(
        StubClient(
            [
                payload,
                payload,
            ]
        )
    )
    verdicts = reviewer.review_twice(
        tool_name="scale_dep",
        tool_args={},
        tool_def=_tool(),
        symptom_context="",
        prior_evidence="",
    )
    assert all(v.verdict == "APPROVED" for v in verdicts)
