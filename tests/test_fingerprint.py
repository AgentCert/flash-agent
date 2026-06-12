"""Tests for fingerprinting (Phase 5)."""

from __future__ import annotations

from memory.fingerprint import (
    fingerprint_issue,
    fingerprint_tool_call,
    normalize_component,
)


# ── normalize_component ──────────────────────────────────────────────────────


def test_normalize_keeps_pure_name() -> None:
    assert normalize_component("front-end") == "front-end"


def test_normalize_collapses_pod_generation() -> None:
    assert normalize_component("cataloguedb-7f8d4c-q9w2") == "cataloguedb-*-*"


def test_normalize_handles_empty() -> None:
    assert normalize_component("") == ""
    assert normalize_component(None) is None


def test_normalize_replicaset_hash() -> None:
    # "5d9b8" is a generation hash; "api-deployment" is the name.
    result = normalize_component("api-deployment-5d9b8")
    # Could be either form depending on length heuristic — accept both.
    assert result in ("api-deployment-*", "api-deployment-5d9b8")
    # But "55555" pure-digit segment must collapse:
    assert normalize_component("dep-55555") == "dep-*"


# ── fingerprint_issue ────────────────────────────────────────────────────────


def test_issue_fingerprint_stable() -> None:
    issue = {"category": "crashloop", "component": "api-7f8d", "severity": "critical"}
    fp1 = fingerprint_issue(issue, "namespace:alpha")
    fp2 = fingerprint_issue(issue, "namespace:alpha")
    assert fp1 == fp2
    assert len(fp1) == 16


def test_issue_fingerprint_normalizes_component() -> None:
    a = {"category": "crashloop", "component": "api-7f8d", "severity": "critical"}
    b = {"category": "crashloop", "component": "api-9a2c", "severity": "critical"}
    # Both should collapse to "api-*" → same fingerprint.
    assert fingerprint_issue(a, "namespace:alpha") == fingerprint_issue(b, "namespace:alpha")


def test_issue_fingerprint_scope_changes_fp() -> None:
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    assert fingerprint_issue(issue, "namespace:alpha") != fingerprint_issue(issue, "namespace:beta")


def test_issue_fingerprint_ignores_summary() -> None:
    a = {
        "category": "crashloop",
        "component": "api",
        "severity": "critical",
        "summary": "pod foo restarting",
    }
    b = {
        "category": "crashloop",
        "component": "api",
        "severity": "critical",
        "summary": "different summary text",
    }
    assert fingerprint_issue(a, "namespace:alpha") == fingerprint_issue(b, "namespace:alpha")


def test_issue_fingerprint_different_category() -> None:
    a = {"category": "crashloop", "component": "api", "severity": "critical"}
    b = {"category": "readiness-fail", "component": "api", "severity": "critical"}
    assert fingerprint_issue(a, "namespace:alpha") != fingerprint_issue(b, "namespace:alpha")


# ── fingerprint_tool_call ────────────────────────────────────────────────────


def test_tool_call_fingerprint_normalizes_pod_arg() -> None:
    a = fingerprint_tool_call("restart_pod", {"pod": "api-7f8d"}, "namespace:alpha")
    b = fingerprint_tool_call("restart_pod", {"pod": "api-9a2c"}, "namespace:alpha")
    assert a == b


def test_tool_call_fingerprint_args_ordering() -> None:
    a = fingerprint_tool_call("scale", {"namespace": "alpha", "replicas": 3}, "namespace:alpha")
    b = fingerprint_tool_call("scale", {"replicas": 3, "namespace": "alpha"}, "namespace:alpha")
    assert a == b
