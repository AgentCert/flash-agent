"""
Flash Agent – Entry Point
==========================

Thin orchestrator harness. Loads configuration, sets up logging,
registers signal handlers, and drives the scan loop.
"""

from __future__ import annotations

import logging
import os
import signal
import time

from config import AgentConfig
from flash_agent import FlashAgent

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("flash-agent")

# ──────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum, _frame) -> None:
    global _shutdown
    logger.info("Received signal %s – shutting down gracefully", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Entry point for Flash Agent.

    Runs in two modes:
      - CronJob mode (SCAN_INTERVAL <= 0): single scan, then exit.
      - Continuous mode (SCAN_INTERVAL > 0): scan every N seconds until shutdown.
    """
    cfg = AgentConfig.from_env()
    errors = cfg.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        raise SystemExit(1)

    logger.info(
        "Flash Agent | agent=%s | model=%s | MCP servers=%d",
        cfg.agent_name, cfg.model_alias, len(cfg.mcp_urls),
    )

    agent = FlashAgent(cfg)

    if cfg.scan_interval <= 0:
        logger.info("CronJob mode – single scan")
        agent.scan(cfg.scan_query)
    else:
        logger.info("Continuous mode – scan every %ds", cfg.scan_interval)
        while not _shutdown:
            try:
                agent.scan(cfg.scan_query)
            except Exception as exc:
                logger.exception("Scan cycle failed: %s", exc)
            for _ in range(cfg.scan_interval):
                if _shutdown:
                    break
                time.sleep(1)

    logger.info("Flash Agent shut down cleanly")


if __name__ == "__main__":
    main()
