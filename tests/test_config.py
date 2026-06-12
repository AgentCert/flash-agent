"""Tests for AgentConfig validation (Phase 0)."""

from __future__ import annotations

import os

import pytest

from config import (
    DEFAULT_PATTERNS_HARD,
    DEFAULT_PATTERNS_SOFT,
    DEFAULT_PATTERNS_VIOLENT,
    AgentConfig,
)


def test_default_mode_is_observe(monkeypatch) -> None:
    for k in [
        "AGENT_MODE",
        "AGENT_SCOPE_NAMESPACE",
        "MITIGATION_AUDIT_PATH",
        "MITIGATION_ALLOW_DISCOVERED_SCOPE",
        "REVIEWER_MODEL_ALIAS",
        "AGENT_MEMORY_PATH",
        "ACTION_PATTERNS_SOFT",
        "ACTION_PATTERNS_HARD",
        "ACTION_PATTERNS_VIOLENT",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost")
    monkeypatch.setenv("MODEL_ALIAS", "m")
    monkeypatch.setenv("MCP_URLS", "http://localhost:8086/mcp")
    cfg = AgentConfig.from_env()
    assert cfg.agent_mode == "observe"
    assert cfg.action_patterns_soft == DEFAULT_PATTERNS_SOFT
    assert cfg.action_patterns_hard == DEFAULT_PATTERNS_HARD
    assert cfg.action_patterns_violent == DEFAULT_PATTERNS_VIOLENT


def test_mitigate_requires_scope_or_ack(base_cfg) -> None:
    base_cfg.agent_mode = "mitigate"
    base_cfg.scope_override = ""
    base_cfg.allow_discovered_scope = False
    errors = base_cfg.validate()
    assert any("AGENT_MODE=mitigate" in e for e in errors)


def test_mitigate_passes_with_explicit_scope(base_cfg) -> None:
    base_cfg.agent_mode = "mitigate"
    base_cfg.scope_override = "myns"
    base_cfg.allow_discovered_scope = False
    assert base_cfg.validate() == []


def test_mitigate_passes_with_ack(base_cfg) -> None:
    base_cfg.agent_mode = "mitigate"
    base_cfg.scope_override = ""
    base_cfg.allow_discovered_scope = True
    assert base_cfg.validate() == []


def test_pattern_overrides_parse(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost")
    monkeypatch.setenv("MODEL_ALIAS", "m")
    monkeypatch.setenv("MCP_URLS", "http://localhost:8086/mcp")
    monkeypatch.setenv("ACTION_PATTERNS_SOFT", r"\bcustom_read\b,\bcustom_get\b")
    cfg = AgentConfig.from_env()
    assert cfg.action_patterns_soft == [r"\bcustom_read\b", r"\bcustom_get\b"]
