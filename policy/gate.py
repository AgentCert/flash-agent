"""
Execution Gate – Scope + Action-Class Enforcement
==================================================

Every tool call (read OR mutate) passes through a single gate that validates
two things at execution time, independent of any prompt instruction the LLM
might have received:

  1. **Scope**: the tool call must be authorised under the MCP server's
     discovered scope (namespace / cluster / agnostic). Out-of-scope calls
     are blocked.
  2. **Action class**: violent tools are blocked unconditionally (defense in
     depth — Phase 1's filter should have already removed them); hard tools
     return ``needs_review`` so the caller can run the Phase 4 reviewer;
     soft tools are allowed once scope passes.

The gate is intentionally side-effect-free except for emitting an audit-log
record per decision. The caller (the ReAct loop) is responsible for acting
on the decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

from config import AgentConfig
from mcp.client import MCPScope
from policy.audit import AuditLog

logger = logging.getLogger("flash-agent")

Decision = Literal["allow", "block", "needs_review"]


@dataclass
class GateDecision:
    """One decision from the execution gate."""

    decision: Decision
    reason: str
    rule: str  # which rule fired — for debugging / audit
    action_class: str = ""


def _is_namespace_scoped_schema(tool_def: Dict[str, Any]) -> bool:
    """True if the tool's inputSchema declares a `namespace` property."""
    schema = tool_def.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    return "namespace" in props


