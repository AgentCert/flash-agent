"""
Episode Store – Persistent JSONL backend with in-memory fallback
==================================================================

Two backends share the ``MemoryStore`` interface:

  ``FileMemoryStore``    — append-only JSONL at ``cfg.memory_path``.
                           File-locked appends. In-memory index built at load.
  ``InMemoryStore``      — fallback when ``memory_path`` is empty.

Both apply ``cfg.memory_ttl_days`` on read so stale episodes are filtered
without rewriting the file. Schema version on every record; mismatches log
a warning and are skipped (compaction is a separate operation).

Per-fingerprint episode cap is enforced on read — the store returns the most
recent N entries per fingerprint, not the full history.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from config import AgentConfig
from memory.episode import EPISODE_SCHEMA_VERSION, MitigationEpisode

logger = logging.getLogger("flash-agent")

# Per-fingerprint cap — keep at most this many recent episodes per fingerprint
# when answering ``find_by_fingerprint``. Protects against runaway storage.
MAX_EPISODES_PER_FINGERPRINT = 20

# Cross-thread file lock — single writer at a time within this process.
_FILE_LOCK = threading.RLock()


class MemoryStore(Protocol):
    """The interface every backend implements."""

    def append(self, episode: MitigationEpisode) -> None: ...

    def update(self, episode: MitigationEpisode) -> None: ...

    def find_pending(self, scope_key: str) -> List[MitigationEpisode]: ...

    def find_by_fingerprint(self, fp: str, limit: int = 10) -> List[MitigationEpisode]: ...

    def all_for_scope(self, scope_key: str) -> List[MitigationEpisode]: ...

    def put_fingerprints(
        self,
        scope_key: str,
        scan_id: str,
        fps: List[str],
        issues: Optional[List[Dict[str, Any]]] = None,
    ) -> None: ...

    def get_latest_fingerprints(self, scope_key: str) -> List[str]: ...

    def get_latest_issues(self, scope_key: str) -> List[Dict[str, Any]]: ...


def _within_ttl(episode: MitigationEpisode, ttl_seconds: float, now: float) -> bool:
    if ttl_seconds <= 0:
        return True  # disabled
    return (now - episode.ts) <= ttl_seconds


class InMemoryStore:
    """Fallback when ``memory_path`` is empty. Same interface as ``FileMemoryStore``."""

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._episodes: List[MitigationEpisode] = []
        # ``_fp_by_scope[scope_key]`` → list of (scan_id, [fps]) — most-recent last.
        self._fp_by_scope: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    @property
    def _ttl_seconds(self) -> float:
        return max(0, int(self.cfg.memory_ttl_days)) * 86400

    def append(self, episode: MitigationEpisode) -> None:
        with self._lock:
            self._episodes.append(episode)

    def update(self, episode: MitigationEpisode) -> None:
        with self._lock:
            # Find by (scan_id, tool, ts) — episodes are de-facto unique by ts.
            for i, ep in enumerate(self._episodes):
                if (
                    ep.scan_id == episode.scan_id
                    and ep.tool == episode.tool
                    and abs(ep.ts - episode.ts) < 1e-6
                ):
                    self._episodes[i] = episode
                    return
            # If not found, append (idempotent update semantics).
            self._episodes.append(episode)

    def find_pending(self, scope_key: str) -> List[MitigationEpisode]:
        now = time.time()
        with self._lock:
            return [
                ep
                for ep in self._episodes
                if ep.scope_key == scope_key
                and ep.outcome == "pending"
                and _within_ttl(ep, self._ttl_seconds, now)
            ]

    def find_by_fingerprint(self, fp: str, limit: int = 10) -> List[MitigationEpisode]:
        now = time.time()
        cap = min(limit, MAX_EPISODES_PER_FINGERPRINT)
        with self._lock:
            matches = [
                ep
                for ep in self._episodes
                if ep.symptom_fingerprint == fp
                and _within_ttl(ep, self._ttl_seconds, now)
            ]
            return matches[-cap:]

    def all_for_scope(self, scope_key: str) -> List[MitigationEpisode]:
        now = time.time()
        with self._lock:
            return [
                ep
                for ep in self._episodes
                if ep.scope_key == scope_key and _within_ttl(ep, self._ttl_seconds, now)
            ]

    def put_fingerprints(
        self,
        scope_key: str,
        scan_id: str,
        fps: List[str],
        issues: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        with self._lock:
            bucket = self._fp_by_scope.setdefault(scope_key, [])
            bucket.append(
                {
                    "scan_id": scan_id,
                    "fps": list(fps),
                    "issues": list(issues or []),
                    "ts": time.time(),
                }
            )
            if len(bucket) > 20:
                self._fp_by_scope[scope_key] = bucket[-20:]

    def get_latest_fingerprints(self, scope_key: str) -> List[str]:
        with self._lock:
            bucket = self._fp_by_scope.get(scope_key, [])
            return list(bucket[-1]["fps"]) if bucket else []

    def get_latest_issues(self, scope_key: str) -> List[Dict[str, Any]]:
        with self._lock:
            bucket = self._fp_by_scope.get(scope_key, [])
            return list(bucket[-1].get("issues", [])) if bucket else []


class FileMemoryStore:
    """
    Append-only JSONL store. Episodes and fingerprint snapshots share the file
    via a ``record_type`` discriminator.
    """

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self.path = cfg.memory_path
        parent = Path(self.path).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Memory store dir %s could not be created: %s — using in-memory",
                parent,
                exc,
            )
            self._fallback = InMemoryStore(cfg)
            self._use_fallback = True
            return
        self._use_fallback = False
        self._fallback = None  # type: ignore[assignment]
        # In-memory index for fast queries; rebuilt on init.
        self._episodes: List[MitigationEpisode] = []
        self._fp_by_scope: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    @property
    def _ttl_seconds(self) -> float:
        return max(0, int(self.cfg.memory_ttl_days)) * 86400

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with _FILE_LOCK:
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError as exc:
                            logger.warning("Skipping malformed memory line: %s", exc)
                            continue
                        rtype = rec.get("record_type", "episode")
                        if rtype == "episode":
                            self._index_episode(rec)
                        elif rtype == "fingerprint_snapshot":
                            self._index_fingerprint_snapshot(rec)
                        else:
                            logger.debug("Unknown memory record_type=%r — skipped", rtype)
        except OSError as exc:
            logger.warning("Memory load failed: %s — starting empty", exc)

    def _index_episode(self, rec: Dict[str, Any]) -> None:
        version = int(rec.get("schema_version", 0))
        if version != EPISODE_SCHEMA_VERSION:
            logger.warning(
                "Skipping episode with schema_version=%d (current=%d)",
                version,
                EPISODE_SCHEMA_VERSION,
            )
            return
        try:
            ep = MitigationEpisode.from_dict(rec)
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping malformed episode: %s", exc)
            return
        # Apply update-semantics: if there's an existing episode with the same
        # (scan_id, tool, ts), the *later* record wins (reconciliation update).
        for i, existing in enumerate(self._episodes):
            if (
                existing.scan_id == ep.scan_id
                and existing.tool == ep.tool
                and abs(existing.ts - ep.ts) < 1e-6
            ):
                self._episodes[i] = ep
                return
        self._episodes.append(ep)

    def _index_fingerprint_snapshot(self, rec: Dict[str, Any]) -> None:
        scope_key = rec.get("scope_key", "")
        bucket = self._fp_by_scope.setdefault(scope_key, [])
        bucket.append(
            {
                "scan_id": rec.get("scan_id", ""),
                "fps": list(rec.get("fps", []) or []),
                "issues": list(rec.get("issues", []) or []),
                "ts": float(rec.get("ts", time.time())),
            }
        )

    def _append_line(self, line: str) -> None:
        with _FILE_LOCK:
            # O_APPEND is atomic for writes < PIPE_BUF on POSIX, so a single
            # JSONL line write is safe even across processes.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)

    # ── Public API ───────────────────────────────────────────────────────────

    def append(self, episode: MitigationEpisode) -> None:
        if self._use_fallback:
            return self._fallback.append(episode)
        rec = {"record_type": "episode", **episode.to_dict()}
        try:
            self._append_line(json.dumps(rec, default=str, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Episode persist failed: %s", exc)
        # Update index regardless of disk success — losing one record to a
        # transient disk error shouldn't break in-memory queries this scan.
        self._episodes.append(episode)

    def update(self, episode: MitigationEpisode) -> None:
        """Append a new record for the updated episode — append-only semantics."""
        if self._use_fallback:
            return self._fallback.update(episode)
        rec = {"record_type": "episode", **episode.to_dict()}
        try:
            self._append_line(json.dumps(rec, default=str, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Episode update persist failed: %s", exc)
        # Update in-memory index.
        for i, ep in enumerate(self._episodes):
            if (
                ep.scan_id == episode.scan_id
                and ep.tool == episode.tool
                and abs(ep.ts - episode.ts) < 1e-6
            ):
                self._episodes[i] = episode
                return
        self._episodes.append(episode)

    def find_pending(self, scope_key: str) -> List[MitigationEpisode]:
        if self._use_fallback:
            return self._fallback.find_pending(scope_key)
        now = time.time()
        return [
            ep
            for ep in self._episodes
            if ep.scope_key == scope_key
            and ep.outcome == "pending"
            and _within_ttl(ep, self._ttl_seconds, now)
        ]

    def find_by_fingerprint(self, fp: str, limit: int = 10) -> List[MitigationEpisode]:
        if self._use_fallback:
            return self._fallback.find_by_fingerprint(fp, limit)
        now = time.time()
        cap = min(limit, MAX_EPISODES_PER_FINGERPRINT)
        matches = [
            ep
            for ep in self._episodes
            if ep.symptom_fingerprint == fp and _within_ttl(ep, self._ttl_seconds, now)
        ]
        return matches[-cap:]

    def all_for_scope(self, scope_key: str) -> List[MitigationEpisode]:
        if self._use_fallback:
            return self._fallback.all_for_scope(scope_key)
        now = time.time()
        return [
            ep
            for ep in self._episodes
            if ep.scope_key == scope_key and _within_ttl(ep, self._ttl_seconds, now)
        ]

    def put_fingerprints(
        self,
        scope_key: str,
        scan_id: str,
        fps: List[str],
        issues: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if self._use_fallback:
            return self._fallback.put_fingerprints(scope_key, scan_id, fps, issues)
        rec = {
            "record_type": "fingerprint_snapshot",
            "scope_key": scope_key,
            "scan_id": scan_id,
            "fps": list(fps),
            "issues": list(issues or []),
            "ts": time.time(),
        }
        try:
            self._append_line(json.dumps(rec, default=str, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Fingerprint snapshot persist failed: %s", exc)
        bucket = self._fp_by_scope.setdefault(scope_key, [])
        bucket.append(
            {
                "scan_id": scan_id,
                "fps": list(fps),
                "issues": list(issues or []),
                "ts": rec["ts"],
            }
        )
        if len(bucket) > 20:
            self._fp_by_scope[scope_key] = bucket[-20:]

    def get_latest_fingerprints(self, scope_key: str) -> List[str]:
        if self._use_fallback:
            return self._fallback.get_latest_fingerprints(scope_key)
        bucket = self._fp_by_scope.get(scope_key, [])
        return list(bucket[-1]["fps"]) if bucket else []

    def get_latest_issues(self, scope_key: str) -> List[Dict[str, Any]]:
        if self._use_fallback:
            return self._fallback.get_latest_issues(scope_key)
        bucket = self._fp_by_scope.get(scope_key, [])
        return list(bucket[-1].get("issues", [])) if bucket else []


def memory_store_from_config(cfg: AgentConfig) -> MemoryStore:
    """Factory — picks the right backend based on ``cfg.memory_path``."""
    if cfg.memory_path:
        return FileMemoryStore(cfg)  # type: ignore[return-value]
    return InMemoryStore(cfg)  # type: ignore[return-value]
