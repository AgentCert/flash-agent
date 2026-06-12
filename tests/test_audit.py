"""Tests for the audit log writer (Phase 0)."""

from __future__ import annotations

import json
from pathlib import Path

from policy.audit import AuditLog


def test_audit_disabled_when_path_empty(tmp_path) -> None:
    a = AuditLog("")
    assert a.enabled is False
    a.write("x", {"y": 1})  # no-op, no error


def test_audit_writes_jsonl(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    a = AuditLog(str(path))
    a.write("event_a", {"k": 1})
    a.write("event_b", {"k": 2})
    lines = path.read_text(encoding="utf-8").splitlines()
    rec_a = json.loads(lines[0])
    rec_b = json.loads(lines[1])
    assert rec_a["event"] == "event_a"
    assert rec_a["k"] == 1
    assert rec_b["event"] == "event_b"
    assert "ts" in rec_a


def test_audit_creates_parent_dir(tmp_path) -> None:
    path = tmp_path / "nested" / "deep" / "audit.jsonl"
    a = AuditLog(str(path))
    assert a.enabled is True
    a.write("e", {})
    assert path.exists()
