"""
Flash Agent – Configuration
============================

Centralised configuration loaded from environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env / .env.local if python-dotenv is available."""
    try:
        from dotenv import load_dotenv

        env_file = ".env.local" if Path(".env.local").exists() else ".env"
        load_dotenv(env_file, override=True)
    except ImportError:
        pass


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
    mcp_urls: list[str]
    mcp_timeout: int

    # Scan behaviour
    scan_interval: int
    scan_query: str

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Create configuration from environment variables."""
        _load_dotenv()

        # Parse MCP_URLs as comma-separated list
        mcp_urls_str = os.getenv("MCP_URLS", "")
        mcp_urls = [url.strip() for url in mcp_urls_str.split(",") if url.strip()]

        return cls(
            agent_name=os.getenv("AGENT_NAME", "flash-agent"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            model_alias=os.getenv("MODEL_ALIAS", ""),
            azure_api_version=os.getenv("AZURE_API_VERSION", "2025-04-01-preview"),
            mcp_urls=mcp_urls,
            mcp_timeout=int(os.getenv("MCP_TIMEOUT", "30")),
            scan_interval=int(os.getenv("SCAN_INTERVAL", "0")),
            scan_query=os.getenv(
                "SCAN_QUERY",
                "Analyse the data from MCP tools and provide insights.",
            ),
        )

    def validate(self) -> list[str]:
        """Return list of validation errors. Empty list means valid."""
        errors = []
        if not self.openai_base_url:
            errors.append("OPENAI_BASE_URL is required")
        if not self.model_alias:
            errors.append("MODEL_ALIAS is required")
        if not self.mcp_urls:
            errors.append("MCP_URLS is required")
        return errors
