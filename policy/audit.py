"""
Audit Log – Append-only JSONL writer
======================================

Every classification, gate decision, episode transition, and reviewer verdict
goes through this writer. Append-only with file lock so multiple in-process
threads / future shared instances don't interleave records.

Graceful no-op if the configured path is empty — the agent runs identically
without an audit log, only the persistent trace is absent.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("flash-agent")

# Module-level lock — one per process. Cross-process locking is out of scope
# (single-pod agent today); the file is opened with O_APPEND so concurrent
# writers from different processes still get atomic appends on POSIX.
_FILE_LOCK = threading.Lock()


class AuditLog:
    """Append-only JSONL audit log writer."""

    def __init__(self, path: Optional[str]) -> None:
        self.path: Optional[str] = (path or "").strip() or None
        if self.path:
            parent = Path(self.path).parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "Audit log dir %s could not be created: %s — audit disabled",
                    parent,
                    exc,
                )
                self.path = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, event: str, payload: Dict[str, Any]) -> None:
        """Append a single event record. Failure is logged at warning, never raised."""
        if not self.enabled:
            return

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        try:
            with _FILE_LOCK:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            logger.warning("Audit write failed for event=%s: %s", event, exc)

    @contextmanager
    def timing(self, event: str, payload: Dict[str, Any]) -> Iterator[None]:
        """Time an operation and emit its duration into the audit record."""
        start = time.time()
        try:
            yield
        finally:
            self.write(event, {**payload, "duration_sec": round(time.time() - start, 4)})


def _atomic_append(path: str, line: str) -> None:
    """POSIX atomic append helper — used by tests, kept internal."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
