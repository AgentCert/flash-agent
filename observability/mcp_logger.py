"""
Observability – MCP Interaction Audit Logger
==============================================

Persists MCP Server ↔ Agent request/response pairs to a JSONL file
for audit trail and replay.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from config import AgentConfig

logger = logging.getLogger("flash-agent")


def persist_mcp_interaction(
    cfg: AgentConfig,
    server_type: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    duration_sec: float,
    scan_id: str,
) -> None:
    """
    Write MCP Server ↔ Agent (Req, Response) to a JSONL file.
    One JSON object per line for easy tail/grep/streaming.
    """
    record = {
        "scan_id": scan_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server_type": server_type,
        "namespace": cfg.k8s_namespace,
        "duration_sec": round(duration_sec, 3),
        "request": request_payload,
        "response": response_payload,
        "has_error": "error" in response_payload,
    }
    try:
        Path(cfg.mcp_interactions_file).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.mcp_interactions_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        logger.info(
            "\u2460 MCP Req+Res saved to file: %s", cfg.mcp_interactions_file
        )
    except Exception as exc:
        logger.warning("Failed to write MCP interaction file: %s", exc)
