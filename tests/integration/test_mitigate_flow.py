"""
End-to-end integration test for the mitigation loop.

Boots a ``FlashAgent`` with stub MCP clients and a stub OpenAI client. Covers:
  - observe mode unchanged
  - soft tool executes
  - hard tool reviewed-and-approved executes
  - hard tool reviewed-and-blocked surfaces a tool error
  - violent tool filtered out (never reaches LLM)
  - out-of-scope namespace call blocked by gate
  - episode written + reconciler transitions on next scan
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from config import AgentConfig
from flash_agent import FlashAgent
from mcp.client import MCPClient, MCPScope


# ── stub OpenAI client ──────────────────────────────────────────────────────


@dataclass
class _ToolCallFn:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _ToolCallFn
    type: str = "function"


@dataclass
class _Msg:
    content: Optional[str] = None
    tool_calls: Optional[List[_ToolCall]] = None


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Response:
    choices: List[_Choice]


class _StubCompletions:
    """Returns canned responses from a script."""

    def __init__(self, script: List[Any]) -> None:
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):  # noqa: ANN003, D401
        self.calls.append(kwargs)
        if not self.script:
            raise RuntimeError("Stub exhausted — test scripted too few responses")
        nxt = self.script.pop(0)
        if isinstance(nxt, _Response):
            return nxt
        if isinstance(nxt, BaseException):
            raise nxt
        # Plain-string response → return as message.content (reviewer shape).
        if isinstance(nxt, str):
            return _Response(choices=[_Choice(message=_Msg(content=nxt))])
        # Allow shorthand: dict with "tool_calls" or "content"
        if isinstance(nxt, dict):
            if "tool_calls" in nxt:
                tcs = [
                    _ToolCall(
                        id=f"c{i}",
                        function=_ToolCallFn(
                            name=t["name"], arguments=json.dumps(t.get("args", {}))
                        ),
                    )
                    for i, t in enumerate(nxt["tool_calls"])
                ]
                return _Response(choices=[_Choice(message=_Msg(tool_calls=tcs))])
            return _Response(choices=[_Choice(message=_Msg(content=nxt["content"]))])
        raise TypeError(f"Bad scripted response: {nxt!r}")


class _StubChat:
    def __init__(self, completions: _StubCompletions) -> None:
        self.completions = completions


class _StubOpenAI:
    def __init__(self, script: List[Any]) -> None:
        self.chat = _StubChat(_StubCompletions(script))


# ── stub MCP client (subclass to avoid hitting the network) ─────────────────


class _StubMCPClient(MCPClient):
    def __init__(self, url: str, tools: List[Dict[str, Any]], call_handler) -> None:
        super().__init__(url, "test-agent", timeout=1)
        self._tools = tools
        self._call_handler = call_handler

    def initialize(self) -> Optional[str]:
        self._session_id = "stub-session"
        return self._session_id

    def list_tools(self):
        return list(self._tools)

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._call_handler(tool_name, arguments)

    def discover_scope(self, tool_defs, override=None):
        if override:
            return MCPScope(kind="namespace", namespaces=[override], source="explicit")
        return MCPScope(kind="namespace", namespaces=["alpha"], source="probe")


# ── helpers ─────────────────────────────────────────────────────────────────


def _cfg(tmp_path, mode: str = "mitigate") -> AgentConfig:
    return AgentConfig(
        agent_name="test-agent",
        openai_base_url="http://stub",
        openai_api_key="x",
        model_alias="proposer",
        azure_api_version="2025-04-01-preview",
        mcp_urls=["http://stub/mcp"],
        mcp_timeout=1,
        scan_query="health check",
        scope_override="alpha",
        agent_mode=mode,  # type: ignore[arg-type]
        allow_discovered_scope=True,
        mitigation_review_iters=2,
        mitigation_audit_path=str(tmp_path / "audit.jsonl"),
        reviewer_model_alias="reviewer",
        memory_path=str(tmp_path / "memory.jsonl"),
        memory_ttl_days=7,
    )


def _tool(name: str, description: str = "", *, with_ns: bool = True) -> Dict[str, Any]:
    props = {"namespace": {"type": "string"}} if with_ns else {}
    if name in ("get_pod", "describe_pod", "restart_pod", "scale_dep", "patch_dep"):
        props["name"] = {"type": "string"}
    if name == "scale_dep":
        props["replicas"] = {"type": "integer"}
    return {
        "name": name,
        "description": description or name,
        "inputSchema": {"type": "object", "properties": props, "required": []},
    }


def _final_response(content: str) -> Dict[str, Any]:
    return {"content": content}


def _final_analysis() -> str:
    return json.dumps(
        {
            "status_reasoning": {
                "determined_status": "degraded",
                "status_justification": ["pod restarting"],
                "data_quality": {"completeness": "complete", "gaps": [], "confidence_impact": ""},
            },
            "thoughts": {"key_observations": [], "analysis_approach": [], "observability_gaps": []},
            "health": {"total_pods": 1, "healthy_pods": 0, "unhealthy_pods": 1, "error_count": 1, "warning_count": 0, "overall_health_score": 40},
            "issues": [
                {
                    "severity": "critical",
                    "component": "api",
                    "category": "crashloop",
                    "summary": "pod api in crashloop",
                    "recommended_action": "restart",
                }
            ],
            "insights": {"summary": "x", "concerns": [], "recommendations": [], "observability_recommendations": []},
        }
    )


def _final_clean_analysis() -> str:
    return json.dumps(
        {
            "status_reasoning": {"determined_status": "healthy", "status_justification": [], "data_quality": {"completeness": "complete", "gaps": [], "confidence_impact": ""}},
            "thoughts": {"key_observations": [], "analysis_approach": [], "observability_gaps": []},
            "health": {"total_pods": 1, "healthy_pods": 1, "unhealthy_pods": 0, "error_count": 0, "warning_count": 0, "overall_health_score": 100},
            "issues": [],
            "insights": {"summary": "ok", "concerns": [], "recommendations": [], "observability_recommendations": []},
        }
    )


# ── tests ───────────────────────────────────────────────────────────────────


def _build_agent_with_stubs(
    tmp_path,
    *,
    mode: str,
    tools: List[Dict[str, Any]],
    call_handler,
    llm_script: List[Any],
    reviewer_script: Optional[List[Any]] = None,
) -> FlashAgent:
    cfg = _cfg(tmp_path, mode=mode)
    agent = FlashAgent(cfg)

    stub_mcp = _StubMCPClient("http://stub/mcp", tools, call_handler)

    # Monkeypatch _discover_mcp_tools so the agent uses our stub.
    # The real one would do HTTP; we short-circuit with our pre-built tool list.
    def _discover_stub():
        kept_tools: List[Dict[str, Any]] = []
        name_map: Dict[str, Dict[str, Any]] = {}
        from policy.classifier import classify_and_filter

        for t in tools:
            t["_mcp_url"] = "http://stub/mcp"
        kept, _ = classify_and_filter(tools, cfg, agent.audit)
        for t in kept:
            name_map[t["name"]] = t
            kept_tools.append(t)
        scopes = {"http://stub/mcp": MCPScope(kind="namespace", namespaces=["alpha"], source="explicit")}
        clients = {"http://stub/mcp": stub_mcp}
        return kept_tools, clients, scopes, name_map

    agent._discover_mcp_tools = _discover_stub  # type: ignore[assignment]

    # Inject the stub LLM client.
    stub_llm = _StubOpenAI(llm_script)
    import flash_agent as fa
    agent._stub_llm = stub_llm  # keep alive

    def _stub_create_client(_cfg):
        return stub_llm

    # Replace the module-level factory used by _execute_scan_steps.
    fa._create_openai_client = _stub_create_client  # type: ignore[assignment]

    # Reviewer also gets a stub.
    if reviewer_script is not None:
        agent.reviewer.set_client(_StubOpenAI(reviewer_script))

    return agent


def test_observe_mode_runs_without_executing_hard(tmp_path) -> None:
    """Observe mode: hard tool calls are blocked by gate; LLM eventually emits analysis."""
    tools = [
        _tool("get_pod", "Get a pod"),
        _tool("restart_pod", "Restart a pod"),  # hard
        _tool("delete_pod", "Delete a pod"),  # violent — filtered out
    ]

    def call_handler(name, args):
        return {"content": [{"type": "text", "text": f"called {name} {args}"}]}

    script = [
        # First LLM turn: try to call delete_pod (won't be in inventory) → ignore.
        # Actually we should call something present — go with get_pod.
        {"tool_calls": [{"name": "get_pod", "args": {"namespace": "alpha", "name": "api"}}]},
        # Second: final analysis.
        {"content": _final_analysis()},
    ]
    agent = _build_agent_with_stubs(
        tmp_path, mode="observe", tools=tools, call_handler=call_handler, llm_script=script
    )
    analysis = agent.scan("check health")
    assert analysis["health"]["overall_health_score"] == 40
    # Episodes are only recorded in mitigate mode.
    assert analysis.get("mitigations_attempted", []) == []


def test_violent_tool_never_in_openai_tools(tmp_path) -> None:
    """A delete_pod tool must NEVER be passed to the OpenAI tools= argument."""
    tools = [
        _tool("get_pod"),
        _tool("delete_pod", "Delete a pod"),
    ]

    def call_handler(name, args):
        return {"content": [{"type": "text", "text": "ok"}]}

    script = [
        {"content": _final_clean_analysis()},
    ]
    agent = _build_agent_with_stubs(
        tmp_path, mode="mitigate", tools=tools, call_handler=call_handler, llm_script=script
    )
    agent.scan("scan")
    # Inspect the args passed to the stub LLM.
    sent = agent._stub_llm.chat.completions.calls[0]  # type: ignore[attr-defined]
    sent_tool_names = {t["function"]["name"] for t in sent["tools"]}
    assert "get_pod" in sent_tool_names
    assert "delete_pod" not in sent_tool_names


def test_out_of_scope_namespace_blocked(tmp_path) -> None:
    tools = [_tool("get_pod")]
    handled: List[Dict[str, Any]] = []

    def call_handler(name, args):
        handled.append(args)
        return {"content": [{"type": "text", "text": "ok"}]}

    script = [
        {"tool_calls": [{"name": "get_pod", "args": {"namespace": "evil-ns", "name": "x"}}]},
        {"content": _final_clean_analysis()},
    ]
    agent = _build_agent_with_stubs(
        tmp_path, mode="mitigate", tools=tools, call_handler=call_handler, llm_script=script
    )
    agent.scan("scan")
    # Gate blocked → call_handler was NOT invoked.
    assert handled == []
    # The blocked result is fed back as an MCP-shape error.


def test_hard_action_approved_executes(tmp_path) -> None:
    tools = [_tool("get_pod"), _tool("scale_dep", "Scale a deployment")]
    handled: List[tuple] = []

    def call_handler(name, args):
        handled.append((name, args))
        return {"content": [{"type": "text", "text": "scaled"}]}

    # LLM script: observe pod, then propose hard action, then final analysis.
    llm_script = [
        {"tool_calls": [{"name": "get_pod", "args": {"namespace": "alpha", "name": "api"}}]},
        {"tool_calls": [{"name": "scale_dep", "args": {"namespace": "alpha", "name": "api", "replicas": 3}}]},
        {"content": _final_clean_analysis()},
    ]
    reviewer_script = [
        json.dumps({"verdict": "APPROVED", "reasoning": "ok", "evidence_used": ["pod restart count seen"]}),
        json.dumps({"verdict": "APPROVED", "reasoning": "no worse-case", "evidence_used": ["resource snapshot"]}),
    ]
    agent = _build_agent_with_stubs(
        tmp_path,
        mode="mitigate",
        tools=tools,
        call_handler=call_handler,
        llm_script=llm_script,
        reviewer_script=reviewer_script,
    )
    analysis = agent.scan("scan")
    tool_names_called = [h[0] for h in handled]
    assert "get_pod" in tool_names_called
    assert "scale_dep" in tool_names_called
    # Episode written.
    eps = analysis.get("mitigations_attempted", [])
    assert any(ep["tool"] == "scale_dep" for ep in eps)


def test_hard_action_blocked_does_not_execute(tmp_path) -> None:
    tools = [_tool("scale_dep", "Scale a deployment")]
    handled: List[tuple] = []

    def call_handler(name, args):
        handled.append((name, args))
        return {"content": [{"type": "text", "text": "ran"}]}

    llm_script = [
        {"tool_calls": [{"name": "scale_dep", "args": {"namespace": "alpha", "name": "api", "replicas": 3}}]},
        {"content": _final_clean_analysis()},
    ]
    reviewer_script = [
        json.dumps({"verdict": "BLOCKED", "reasoning": "no evidence in trace", "evidence_used": []}),
    ]
    agent = _build_agent_with_stubs(
        tmp_path,
        mode="mitigate",
        tools=tools,
        call_handler=call_handler,
        llm_script=llm_script,
        reviewer_script=reviewer_script,
    )
    analysis = agent.scan("scan")
    # Hard action blocked → never executed.
    assert handled == []
    # Episode recorded as blocked-by-review.
    eps = analysis.get("mitigations_attempted", [])
    assert any(ep["outcome"] == "blocked-by-review" for ep in eps)


def test_unknown_tool_returns_mcp_error(tmp_path) -> None:
    """The LLM hallucinates a tool not in the inventory → synthesised error."""
    tools = [_tool("get_pod")]

    def call_handler(name, args):
        return {"content": [{"type": "text", "text": "ok"}]}

    llm_script = [
        {"tool_calls": [{"name": "ghost_tool", "args": {"namespace": "alpha"}}]},
        {"content": _final_clean_analysis()},
    ]
    agent = _build_agent_with_stubs(
        tmp_path, mode="mitigate", tools=tools, call_handler=call_handler,
        llm_script=llm_script,
    )
    # Should not raise — synthesised MCP error fed back to LLM.
    a = agent.scan("scan")
    assert a["health"]["overall_health_score"] == 100


def test_reconciliation_marks_succeeded_after_second_scan(tmp_path) -> None:
    """Two-scan workflow: action issued in scan 1, reconciled to succeeded at scan 2."""
    tools = [_tool("get_pod"), _tool("restart_pod", "Restart a pod")]
    handled: List[tuple] = []

    def call_handler(name, args):
        handled.append((name, args))
        return {"content": [{"type": "text", "text": "ok"}]}

    # Scan 1: observe → propose restart (hard) → final analysis with issue.
    scan1_script = [
        {"tool_calls": [{"name": "get_pod", "args": {"namespace": "alpha", "name": "api"}}]},
        {"tool_calls": [{"name": "restart_pod", "args": {"namespace": "alpha", "name": "api"}}]},
        {"content": _final_analysis()},  # contains the issue
    ]
    reviewer1 = [
        json.dumps({"verdict": "APPROVED", "reasoning": "evidence", "evidence_used": ["pod logs"]}),
        json.dumps({"verdict": "APPROVED", "reasoning": "no worse", "evidence_used": ["state"]}),
    ]
    agent = _build_agent_with_stubs(
        tmp_path, mode="mitigate", tools=tools, call_handler=call_handler,
        llm_script=scan1_script, reviewer_script=reviewer1,
    )
    a1 = agent.scan("scan 1")
    assert any(ep["tool"] == "restart_pod" for ep in a1.get("mitigations_attempted", []))

    # Scan 2: observe → final analysis with NO issue → reconciler should mark scan-1
    # episode as succeeded (window has elapsed since restart_pod has 90s window and
    # we'll patch the episode's min_observe_until_ts to be in the past).

    # Manually expire the observation window so the reconciler can act on this scan.
    eps_for_scope = agent.memory.all_for_scope("namespace:alpha")
    for ep in eps_for_scope:
        ep.min_observe_until_ts = time.time() - 1
        agent.memory.update(ep)

    scan2_script = [
        {"content": _final_clean_analysis()},
    ]
    # Replace LLM script for the next scan.
    import flash_agent as fa
    new_llm = _StubOpenAI(scan2_script)
    fa._create_openai_client = lambda _cfg: new_llm  # type: ignore[assignment]
    agent._stub_llm = new_llm

    a2 = agent.scan("scan 2")
    reconciled = a2.get("reconciled_outcomes", [])
    assert reconciled, "Expected reconciled outcomes on scan 2"
    transitions = [r["transition"] for r in reconciled]
    assert "succeeded" in transitions
