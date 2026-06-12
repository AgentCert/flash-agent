"""
Tool Classifier – Action Trichotomy
=====================================

Every discovered MCP tool is classified into one of three classes:

  soft     — pure observation: list/get/describe/read/query/search/watch.
             Executes any time in mitigate mode. No review.
  hard     — recoverable mutation: patch/update/scale/restart/etc.
             Requires 2-iteration LLM review before executing.
  violent  — irreversible erasure: delete/destroy/drop/purge/wipe/force-evict.
             Never executed. Filtered out of the tool list shown to the LLM.

Classification is pure: regex patterns over tool name and description, plus
an inputSchema-shape signal. Ambiguous → ``hard`` (safe-by-default).

The classifier is intentionally agnostic to specific tool names — it operates
on verb shapes so it works across any MCP server.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Literal, Tuple

from config import AgentConfig

logger = logging.getLogger("flash-agent")

ActionClass = Literal["soft", "hard", "violent"]


def _compile(patterns: List[str]) -> List[re.Pattern[str]]:
    """Compile a list of regex strings. Skip and log any that fail to compile."""
    compiled: List[re.Pattern[str]] = []
    for pat in patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error as exc:
            logger.warning("Bad action pattern %r: %s — skipped", pat, exc)
    return compiled


def _matches_any(haystack: str, patterns: List[re.Pattern[str]]) -> bool:
    return any(p.search(haystack) for p in patterns)


def is_mutation_schema(tool_def: Dict[str, Any]) -> bool:
    """
    Secondary signal: cluster-scoped mutations (schema lacks ``namespace``
    property) get escalated to violent — they have the highest blast radius.
    """
    schema = tool_def.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    return "namespace" not in props


def classify_tool(tool_def: Dict[str, Any], cfg: AgentConfig) -> ActionClass:
    """
    Classify a single tool definition.

    Resolution order:
      1. Violent patterns (highest priority — must be filtered out).
      2. Hard patterns (mutating but recoverable).
      3. Soft patterns (pure reads).
      4. Cluster-scoped mutation escalation: hard ∧ no namespace → violent.
      5. Ambiguous → hard (safe-by-default — refuses to execute without review).
    """
    name = tool_def.get("name", "") or ""
    description = tool_def.get("description", "") or ""
    haystack = f"{name}\n{description}"

    violent = _compile(cfg.action_patterns_violent)
    hard = _compile(cfg.action_patterns_hard)
    soft = _compile(cfg.action_patterns_soft)

    if _matches_any(haystack, violent):
        return "violent"

    is_hard_verb = _matches_any(haystack, hard)
    is_soft_verb = _matches_any(haystack, soft)

    # If both hard and soft patterns match (common when a tool's description
    # mentions both reading and writing), trust the more dangerous signal.
    if is_hard_verb:
        # Escalate cluster-scoped mutations to violent regardless of pattern.
        if is_mutation_schema(tool_def):
            return "violent"
        return "hard"

    if is_soft_verb:
        return "soft"

    # Ambiguous — default to hard. We refuse to call something whose verb
    # shape we can't recognize as a pure read without a review pass.
    return "hard"


def classify_and_filter(
    tools: List[Dict[str, Any]],
    cfg: AgentConfig,
    audit_writer: Any = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Classify every tool and partition into ``(kept, filtered_violent)``.

    Mutates each kept tool to set ``tool["_action_class"]``. Audit-logs every
    filtered violent tool with ``reason="violent-classified"``.

    Args:
        tools: List of MCP tool definitions (post ``list_tools``).
        cfg: Agent config (action patterns).
        audit_writer: Optional ``AuditLog``-like object with ``write(event, payload)``.

    Returns:
        ``(kept, filtered_violent)`` — only ``kept`` should be shown to the LLM.
    """
    kept: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []

    for tool in tools:
        cls = classify_tool(tool, cfg)
        tool["_action_class"] = cls

        if audit_writer is not None:
            audit_writer.write(
                "tool_classified",
                {
                    "tool": tool.get("name", ""),
                    "mcp_url": tool.get("_mcp_url", ""),
                    "action_class": cls,
                    "has_namespace_param": not is_mutation_schema(tool),
                },
            )

        if cls == "violent":
            filtered.append(tool)
            if audit_writer is not None:
                audit_writer.write(
                    "tool_filtered",
                    {
                        "tool": tool.get("name", ""),
                        "mcp_url": tool.get("_mcp_url", ""),
                        "reason": "violent-classified",
                    },
                )
            logger.info(
                "Filtered violent tool from LLM inventory: %s (mcp=%s)",
                tool.get("name", ""),
                tool.get("_mcp_url", ""),
            )
        else:
            kept.append(tool)

    return kept, filtered
