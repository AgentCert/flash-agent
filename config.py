"""
Flash Agent – Configuration
============================

Centralised configuration loaded from environment variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal

logger = logging.getLogger("flash-agent")


def _load_dotenv() -> None:
    """Load .env / .env.local if python-dotenv is available."""
    try:
        from dotenv import load_dotenv

        env_file = ".env.local" if Path(".env.local").exists() else ".env"
        load_dotenv(env_file, override=True)
    except ImportError:
        pass


# Conservative defaults — patterns are regexes matched against tool name OR description.
# Soft: pure reads / observation.
# Hard: mutating but recoverable (state change, not erase).
# Violent: irreversible erasure or force-kill — never executed by the agent.
#
# Note: we use `(?<![a-z])VERB(?![a-z])` instead of `\bVERB\b` so that tool
# names like ``get_pod`` and ``scale-deployment`` (where ``_`` / ``-`` would
# otherwise NOT count as boundaries — ``_`` is a regex word char) match.
DEFAULT_PATTERNS_SOFT: List[str] = [
    r"(?i)(?<![a-z])(list|get|describe|read|query|search|watch|fetch|show|status|inspect|view|metrics|logs?)(?![a-z])",
]
DEFAULT_PATTERNS_HARD: List[str] = [
    r"(?i)(?<![a-z])(patch|update|scale|restart|rollout|apply|cordon|drain|exec|edit|annotate|label|set)(?![a-z])",
    r"(?i)(?<![a-z])(create|add)(?![a-z])",  # creation is mutating but recoverable
]
DEFAULT_PATTERNS_VIOLENT: List[str] = [
    r"(?i)(?<![a-z])(delete|destroy|drop|purge|wipe|remove)(?![a-z])",
    r"(?i)evict.*--?force",
    r"(?i)kill.*--?grace=?0",
    r"(?i)(?<![a-z])force[-_]?kill(?![a-z])",
]


def _parse_pattern_list(env_value: str, default: List[str]) -> List[str]:
    """Parse comma-separated regex patterns from env, falling back to defaults."""
    raw = (env_value or "").strip()
    if not raw:
        return list(default)
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class AgentConfig:
    """All agent configuration in one place."""

    # Agent identity
    agent_name: str

    # LLM
    openai_base_url: str
    openai_api_key: str
    model_alias: str
    azure_api_version: str

    # MCP Servers (comma-separated URLs)
    mcp_urls: List[str]
    mcp_timeout: int

    # Scan query
    scan_query: str

    # Optional explicit scope override — when set, skips MCP scope discovery
    # and forces the agent to operate within this namespace. Empty = auto-discover.
    scope_override: str

    # ── Mitigation knobs (Phase 0+) ──────────────────────────────────────────
    agent_mode: Literal["observe", "mitigate"] = "observe"
    allow_discovered_scope: bool = False

    action_patterns_soft: List[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS_SOFT))
    action_patterns_hard: List[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS_HARD))
    action_patterns_violent: List[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS_VIOLENT))

    mitigation_review_iters: int = 2
    mitigation_audit_path: str = ""

    # Reviewer model — reuses the same OPENAI_BASE_URL + OPENAI_API_KEY as the
    # main agent. The difference in model alias is the source of uncorrelated
    # errors between the proposer (main agent) and the reviewer.
    reviewer_model_alias: str = ""

    memory_path: str = ""
    memory_ttl_days: int = 7

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Create configuration from environment variables."""
        _load_dotenv()

        mcp_urls_str = os.getenv("MCP_URLS", "")
        mcp_urls = [url.strip() for url in mcp_urls_str.split(",") if url.strip()]

        raw_mode = (os.getenv("AGENT_MODE", "observe") or "observe").strip().lower()
        agent_mode: Literal["observe", "mitigate"] = (
            "mitigate" if raw_mode == "mitigate" else "observe"
        )

        return cls(
            agent_name=os.getenv("AGENT_NAME", "flash-agent"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            model_alias=os.getenv("MODEL_ALIAS", ""),
            azure_api_version=os.getenv("AZURE_API_VERSION", "2025-04-01-preview"),
            mcp_urls=mcp_urls,
            mcp_timeout=int(os.getenv("MCP_TIMEOUT", "30")),
            scan_query=os.getenv(
                "SCAN_QUERY",
                "Analyse the data from MCP tools and provide insights.",
            ),
            scope_override=os.getenv("AGENT_SCOPE_NAMESPACE", "").strip(),
            agent_mode=agent_mode,
            allow_discovered_scope=_parse_bool(
                os.getenv("MITIGATION_ALLOW_DISCOVERED_SCOPE", "false")
            ),
            action_patterns_soft=_parse_pattern_list(
                os.getenv("ACTION_PATTERNS_SOFT", ""), DEFAULT_PATTERNS_SOFT
            ),
            action_patterns_hard=_parse_pattern_list(
                os.getenv("ACTION_PATTERNS_HARD", ""), DEFAULT_PATTERNS_HARD
            ),
            action_patterns_violent=_parse_pattern_list(
                os.getenv("ACTION_PATTERNS_VIOLENT", ""), DEFAULT_PATTERNS_VIOLENT
            ),
            mitigation_review_iters=int(os.getenv("MITIGATION_REVIEW_ITERS", "2")),
            mitigation_audit_path=os.getenv("MITIGATION_AUDIT_PATH", "").strip(),
            reviewer_model_alias=os.getenv("REVIEWER_MODEL_ALIAS", "").strip(),
            memory_path=os.getenv("AGENT_MEMORY_PATH", "").strip(),
            memory_ttl_days=int(os.getenv("MEMORY_TTL_DAYS", "7")),
        )

    def validate(self) -> List[str]:
        """Return list of validation errors. Empty list means valid."""
        errors: List[str] = []
        if not self.openai_base_url:
            errors.append("OPENAI_BASE_URL is required")
        if not self.model_alias:
            errors.append("MODEL_ALIAS is required")
        if not self.mcp_urls:
            errors.append("MCP_URLS is required")

        # Mitigate mode requires explicit operator awareness about the scope it
        # will act on. Either an explicit override is set, or the operator has
        # acknowledged that auto-discovered scope is OK to mutate.
        if self.agent_mode == "mitigate":
            if not self.scope_override and not self.allow_discovered_scope:
                errors.append(
                    "AGENT_MODE=mitigate requires either AGENT_SCOPE_NAMESPACE "
                    "(explicit scope) or MITIGATION_ALLOW_DISCOVERED_SCOPE=true "
                    "(acknowledge acting on auto-discovered scope)"
                )
            if not self.reviewer_model_alias:
                # Degraded mode is allowed but logged loudly so operators can see it.
                logger.warning(
                    "REVIEWER_MODEL_ALIAS is empty — hard-action review will run "
                    "with the proposer's model_alias=%s. Reviewer errors will be "
                    "correlated with the proposer's reasoning (degraded mode).",
                    self.model_alias,
                )

        return errors


def _parse_bool(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")
