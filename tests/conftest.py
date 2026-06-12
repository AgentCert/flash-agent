"""Shared test fixtures and sys.path bootstrap."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the flash-agent root is on sys.path so `import config`, `import policy`
# etc. work without an installable package.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

from config import AgentConfig


@pytest.fixture
def base_cfg(tmp_path) -> AgentConfig:
    """A minimally-valid AgentConfig with audit/memory pointed at tmp_path."""
    return AgentConfig(
        agent_name="test-agent",
        openai_base_url="http://localhost:9999/v1",
        openai_api_key="not-needed",
        model_alias="proposer-model",
        azure_api_version="2025-04-01-preview",
        mcp_urls=["http://localhost:8086/mcp"],
        mcp_timeout=5,
        scan_query="test scan",
        scope_override="testns",
        agent_mode="observe",
        allow_discovered_scope=True,
        mitigation_review_iters=2,
        mitigation_audit_path=str(tmp_path / "audit.jsonl"),
        reviewer_model_alias="reviewer-model",
        memory_path=str(tmp_path / "memory.jsonl"),
        memory_ttl_days=7,
    )


@pytest.fixture
def mitigate_cfg(base_cfg: AgentConfig) -> AgentConfig:
    base_cfg.agent_mode = "mitigate"
    return base_cfg
