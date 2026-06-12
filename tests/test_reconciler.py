"""Tests for outcome reconciliation (Phase 6)."""

from __future__ import annotations

import time

from memory.episode import MitigationEpisode
from memory.fingerprint import fingerprint_issue
from memory.reconciler import reconcile_pending_episodes
from memory.store import InMemoryStore


SCOPE = "namespace:alpha"


def _store(base_cfg) -> InMemoryStore:
    base_cfg.memory_path = ""
    return InMemoryStore(base_cfg)


def _ep(
    base_cfg,
    *,
    component: str,
    fp: str | None,
    ts: float | None = None,
    window: float = 60.0,
    tool: str = "restart_pod",
) -> MitigationEpisode:
    ts = ts if ts is not None else time.time() - 200  # always past observation window
    return MitigationEpisode(
        scan_id="scan-prev",
        ts=ts,
        scope_key=SCOPE,
        tool=tool,
        args={"namespace": "alpha", "pod": component},
        action_class="hard",
        normalized_component=component,
        symptom_fingerprint=fp,
        min_observe_until_ts=ts + window,
        outcome="pending",
    )


# ── window not elapsed ──────────────────────────────────────────────────────


def test_window_not_elapsed_keeps_pending(base_cfg) -> None:
    store = _store(base_cfg)
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    fp = fingerprint_issue(issue, SCOPE)
    now = time.time()
    ep = _ep(base_cfg, component="api", fp=fp, ts=now, window=600)
    store.append(ep)

    # `now` is just before min_observe_until_ts.
    results = reconcile_pending_episodes(store, [issue], SCOPE, now=now)
    assert len(results) == 1
    assert results[0].transition == "pending"
    assert results[0].rule == "window-not-elapsed"


# ── target gone → succeeded ─────────────────────────────────────────────────


def test_succeeded_when_target_fp_absent(base_cfg) -> None:
    store = _store(base_cfg)
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    fp = fingerprint_issue(issue, SCOPE)
    ep = _ep(base_cfg, component="api", fp=fp)
    store.append(ep)

    results = reconcile_pending_episodes(store, [], SCOPE, now=time.time())
    assert results[0].transition == "succeeded"


# ── target still present → ineffective ──────────────────────────────────────


def test_ineffective_when_target_fp_remains(base_cfg) -> None:
    store = _store(base_cfg)
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    fp = fingerprint_issue(issue, SCOPE)
    ep = _ep(base_cfg, component="api", fp=fp)
    store.append(ep)

    results = reconcile_pending_episodes(store, [issue], SCOPE, now=time.time())
    assert results[0].transition == "ineffective"


# ── regressed: new high-severity on same component ──────────────────────────


def test_regressed_when_new_high_severity_on_same_component(base_cfg) -> None:
    store = _store(base_cfg)
    target_issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    target_fp = fingerprint_issue(target_issue, SCOPE)
    ep = _ep(base_cfg, component="api", fp=target_fp)
    store.append(ep)

    new_issue = {"category": "oom-killed", "component": "api", "severity": "critical"}
    results = reconcile_pending_episodes(store, [new_issue], SCOPE, now=time.time())
    assert results[0].transition == "regressed"
    assert results[0].episode.outcome_evidence is not None


# ── ambiguous: same component, different category, lower severity ───────────


def test_ambiguous_same_component_different_category(base_cfg) -> None:
    store = _store(base_cfg)
    target_issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    target_fp = fingerprint_issue(target_issue, SCOPE)
    ep = _ep(base_cfg, component="api", fp=target_fp)
    store.append(ep)

    # Different category, INFO severity → ambiguous (not regressed).
    new_issue = {"category": "readiness-fail", "component": "api", "severity": "info"}
    results = reconcile_pending_episodes(store, [new_issue], SCOPE, now=time.time())
    assert results[0].transition == "ambiguous"


# ── unrelated component issue NOT attributed ────────────────────────────────


def test_unrelated_component_issue_not_attributed(base_cfg) -> None:
    store = _store(base_cfg)
    target_issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    target_fp = fingerprint_issue(target_issue, SCOPE)
    ep = _ep(base_cfg, component="api", fp=target_fp)
    store.append(ep)

    # New critical issue on DIFFERENT component.
    new_issue = {"category": "crashloop", "component": "db", "severity": "critical"}
    results = reconcile_pending_episodes(store, [new_issue], SCOPE, now=time.time())
    # Target fp gone, same-component is clean → succeeded.
    assert results[0].transition == "succeeded"


# ── second-scan workflow (window-elapsed transition) ────────────────────────


def test_two_scan_workflow_window_elapsed(base_cfg) -> None:
    store = _store(base_cfg)
    issue = {"category": "crashloop", "component": "api", "severity": "critical"}
    fp = fingerprint_issue(issue, SCOPE)
    now = time.time()
    # Episode created at now-30, 60s window → not elapsed yet at now+30.
    ep = _ep(base_cfg, component="api", fp=fp, ts=now - 30, window=60)
    store.append(ep)

    # First reconcile at now → window NOT elapsed.
    r1 = reconcile_pending_episodes(store, [issue], SCOPE, now=now)
    assert r1[0].transition == "pending"

    # Second reconcile much later → window elapsed.
    r2 = reconcile_pending_episodes(store, [], SCOPE, now=now + 120)
    assert r2[0].transition == "succeeded"
