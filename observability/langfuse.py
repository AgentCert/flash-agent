"""
Observability – Langfuse Tier-2 Metadata Enrichment
=====================================================

Fire-and-forget POST to Langfuse ingestion API to merge additional
metadata into existing generation spans after LLM calls complete.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import requests

from config import AgentConfig

logger = logging.getLogger("flash-agent")


def update_generation_metadata(
    cfg: AgentConfig,
    generation_id: str,
    extra_metadata: Dict[str, Any],
) -> None:
    """
    Merge additional metadata into an existing Langfuse generation span.

    Used for Tier-2 fields only available after the LLM call completes
    (token usage, fault pass/fail counts).  Langfuse deep-merges metadata
    on observation-update events, so pre-call metadata is preserved.

    Silently skipped when Langfuse credentials are missing.
    """
    if not all((cfg.langfuse_host, cfg.langfuse_public_key, cfg.langfuse_secret_key)):
        return
    try:
        requests.post(
            f"{cfg.langfuse_host.rstrip('/')}/api/public/ingestion",
            json={
                "batch": [
                    {
                        "id": str(uuid.uuid4()),
                        "type": "observation-update",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "body": {
                            "id": generation_id,
                            "metadata": extra_metadata,
                        },
                    }
                ]
            },
            auth=(cfg.langfuse_public_key, cfg.langfuse_secret_key),
            timeout=5,
        )
    except Exception:
        logger.debug("Langfuse post-call metadata update skipped", exc_info=True)