class ExecutionGate:
    """
    Scope + action-class enforcement point.

    Holds the merged scope shown to the LLM AND the per-MCP scope map so it
    can look up the originating MCP's scope class for each tool. Decision
    rules are deterministic and all paths are audit-logged.
    """

    def __init__(
        self,
        cfg: AgentConfig,
        merged_scope: MCPScope,
        mcp_scopes: Dict[str, MCPScope],
        audit: AuditLog,
    ) -> None:
        self.cfg = cfg
        self.merged_scope = merged_scope
        self.mcp_scopes = mcp_scopes
        self.audit = audit

    def evaluate(
        self,
        tool_def: Dict[str, Any],
        args: Dict[str, Any],
        action_class: str,
    ) -> GateDecision:
        """
        Apply the decision rules in order and return the verdict.

        The full ``tool_def`` (not just the name) is required so we can read
        ``tool["_mcp_url"]`` for the per-MCP scope lookup and ``inputSchema``
        for the namespace-arg check.
        """
        tool_name = tool_def.get("name", "")
        mcp_url = tool_def.get("_mcp_url", "")

        # ── Rule 1: violent is always blocked, regardless of scope ───────────
        if action_class == "violent":
            return self._emit(
                tool_name,
                mcp_url,
                action_class,
                args,
                GateDecision(
                    decision="block",
                    reason="violent action class — irreversible operations are never executed",
                    rule="violent-block",
                    action_class=action_class,
                ),
            )

        # ── Rule 2: scope check — depends on the *originating MCP's* scope ──
        per_mcp_scope = self.mcp_scopes.get(mcp_url)
        if per_mcp_scope is None:
            # Fall back to merged scope if we somehow can't find the per-MCP
            # entry. Conservative: in mitigate mode, unknown origin → block.
            per_mcp_scope = self.merged_scope

        scope_decision = self._scope_check(tool_def, args, per_mcp_scope)
        if scope_decision is not None:
            return self._emit(tool_name, mcp_url, action_class, args, scope_decision)

        # ── Rule 3: scope OK — action-class dispatch ─────────────────────────
        if action_class == "soft":
            return self._emit(
                tool_name,
                mcp_url,
                action_class,
                args,
                GateDecision(
                    decision="allow",
                    reason="soft action — scope passed",
                    rule="soft-allow",
                    action_class=action_class,
                ),
            )

        if action_class == "hard":
            # Hard actions short-circuit to block when not in mitigate mode —
            # observe mode never executes mutations.
            if self.cfg.agent_mode != "mitigate":
                return self._emit(
                    tool_name,
                    mcp_url,
                    action_class,
                    args,
                    GateDecision(
                        decision="block",
                        reason="hard action attempted while agent_mode=observe",
                        rule="observe-mode-block",
                        action_class=action_class,
                    ),
                )
            return self._emit(
                tool_name,
                mcp_url,
                action_class,
                args,
                GateDecision(
                    decision="needs_review",
                    reason="hard action — requires reviewer approval",
                    rule="hard-needs-review",
                    action_class=action_class,
                ),
            )

        # Unknown action class — fail closed.
        return self._emit(
            tool_name,
            mcp_url,
            action_class,
            args,
            GateDecision(
                decision="block",
                reason=f"unknown action class {action_class!r} — failing closed",
                rule="unknown-class-block",
                action_class=action_class,
            ),
        )

    # ── internal ─────────────────────────────────────────────────────────────

    def _scope_check(
        self,
        tool_def: Dict[str, Any],
        args: Dict[str, Any],
        per_mcp_scope: MCPScope,
    ) -> Optional[GateDecision]:
        """
        Return a block-decision if scope fails, else None to fall through.

        Rules by per-MCP scope class:
          - ``agnostic``  → allow (scope embedded in query body, e.g. PromQL).
          - ``cluster``   → allow.
          - ``namespace``/``namespaces`` → if schema has a ``namespace`` arg,
            it must be present and in the scope's namespace set; if the schema
            HAS a namespace property but the call omits it, block; if the
            schema has NO namespace property under a namespace-scoped MCP,
            block (the tool can't be scoped — high blast risk).
          - ``unknown`` → block in mitigate mode (refuse to act blind);
            allow in observe mode (today's behavior — we only read).
        """
        kind = per_mcp_scope.kind
        action_class = tool_def.get("_action_class", "")

        if kind == "agnostic":
            return None  # fall through to action-class dispatch

        if kind == "cluster":
            return None  # cluster-wide RBAC — no namespace constraint

        if kind in ("namespace", "namespaces"):
            schema_has_ns = _is_namespace_scoped_schema(tool_def)
            allowed_ns = set(per_mcp_scope.namespaces)

            if not schema_has_ns:
                # Under a namespace-scoped MCP, a tool with no `namespace`
                # property in its schema is cluster-shaped — block it.
                return GateDecision(
                    decision="block",
                    reason=(
                        f"tool has no `namespace` property under namespace-scoped MCP "
                        f"(allowed: {sorted(allowed_ns)})"
                    ),
                    rule="ns-scope-cluster-tool-block",
                    action_class=action_class,
                )

            call_ns = args.get("namespace")
            if not call_ns:
                return GateDecision(
                    decision="block",
                    reason="tool schema declares `namespace` but call omitted it",
                    rule="ns-scope-missing-arg-block",
                    action_class=action_class,
                )

            if call_ns not in allowed_ns:
                return GateDecision(
                    decision="block",
                    reason=(
                        f"namespace={call_ns!r} not in allowed scope "
                        f"{sorted(allowed_ns)}"
                    ),
                    rule="ns-scope-out-of-scope-block",
                    action_class=action_class,
                )
            return None

        if kind == "unknown":
            if self.cfg.agent_mode == "mitigate":
                return GateDecision(
                    decision="block",
                    reason="MCP scope is unknown — refusing to act in mitigate mode",
                    rule="unknown-scope-block",
                    action_class=action_class,
                )
            # Observe mode tolerates unknown scope (today's behavior).
            return None

        # Shouldn't reach — defensive.
        return GateDecision(
            decision="block",
            reason=f"unrecognised scope kind {kind!r}",
            rule="unknown-scope-kind-block",
            action_class=action_class,
        )

    def _emit(
        self,
        tool_name: str,
        mcp_url: str,
        action_class: str,
        args: Dict[str, Any],
        decision: GateDecision,
    ) -> GateDecision:
        """Audit-log the decision before returning it."""
        self.audit.write(
            "gate_decision",
            {
                "tool": tool_name,
                "mcp_url": mcp_url,
                "action_class": action_class,
                "args": _redact_args(args),
                "decision": decision.decision,
                "reason": decision.reason,
                "rule": decision.rule,
                "mcp_scope_kind": (
                    self.mcp_scopes.get(mcp_url, self.merged_scope).kind
                ),
            },
        )
        return decision


def _redact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Truncate args for audit-log space. No secret-stripping here — MCP tool
    args are typically resource names, namespaces, label selectors."""
    out: Dict[str, Any] = {}
    for k, v in args.items():
        s = str(v)
        if len(s) > 256:
            out[k] = s[:256] + "...(truncated)"
        else:
            out[k] = v
    return out


def synthesize_blocked_result(reason: str) -> Dict[str, Any]:
    """
    Build an MCP-shape error result so the ReAct loop sees a normal tool
    failure (not a Python exception) and can re-plan.
    """
    return {
        "isError": True,
        "content": [
            {"type": "text", "text": f"BLOCKED: {reason}"},
        ],
    }
