"""Tests for the scope-enforced execution gate (Phase 2)."""

from __future__ import annotations

from mcp.client import MCPScope
from policy.audit import AuditLog
from policy.gate import ExecutionGate, synthesize_blocked_result


def _tool(name: str, mcp_url: str, action_class: str, *, with_ns: bool = True) -> dict:
    props = {"namespace": {"type": "string"}} if with_ns else {}
    return {
        "name": name,
        "description": "",
        "inputSchema": {"type": "object", "properties": props, "required": []},
        "_mcp_url": mcp_url,
        "_action_class": action_class,
    }


def _gate(cfg, mcp_scope_kind: str, namespaces=("alpha",)) -> ExecutionGate:
    if mcp_scope_kind == "namespace":
        scope = MCPScope(kind="namespace", namespaces=list(namespaces[:1]))
    elif mcp_scope_kind == "namespaces":
        scope = MCPScope(kind="namespaces", namespaces=list(namespaces))
    elif mcp_scope_kind == "agnostic":
        scope = MCPScope(kind="agnostic")
    elif mcp_scope_kind == "cluster":
        scope = MCPScope(kind="cluster")
    else:
        scope = MCPScope(kind="unknown")
    mcp_scopes = {"http://m": scope}
    return ExecutionGate(cfg, scope, mcp_scopes, AuditLog(None))


# ── violent always blocked ──────────────────────────────────────────────────


def test_violent_blocked(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespace")
    t = _tool("delete_pod", "http://m", "violent")
    d = g.evaluate(t, {"namespace": "alpha"}, "violent")
    assert d.decision == "block"
    assert d.rule == "violent-block"


# ── agnostic MCPs (e.g. Prometheus) ─────────────────────────────────────────


def test_agnostic_scope_allows_without_namespace_arg(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "agnostic")
    t = _tool("query", "http://m", "soft", with_ns=False)
    d = g.evaluate(t, {"query": "up"}, "soft")
    assert d.decision == "allow"
    assert d.rule == "soft-allow"


# Critical regression — mitigate mode must NOT introduce a blind spot for
# Prometheus-style (agnostic) tools whose namespace lives in the query body.
def test_agnostic_query_allowed_in_mitigate(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "agnostic")
    t = _tool("prom_query", "http://m", "soft", with_ns=False)
    d = g.evaluate(t, {"query": 'up{namespace="alpha"}'}, "soft")
    assert d.decision == "allow"


# ── cluster-scoped MCP ──────────────────────────────────────────────────────


def test_cluster_scope_allows_namespace_omission(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "cluster")
    t = _tool("list_nodes", "http://m", "soft", with_ns=False)
    d = g.evaluate(t, {}, "soft")
    assert d.decision == "allow"


# ── namespace-scoped MCP ────────────────────────────────────────────────────


def test_namespace_in_scope_allows_soft(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespace")
    t = _tool("list_pods", "http://m", "soft")
    d = g.evaluate(t, {"namespace": "alpha"}, "soft")
    assert d.decision == "allow"


def test_namespace_out_of_scope_blocks(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespace")
    t = _tool("list_pods", "http://m", "soft")
    d = g.evaluate(t, {"namespace": "evil-ns"}, "soft")
    assert d.decision == "block"
    assert d.rule == "ns-scope-out-of-scope-block"


def test_namespace_arg_missing_blocks(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespace")
    t = _tool("list_pods", "http://m", "soft")
    d = g.evaluate(t, {}, "soft")
    assert d.decision == "block"
    assert d.rule == "ns-scope-missing-arg-block"


def test_cluster_tool_under_namespace_scope_blocks(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespace")
    t = _tool("list_nodes", "http://m", "soft", with_ns=False)
    d = g.evaluate(t, {}, "soft")
    assert d.decision == "block"
    assert d.rule == "ns-scope-cluster-tool-block"


def test_namespaces_scope_allows_member(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespaces", namespaces=("alpha", "beta"))
    t = _tool("list_pods", "http://m", "soft")
    assert g.evaluate(t, {"namespace": "beta"}, "soft").decision == "allow"


def test_namespaces_scope_rejects_non_member(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespaces", namespaces=("alpha", "beta"))
    t = _tool("list_pods", "http://m", "soft")
    assert g.evaluate(t, {"namespace": "gamma"}, "soft").decision == "block"


# ── unknown scope ───────────────────────────────────────────────────────────


def test_unknown_scope_blocks_in_mitigate(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "unknown")
    t = _tool("list_pods", "http://m", "soft")
    d = g.evaluate(t, {"namespace": "x"}, "soft")
    assert d.decision == "block"
    assert d.rule == "unknown-scope-block"


def test_unknown_scope_allows_in_observe(base_cfg) -> None:
    base_cfg.agent_mode = "observe"
    g = _gate(base_cfg, "unknown")
    t = _tool("list_pods", "http://m", "soft")
    assert g.evaluate(t, {"namespace": "x"}, "soft").decision == "allow"


# ── action-class dispatch (post-scope) ──────────────────────────────────────


def test_hard_action_returns_needs_review_in_mitigate(mitigate_cfg) -> None:
    g = _gate(mitigate_cfg, "namespace")
    t = _tool("patch_dep", "http://m", "hard")
    d = g.evaluate(t, {"namespace": "alpha"}, "hard")
    assert d.decision == "needs_review"
    assert d.rule == "hard-needs-review"


def test_hard_action_blocked_in_observe(base_cfg) -> None:
    base_cfg.agent_mode = "observe"
    g = _gate(base_cfg, "namespace")
    t = _tool("patch_dep", "http://m", "hard")
    d = g.evaluate(t, {"namespace": "alpha"}, "hard")
    assert d.decision == "block"
    assert d.rule == "observe-mode-block"


# ── synth helper ────────────────────────────────────────────────────────────


def test_synthesize_blocked_result_shape() -> None:
    r = synthesize_blocked_result("nope")
    assert r["isError"] is True
    assert "BLOCKED" in r["content"][0]["text"]
