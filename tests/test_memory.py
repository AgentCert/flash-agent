"""Tests for the persistent episode store (Phase 5)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from memory.episode import EPISODE_SCHEMA_VERSION, MitigationEpisode
from memory.store import (
    FileMemoryStore,
    InMemoryStore,
    memory_store_from_config,
)


def _ep(
    *,
    scan_id: str = "scan-1",
    scope_key: str = "namespace:alpha",
    tool: str = "patch_dep",
    fp: str | None = None,
    ts: float | None = None,
    outcome: str = "pending",
) -> MitigationEpisode:
    return MitigationEpisode(
        scan_id=scan_id,
        ts=ts or time.time(),
        scope_key=scope_key,
        tool=tool,
        args={"namespace": "alpha"},
        action_class="hard",
        symptom_fingerprint=fp,
        normalized_component="api",
        outcome=outcome,  # type: ignore[arg-type]
    )


# ── factory ─────────────────────────────────────────────────────────────────


def test_factory_picks_file_store(base_cfg) -> None:
    store = memory_store_from_config(base_cfg)
    assert isinstance(store, FileMemoryStore)


def test_factory_picks_in_memory_when_path_empty(base_cfg) -> None:
    base_cfg.memory_path = ""
    store = memory_store_from_config(base_cfg)
    assert isinstance(store, InMemoryStore)


# ── round-trip ──────────────────────────────────────────────────────────────


def test_file_store_roundtrip(base_cfg) -> None:
    store = FileMemoryStore(base_cfg)
    ep = _ep()
    store.append(ep)

    # Reopen — should rehydrate the episode.
    store2 = FileMemoryStore(base_cfg)
    found = store2.find_pending("namespace:alpha")
    assert len(found) == 1
    assert found[0].tool == "patch_dep"


# ── update writes new record, keeps in-memory consistent ────────────────────


def test_file_store_update(base_cfg) -> None:
    store = FileMemoryStore(base_cfg)
    ep = _ep()
    store.append(ep)
    ep.outcome = "succeeded"  # type: ignore[assignment]
    store.update(ep)

    pending = store.find_pending("namespace:alpha")
    assert pending == []

    # Reopen and verify the *latest* outcome wins (append-only resolution).
    store2 = FileMemoryStore(base_cfg)
    all_eps = store2.all_for_scope("namespace:alpha")
    assert len(all_eps) == 1
    assert all_eps[0].outcome == "succeeded"


# ── TTL filtering ────────────────────────────────────────────────────────────


def test_ttl_filters_old_records(base_cfg) -> None:
    base_cfg.memory_ttl_days = 1
    store = FileMemoryStore(base_cfg)
    old_ts = time.time() - (2 * 86400)
    ep = _ep(ts=old_ts)
    store.append(ep)

    fresh = _ep(scan_id="scan-2", ts=time.time())
    store.append(fresh)

    pending = store.find_pending("namespace:alpha")
    # Only the fresh one (within TTL) should be visible.
    assert len(pending) == 1
    assert pending[0].scan_id == "scan-2"


# ── schema version mismatch is skipped ───────────────────────────────────────


def test_schema_version_mismatch_skipped(base_cfg) -> None:
    path = Path(base_cfg.memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = {
        "record_type": "episode",
        "scan_id": "scan-old",
        "ts": time.time(),
        "scope_key": "namespace:alpha",
        "tool": "x",
        "args": {},
        "action_class": "hard",
        "outcome": "pending",
        "schema_version": EPISODE_SCHEMA_VERSION + 100,
    }
    path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    store = FileMemoryStore(base_cfg)
    assert store.find_pending("namespace:alpha") == []


# ── put_fingerprints / get_latest_fingerprints / get_latest_issues ──────────


def test_fingerprint_snapshot_persistence(base_cfg) -> None:
    store = FileMemoryStore(base_cfg)
    issues = [{"category": "crashloop", "component": "api", "severity": "critical"}]
    store.put_fingerprints("namespace:alpha", "scan-1", ["fpA"], issues=issues)

    # Reopen → snapshot persists.
    store2 = FileMemoryStore(base_cfg)
    assert store2.get_latest_fingerprints("namespace:alpha") == ["fpA"]
    fetched_issues = store2.get_latest_issues("namespace:alpha")
    assert fetched_issues[0]["component"] == "api"


# ── per-fingerprint cap ─────────────────────────────────────────────────────


def test_find_by_fingerprint_respects_cap(base_cfg) -> None:
    store = FileMemoryStore(base_cfg)
    for i in range(25):
        store.append(_ep(scan_id=f"scan-{i}", fp="fpX", ts=time.time() + i))
    found = store.find_by_fingerprint("fpX", limit=50)
    # Default cap is 20.
    assert len(found) == 20


# ── concurrent appends don't corrupt records ────────────────────────────────


def test_concurrent_appends(base_cfg) -> None:
    store = FileMemoryStore(base_cfg)

    def writer(start: int) -> None:
        for i in range(20):
            store.append(_ep(scan_id=f"t{start}-{i}", ts=time.time() + i))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Reload from disk — verify every line is valid JSON.
    text = Path(base_cfg.memory_path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip():
            json.loads(line)  # raises on corruption

    store2 = FileMemoryStore(base_cfg)
    assert len(store2.find_pending("namespace:alpha")) == 80


# ── in-memory store mirrors API ─────────────────────────────────────────────


def test_in_memory_store_api(base_cfg) -> None:
    base_cfg.memory_path = ""
    store = InMemoryStore(base_cfg)
    ep = _ep()
    store.append(ep)
    assert len(store.find_pending("namespace:alpha")) == 1
    store.put_fingerprints("namespace:alpha", "scan-1", ["fpA"], issues=[{"x": 1}])
    assert store.get_latest_fingerprints("namespace:alpha") == ["fpA"]
    assert store.get_latest_issues("namespace:alpha") == [{"x": 1}]
