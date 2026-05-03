"""
Flash Agent – FLASH-style Workflow Automation Agent
====================================================

Implements Microsoft Research's FLASH methodology for reliable
multi-step task execution with status supervision and hindsight.

FLASH Reasoning Loop (per scan):
  1. DISCOVER          → Discover available MCP tools
  2. REASON + ACT      → LLM decides which tools to call (ReAct loop)
  3. ANALYZE           → LLM produces structured analysis
  4. REFLECT           → Generate hindsight from failures for future scans

Key FLASH enhancements:
  - Status Supervision: Breaks complex tasks into status-dependent steps
  - Hindsight Integration: Learns from past failures to improve reliability
  - Agentic Tool Use: LLM decides which tools to call with what arguments

Reference: https://www.microsoft.com/en-us/research/project/flash-a-reliable-workflow-automation-agent/
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from openai import AzureOpenAI, OpenAI
from openai.types.chat import ChatCompletionMessageToolCall

from config import AgentConfig
from llm.hindsight import HindsightBuilder
from llm.utils import (
    create_history_entry,
    format_analysis_for_history,
    trim_history_to_token_limit,
)
from mcp.client import MCPClient, MCPScope

logger = logging.getLogger("flash-agent")

# Maximum tool-calling iterations to prevent infinite loops
MAX_TOOL_ITERATIONS = 10


def _build_system_prompt(scope: MCPScope) -> str:
    """
    Build a scope-aware system prompt from a discovered :class:`MCPScope`.

    The scope kind drives which constraints the LLM sees:
      - ``namespace``  → strict single-ns prompt (most common case)
      - ``namespaces`` → multi-ns prompt; LLM must pass explicit ``namespace=``
      - ``cluster``    → cluster-wide read is permitted
      - ``agnostic``/``unknown`` → soft fallback; prefer namespace-scoped tools
                                   but don't claim authority we haven't verified
    """
    if scope.kind == "namespace" and scope.namespaces:
        ns = scope.namespaces[0]
        scope_block = (
            f"## Your Scope\n"
            f"You operate inside a single Kubernetes namespace: **`{ns}`**.\n"
            f"The MCP server backing your tools is bound by RBAC to this namespace only.\n"
            f"You MUST stay within this scope:\n"
            f"- Always pass `namespace=\"{ns}\"` to any tool that accepts a `namespace` argument.\n"
            f"- Prefer namespace-scoped tool variants (commonly suffixed `_in_namespace`, or any tool whose schema declares a `namespace` parameter).\n"
            f"- Do NOT call cluster-scoped tools — they will fail with RBAC `forbidden`:\n"
            f"  - `namespaces_list` (cluster-scoped resource)\n"
            f"  - `pods_list`, `events_list` without a namespace argument (defaults to `default`, which you cannot read)\n"
            f"  - `nodes_top`, `nodes_*` (cluster-scoped; metrics API may also be unavailable)\n"
            f"- For Prometheus-style queries, pin `{{namespace=\"{ns}\"}}` selectors.\n"
            f"- If a tool returns `forbidden` or `not available`, do NOT retry it. Record it as an observability gap and continue with tools that work.\n"
        )
    elif scope.kind == "namespaces" and scope.namespaces:
        ns_list = ", ".join(f"`{n}`" for n in scope.namespaces)
        scope_block = (
            f"## Your Scope\n"
            f"You may operate across these Kubernetes namespaces: {ns_list}.\n"
            f"The MCP server's RBAC allows access to each of these — but NO others.\n"
            f"You MUST:\n"
            f"- Pass an explicit `namespace=...` argument on every tool call (one of the namespaces above).\n"
            f"- Prefer namespace-scoped tool variants. Do NOT call `namespaces_list`, `pods_list` (cluster-wide), or `nodes_top`.\n"
            f"- For Prometheus-style queries, pin `{{namespace=~\"{('|'.join(scope.namespaces))}\"}}` selectors.\n"
            f"- If a tool returns `forbidden` or `not available`, do NOT retry it; record it as an observability gap.\n"
        )
    elif scope.kind == "cluster":
        scope_block = (
            "## Your Scope\n"
            "The MCP server has cluster-wide RBAC. You may use cluster-scoped tools "
            "(`pods_list`, `namespaces_list`, `nodes_top`) and namespace-scoped tools alike.\n"
            "Still, prefer narrow queries when investigating a specific component to keep results actionable.\n"
            "If a tool returns `not available` (e.g. metrics API), record it as an observability gap.\n"
        )
    else:
        # agnostic or unknown
        scope_block = (
            "## Your Scope\n"
            "Target scope could not be auto-discovered from the MCP server.\n"
            "Prefer namespace-scoped tools (any tool whose schema declares a `namespace` parameter).\n"
            "Avoid cluster-scoped calls (`namespaces_list`, `nodes_top`) unless you have evidence the MCP service account is authorized cluster-wide.\n"
            "If a tool returns `forbidden` or `not available`, do NOT retry it; record it as an observability gap.\n"
        )

    return _SYSTEM_PROMPT_HEAD + scope_block + _SYSTEM_PROMPT_TAIL


_SYSTEM_PROMPT_HEAD = """You are an ITOps analysis agent with access to Kubernetes tools via MCP.

Your task is to analyze the health of your assigned Kubernetes scope by:
1. Using the available tools to gather information
2. Investigating any issues you discover
3. Producing a final structured analysis

"""

_SYSTEM_PROMPT_TAIL = """
## Tool Usage Guidelines
- After identifying problem pods, drill down with `pods_log` and `pods_get`.
- Try `pods_top` (within your namespace) to check pod-level resource utilization.
- Not all tools require arguments — inspect each tool's schema.

## CRITICAL: Observability Gap Detection
You MUST attempt to gather resource metrics (CPU/memory) via `pods_top` (namespace-scoped).
If it fails with "metrics API not available" or similar errors:

1. **Report this as a WARNING issue** - this is a significant observability gap
2. **Explicitly state the limitations** in your analysis:
   - Cannot detect CPU/memory pressure or throttling
   - Cannot identify resource saturation on nodes
   - Cannot correlate events with resource exhaustion
   - Health assessment is INCOMPLETE without metrics
3. **Recommend remediation**: Deploy metrics-server or equivalent
4. **Reduce confidence** in health score (cap at 85 if metrics unavailable)

Without metrics, you can only assess:
✓ Pod status (Running/Pending/Failed)
✓ Restart counts
✓ Events (warnings, errors)
✓ Basic availability

You CANNOT assess without metrics:
✗ CPU/memory utilization
✗ Resource pressure or throttling
✗ Node capacity issues
✗ OOM risk

## When You Have Enough Information
Once you have gathered sufficient data to assess cluster health, provide your final analysis.

## JSON Output Format
Your final response must be a valid JSON object with this structure:
{
  "status_reasoning": {
    "determined_status": "healthy|degraded|critical|unknown",
    "status_justification": ["reason1", "reason2"],
    "data_quality": {
      "completeness": "complete|partial|insufficient",
      "gaps": ["metrics_unavailable", "logs_unavailable", ...],
      "confidence_impact": "Health score capped due to missing observability"
    }
  },
  "thoughts": {
    "key_observations": [],
    "analysis_approach": [],
    "observability_gaps": ["List any tools that failed or data that was unavailable"]
  },
  "health": {
    "total_pods": 0,
    "healthy_pods": 0,
    "unhealthy_pods": 0,
    "error_count": 0,
    "warning_count": 0,
    "overall_health_score": 0
  },
  "issues": [
    {
      "severity": "critical|warning|info",
      "component": "component-name",
      "category": "category",
      "summary": "description",
      "recommended_action": "what to do"
    }
  ],
  "insights": {
    "summary": "overall summary",
    "concerns": [],
    "recommendations": [],
    "observability_recommendations": ["Deploy metrics-server", ...]
  }
}
"""

BASELINE_PROMPT = """You are setting up continuous health monitoring for a Kubernetes namespace.

## Available MCP Tools
{tool_list}

## Task
Select the minimal set of tools to poll repeatedly for monitoring namespace "{namespace}".
These tools will run every few seconds WITHOUT an LLM - only when metrics change will analysis trigger.

## Selection Guidelines
- Prefer namespace-scoped tools (e.g., `pods_list_in_namespace` over `pods_list`)
- Include pod status monitoring (required)
- Include events if available (recommended for detecting issues)
- Do NOT include tools requiring specific pod names (unknown at setup time)
- Do NOT include log tools (too noisy for polling)

## Response Format
Return ONLY valid JSON:
{{
  "watch_tools": [
    {{"name": "tool_name", "args": {{"namespace": "{namespace}"}}}}
  ],
  "healthy_thresholds": {{
    "min_pods": 1,
    "max_restart_delta": 0,
    "max_pending_pods": 0,
    "max_failed_pods": 0
  }}
}}
"""


@dataclass
class WatchBaseline:
    """Baseline configuration for watch mode."""
    namespace: str
    watch_tools: List[Dict[str, Any]]
    healthy_thresholds: Dict[str, int]
    baseline_metrics: Dict[str, Any] = field(default_factory=dict)
    established_at: str = ""


def _create_openai_client(cfg: AgentConfig) -> OpenAI:
    """Create an OpenAI-compatible client (supports Azure or standard endpoints)."""
    if cfg.openai_base_url and ".openai.azure.com" in cfg.openai_base_url:
        return AzureOpenAI(
            api_key=cfg.openai_api_key,
            azure_endpoint=cfg.openai_base_url,
            api_version=cfg.azure_api_version,
            timeout=120.0,
        )
    return OpenAI(
        api_key=cfg.openai_api_key or "not-needed",
        base_url=cfg.openai_base_url,
        timeout=120.0,
    )


def _convert_mcp_tool_to_openai(tool_def: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an MCP tool definition to OpenAI function-calling format.
    
    MCP format:
        {"name": "pods_list", "description": "...", "inputSchema": {...}}
    
    OpenAI format:
        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    input_schema = tool_def.get("inputSchema", {})
    
    # Clean up the schema for OpenAI compatibility
    parameters = {
        "type": input_schema.get("type", "object"),
        "properties": input_schema.get("properties", {}),
    }
    
    # Only include required if it has items
    required = input_schema.get("required", [])
    if required:
        parameters["required"] = required
    
    return {
        "type": "function",
        "function": {
            "name": tool_def.get("name", ""),
            "description": tool_def.get("description", "No description"),
            "parameters": parameters,
        }
    }


def _format_tool_result(tool_name: str, result: Dict[str, Any]) -> str:
    """Format a tool result for inclusion in conversation."""
    if isinstance(result, dict) and result.get("error"):
        return f"Error: {result['error']}"
    
    # Extract text content from MCP response
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            result_text = "\n".join(texts)
        else:
            result_text = str(content)
    else:
        result_text = json.dumps(result, indent=2, default=str)
    
    # Truncate very long results
    if len(result_text) > 8000:
        result_text = result_text[:8000] + "\n... (truncated)"
    
    return result_text


class FlashAgent:
    """
    Flash Agent – ITOps analysis agent with hindsight reflection.

    Uses FLASH-style hindsight integration for improved reliability
    on multi-step tasks. Dynamically discovers tools from configured
    MCP servers and applies LLM analysis with optional hindsight.
    """

    # Maximum history entries to retain across scans
    MAX_HISTORY_SIZE = 20

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._scan_counter = 0
        
        # FLASH hindsight components
        self.history: List[Dict[str, Any]] = []
        self.hindsight_builder = HindsightBuilder(cfg)
        self._last_hindsight: str | None = None

    def scan(self, query: str) -> Dict[str, Any]:
        """Execute one full analysis scan cycle."""
        self._scan_counter += 1
        scan_start = time.time()
        scan_id = (
            f"{self.cfg.agent_name}"
            f"-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        )
        logger.info(
            "\u2550\u2550\u2550 Scan #%d started | scan_id=%s \u2550\u2550\u2550",
            self._scan_counter,
            scan_id,
        )

        analysis = self._execute_scan_steps(
            scan_query=query,
            scan_id=scan_id,
            scan_start=scan_start,
        )
        return analysis

    def health_check(self) -> bool:
        """Return True if the agent is ready to accept scan requests."""
        return bool(self.cfg.agent_name and self.cfg.mcp_urls)

    def get_capabilities(self) -> List[str]:
        """Return list of MCP server URLs this agent is configured to use."""
        return list(self.cfg.mcp_urls)

    def _execute_scan_steps(
        self,
        scan_query: str,
        scan_id: str,
        scan_start: float,
    ) -> Dict[str, Any]:
        """
        Execute FLASH reasoning loop with agentic tool calling.

        ReAct Loop:
          1. Discover tools from MCP servers
          2. Give LLM the task + available tools
          3. LLM decides which tools to call
          4. Execute tool calls, return results to LLM
          5. Repeat until LLM produces final analysis
          6. Generate hindsight if needed
        """
        # ── Record query in history ──────────────────────────────────────────
        self._add_to_history("user", f"Query: {scan_query}", {"scan_id": scan_id})

        # ── Step 1: Discover MCP tools + scope ───────────────────────────────
        mcp_tools, mcp_clients, mcp_scopes = self._discover_mcp_tools()

        if not mcp_tools:
            logger.error("No MCP tools discovered – cannot proceed")
            return {"health": {"overall_health_score": -1}, "issues": []}

        # Convert to OpenAI function format
        openai_tools = [_convert_mcp_tool_to_openai(t) for t in mcp_tools]
        tool_names = [t["function"]["name"] for t in openai_tools]
        logger.info("Discovered %d tools: %s", len(openai_tools), tool_names)

        # Merge per-MCP scopes into the single scope shown to the LLM.
        merged_scope = self._merge_scopes(list(mcp_scopes.values()))
        logger.info("Discovered scope: %s", merged_scope.describe())

        # ── Step 2: Initialize LLM conversation ──────────────────────────────
        client = _create_openai_client(self.cfg)
        system_prompt = _build_system_prompt(merged_scope)

        # Build initial prompt with optional hindsight
        hindsight = self._get_hindsight_for_prompt(scan_id)
        if merged_scope.kind == "namespace" and merged_scope.namespaces:
            user_prompt = (
                f"Analyze the health of namespace `{merged_scope.namespaces[0]}` "
                f"using only namespace-scoped tools. Task: {scan_query}"
            )
        elif merged_scope.kind == "namespaces" and merged_scope.namespaces:
            ns_csv = ", ".join(merged_scope.namespaces)
            user_prompt = (
                f"Analyze the health of namespaces [{ns_csv}] "
                f"using namespace-scoped tools (always pass an explicit `namespace=`). "
                f"Task: {scan_query}"
            )
        elif merged_scope.kind == "cluster":
            user_prompt = f"Analyze the Kubernetes cluster health. Task: {scan_query}"
        else:
            user_prompt = f"Analyze the Kubernetes namespace health. Task: {scan_query}"
        if hindsight:
            user_prompt = f"{user_prompt}\n\nHINDSIGHT FROM PREVIOUS ANALYSIS:\n{hindsight}"
            logger.info("Injected hindsight into prompt (%d chars)", len(hindsight))

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        
        # ── Step 3: ReAct loop - LLM decides tools, we execute ───────────────
        analysis = None
        iteration = 0
        tool_calls_made: List[str] = []
        
        while iteration < MAX_TOOL_ITERATIONS:
            iteration += 1
            logger.info("ReAct iteration %d/%d", iteration, MAX_TOOL_ITERATIONS)
            
            try:
                response = client.chat.completions.create(
                    model=self.cfg.model_alias,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto",
                    temperature=0.1,
                )
            except Exception as exc:
                logger.error("LLM call failed: %s", exc)
                break
            
            assistant_message = response.choices[0].message
            
            # Check if LLM wants to call tools
            if assistant_message.tool_calls:
                # Add assistant message with tool calls to conversation
                messages.append({
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }
                        }
                        for tc in assistant_message.tool_calls
                    ]
                })
                
                # Execute each tool call
                for tool_call in assistant_message.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                    
                    logger.info("  Tool call: %s(%s)", tool_name, tool_args)
                    tool_calls_made.append(tool_name)
                    
                    # Execute via MCP
                    result = self._execute_mcp_tool(mcp_clients, tool_name, tool_args)
                    result_text = _format_tool_result(tool_name, result)
                    
                    logger.info("  Tool result: %d chars", len(result_text))
                    
                    # Add tool result to conversation
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    })
            else:
                # LLM produced final response - try to parse as JSON
                content = assistant_message.content or ""
                logger.info("LLM final response (%d chars)", len(content))
                
                try:
                    # Try to extract JSON from the response
                    analysis = self._parse_analysis_response(content)
                    break
                except Exception as exc:
                    logger.warning("Could not parse analysis: %s", exc)
                    # Ask LLM to format as JSON
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user", 
                        "content": "Please provide your analysis as a valid JSON object matching the required schema."
                    })
        
        if analysis is None:
            logger.error("ReAct loop ended without valid analysis")
            return {"health": {"overall_health_score": -1}, "issues": []}

        # ── Step 4: Log results ──────────────────────────────────────────────
        duration = time.time() - scan_start
        health = analysis.get("health", {})
        issues = analysis.get("issues", [])
        
        logger.info(
            "\u2550\u2550\u2550 Scan complete | scan_id=%s | %.1fs | tools_called=%d | "
            "health=%s | issues=%d \u2550\u2550\u2550",
            scan_id,
            duration,
            len(tool_calls_made),
            health.get("overall_health_score", "?"),
            len(issues),
        )
        for issue in issues:
            logger.info(
                "  [%s] %s — %s",
                issue.get("severity", "?").upper(),
                issue.get("component", "?"),
                issue.get("summary", ""),
            )

        # ── Record in history ────────────────────────────────────────────────
        analysis_summary = format_analysis_for_history(analysis)
        self._add_to_history("assistant", analysis_summary, {
            "scan_id": scan_id,
            "tools_called": tool_calls_made,
        })

        # ── Add metadata ─────────────────────────────────────────────────────
        analysis["_metadata"] = {
            "scan_id": scan_id,
            "duration_sec": round(duration, 2),
            "tool_calls": tool_calls_made,
            "iterations": iteration,
        }
        
        if hindsight:
            analysis["hindsight_reflection"] = {
                "generated": True,
                "content": hindsight,
            }
            self._last_hindsight = hindsight
        else:
            analysis["hindsight_reflection"] = {"generated": False}

        return analysis

    def _merge_scopes(self, scopes: List[MCPScope]) -> MCPScope:
        """
        Merge per-MCP scopes into the single scope shown to the LLM.

        Rules:
          - Drop ``agnostic`` scopes (e.g. Prometheus) — they don't constrain.
          - If everything left is ``unknown``, return ``unknown`` honestly.
          - If any concrete namespace scope exists, the union of namespaces
            wins (least-privilege wins over any cluster-wide peer).
          - Otherwise, ``cluster``.
        """
        if not scopes:
            return MCPScope(kind="unknown", source="fallback")

        non_agnostic = [s for s in scopes if s.kind != "agnostic"]
        if not non_agnostic:
            return MCPScope(kind="unknown", source="fallback (all-agnostic)")

        known = [s for s in non_agnostic if s.kind != "unknown"]
        if not known:
            return MCPScope(kind="unknown", source="fallback (all-unknown)")

        ns_scopes = [s for s in known if s.kind in ("namespace", "namespaces")]
        if ns_scopes:
            collected: List[str] = []
            for s in ns_scopes:
                collected.extend(s.namespaces)
            distinct = list(dict.fromkeys(collected))  # order-preserving dedupe
            kind: str = "namespace" if len(distinct) == 1 else "namespaces"
            return MCPScope(kind=kind, namespaces=distinct, source="merged")

        # Only cluster-scoped MCPs known
        return MCPScope(kind="cluster", source="merged")

    def _discover_mcp_tools(
        self,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, MCPClient], Dict[str, MCPScope]]:
        """
        Discover tools and scope from each configured MCP server.

        Scope discovery uses the MCP server itself as the authority (via its
        introspection / probe tools), never the URL or any deployment-shape
        signal. ``cfg.scope_override`` short-circuits discovery when set.

        Returns:
            ``(all_tool_definitions, {url: client}, {url: scope})``.
        """
        all_tools: List[Dict[str, Any]] = []
        clients: Dict[str, MCPClient] = {}
        scopes: Dict[str, MCPScope] = {}
        override = self.cfg.scope_override or None

        for mcp_url in self.cfg.mcp_urls:
            logger.info("Discovering tools from %s", mcp_url)
            try:
                client = MCPClient(mcp_url, self.cfg.agent_name, self.cfg.mcp_timeout)
                session_id = client.initialize()
                logger.info("MCP session: %s", session_id)

                tools = client.list_tools()
                for tool in tools:
                    tool["_mcp_url"] = mcp_url  # Track which server has this tool
                all_tools.extend(tools)
                clients[mcp_url] = client

                # Discover this MCP's authorization scope from the server itself.
                try:
                    scope = client.discover_scope(tools, override=override)
                except Exception as exc:
                    logger.warning("Scope discovery failed for %s: %s", mcp_url, exc)
                    scope = MCPScope(kind="unknown", source="fallback (error)")
                scopes[mcp_url] = scope
                logger.info("MCP %s → %s", mcp_url, scope.describe())

                logger.info("Found %d tools from %s", len(tools), mcp_url)
            except Exception as exc:
                logger.error("Failed to discover tools from %s: %s", mcp_url, exc)

        return all_tools, clients, scopes

    def _execute_mcp_tool(
        self,
        clients: Dict[str, MCPClient],
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Execute a tool call via the appropriate MCP client.
        """
        # Try each client until one succeeds
        for mcp_url, client in clients.items():
            try:
                result = client.call_tool(tool_name, arguments)
                return result
            except Exception as exc:
                logger.debug("Tool %s failed on %s: %s", tool_name, mcp_url, exc)
                continue
        
        return {"error": f"Tool '{tool_name}' not found or failed on all MCP servers"}

    def _parse_analysis_response(self, content: str) -> Dict[str, Any]:
        """
        Parse LLM response into analysis dict.
        Handles JSON in markdown code blocks.
        """
        # Try direct JSON parse first
        content = content.strip()
        
        # Handle markdown code blocks
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            if end > start:
                content = content[start:end].strip()
        elif "```" in content:
            start = content.find("```") + 3
            end = content.find("```", start)
            if end > start:
                content = content[start:end].strip()
        
        return json.loads(content)

    def _get_hindsight_for_prompt(self, scan_id: str) -> Optional[str]:
        """
        Check if hindsight should be included in the prompt.
        """
        if len(self.history) < 2:
            return None
        
        # Check for warning patterns
        if not self._detect_warning_patterns():
            return None
        
        logger.info("Generating hindsight for scan %s", scan_id)
        
        failure_context = {
            "scan_number": self._scan_counter,
            "history_length": len(self.history),
        }
        
        trimmed_history = trim_history_to_token_limit(self.history, max_tokens=50000)
        
        hindsight = self.hindsight_builder.develop_hindsight(
            current_input="Starting new scan",
            history=trimmed_history,
            failure_context=failure_context,
            scan_id=scan_id,
        )
        
        return hindsight

    def _add_to_history(
        self,
        role: str,
        content: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        """
        Add an entry to the execution history.
        
        Maintains a bounded history buffer for hindsight generation.
        """
        entry = create_history_entry(role, content, metadata)
        self.history.append(entry)
        
        # Trim to max size
        if len(self.history) > self.MAX_HISTORY_SIZE:
            self.history = self.history[-self.MAX_HISTORY_SIZE:]

    def _detect_warning_patterns(self) -> bool:
        """
        Detect warning patterns in recent history that warrant hindsight.
        
        Returns:
            True if warning patterns are detected
        """
        if len(self.history) < 2:
            return False
        
        # Check last few entries for error/warning keywords
        warning_keywords = ["error", "failed", "warning", "critical", "timeout"]
        recent = self.history[-3:]
        
        warning_count = 0
        for entry in recent:
            content = str(entry.get("content", "")).lower()
            if any(kw in content for kw in warning_keywords):
                warning_count += 1
        
        # Trigger if 2+ recent entries have warnings
        return warning_count >= 2

    def get_history_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the current history state.
        
        Useful for debugging and observability.
        """
        return {
            "total_entries": len(self.history),
            "last_hindsight": self._last_hindsight[:200] if self._last_hindsight else None,
            "scan_count": self._scan_counter,
        }

    def establish_baseline(self, namespace: str) -> WatchBaseline:
        """
        Ask LLM to select watch tools and establish baseline metrics.
        
        Args:
            namespace: The Kubernetes namespace to monitor
            
        Returns:
            WatchBaseline with selected tools and initial metrics
        """
        logger.info("Establishing watch baseline for namespace: %s", namespace)

        # Discover available MCP tools (scope info ignored — namespace is given
        # explicitly by the caller in watch mode).
        mcp_tools, mcp_clients, _ = self._discover_mcp_tools()
        if not mcp_tools:
            raise RuntimeError("No MCP tools discovered - cannot establish baseline")
        
        # Format tool list for prompt
        tool_descriptions = []
        for tool in mcp_tools:
            name = tool.get("name", "")
            desc = tool.get("description", "No description")
            schema = tool.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])
            
            args_desc = ""
            if props:
                args_list = [f"{k}{'*' if k in required else ''}" for k in props.keys()]
                args_desc = f" (args: {', '.join(args_list)})"
            
            tool_descriptions.append(f"- {name}: {desc}{args_desc}")
        
        tool_list = "\n".join(tool_descriptions)
        
        # Ask LLM to select watch tools
        client = _create_openai_client(self.cfg)
        prompt = BASELINE_PROMPT.format(tool_list=tool_list, namespace=namespace)
        
        try:
            response = client.chat.completions.create(
                model=self.cfg.model_alias,
                messages=[
                    {"role": "system", "content": "You are a Kubernetes monitoring configuration assistant."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            content = response.choices[0].message.content or ""
            baseline_config = self._parse_analysis_response(content)
        except Exception as exc:
            logger.error("Failed to get baseline from LLM: %s", exc)
            # Fallback: use default tools
            baseline_config = {
                "watch_tools": [
                    {"name": "pods_list_in_namespace", "args": {"namespace": namespace}},
                ],
                "healthy_thresholds": {
                    "min_pods": 1,
                    "max_restart_delta": 0,
                    "max_pending_pods": 0,
                    "max_failed_pods": 0,
                },
            }
        
        baseline = WatchBaseline(
            namespace=namespace,
            watch_tools=baseline_config.get("watch_tools", []),
            healthy_thresholds=baseline_config.get("healthy_thresholds", {}),
            established_at=datetime.now(timezone.utc).isoformat(),
        )
        
        logger.info("Baseline tools selected: %s", [t["name"] for t in baseline.watch_tools])
        
        # Run initial tool calls to establish baseline metrics
        baseline.baseline_metrics = self._collect_watch_metrics(mcp_clients, baseline.watch_tools)
        logger.info("Baseline metrics: %s", baseline.baseline_metrics)
        
        return baseline

    def watch(
        self,
        baseline: WatchBaseline,
        poll_interval: float = 5.0,
        on_change: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
        shutdown_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        Continuous watch loop - polls tools and triggers analysis on change.
        
        Args:
            baseline: The established baseline from establish_baseline()
            poll_interval: Seconds between polls (default 5)
            on_change: Optional callback(old_metrics, new_metrics) on deviation
            shutdown_check: Optional callable returning True to stop watching
        """
        logger.info(
            "Starting watch loop | namespace=%s | interval=%.1fs | tools=%d",
            baseline.namespace, poll_interval, len(baseline.watch_tools),
        )
        
        mcp_tools, mcp_clients, _ = self._discover_mcp_tools()
        if not mcp_clients:
            logger.error("No MCP clients available for watch")
            return
        
        last_metrics = baseline.baseline_metrics.copy()
        poll_count = 0
        
        while True:
            if shutdown_check and shutdown_check():
                logger.info("Watch loop shutdown requested")
                break
            
            poll_count += 1
            poll_start = time.time()
            
            # Collect current metrics
            current_metrics = self._collect_watch_metrics(mcp_clients, baseline.watch_tools)
            
            # Check for deviation
            deviation = self._detect_deviation(
                baseline.baseline_metrics,
                last_metrics,
                current_metrics,
                baseline.healthy_thresholds,
            )
            
            poll_duration = time.time() - poll_start
            
            if deviation:
                logger.info(
                    "Watch poll #%d | %.2fs | DEVIATION: %s",
                    poll_count, poll_duration, deviation,
                )
                
                if on_change:
                    on_change(last_metrics, current_metrics)
                else:
                    # Default: run full LLM analysis
                    logger.info("Triggering full scan due to deviation")
                    self.scan(f"Analyze {baseline.namespace} - deviation detected: {deviation}")
                
                # Update baseline with new "normal" after analysis
                last_metrics = current_metrics
            else:
                logger.debug(
                    "Watch poll #%d | %.2fs | OK | pods=%s restarts=%s",
                    poll_count, poll_duration,
                    current_metrics.get("pod_count", "?"),
                    current_metrics.get("total_restarts", "?"),
                )
            
            # Sleep until next poll (interruptible)
            sleep_start = time.time()
            while time.time() - sleep_start < poll_interval:
                if shutdown_check and shutdown_check():
                    break
                time.sleep(0.5)

    def _collect_watch_metrics(
        self,
        clients: Dict[str, MCPClient],
        watch_tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Execute watch tools and extract metrics.
        
        Returns dict with standardized metrics:
            pod_count, running_pods, pending_pods, failed_pods,
            total_restarts, error_events, raw_hash
        """
        raw_outputs: List[str] = []
        metrics: Dict[str, Any] = {
            "pod_count": 0,
            "running_pods": 0,
            "pending_pods": 0,
            "failed_pods": 0,
            "total_restarts": 0,
            "error_events": 0,
            "pods": [],
        }
        
        for tool_spec in watch_tools:
            tool_name = tool_spec.get("name", "")
            tool_args = tool_spec.get("args", {})
            
            result = self._execute_mcp_tool(clients, tool_name, tool_args)
            result_text = _format_tool_result(tool_name, result)
            raw_outputs.append(result_text)
            
            # Extract metrics based on tool type
            if "pods" in tool_name.lower():
                self._extract_pod_metrics(result_text, metrics)
            elif "events" in tool_name.lower():
                self._extract_event_metrics(result_text, metrics)
        
        # Hash raw output for simple change detection
        raw_combined = "\n".join(raw_outputs)
        metrics["raw_hash"] = hashlib.md5(raw_combined.encode()).hexdigest()[:12]
        
        return metrics

    def _extract_pod_metrics(self, output: str, metrics: Dict[str, Any]) -> None:
        """Extract pod metrics from tool output."""
        lines = output.strip().split("\n")
        
        for line in lines:
            # Skip headers and empty lines
            if not line.strip() or line.startswith("NAMESPACE") or line.startswith("NAME"):
                continue
            
            parts = line.split()
            if len(parts) < 5:
                continue
            
            # Parse kubectl-style output: NAMESPACE KIND NAME READY STATUS RESTARTS AGE
            # or: NAMESPACE APIVERSION KIND NAME READY STATUS RESTARTS AGE
            try:
                # Find STATUS and RESTARTS columns
                status_idx = -1
                for i, part in enumerate(parts):
                    if part in ("Running", "Pending", "Failed", "CrashLoopBackOff", 
                               "Error", "Completed", "Terminating", "ContainerCreating"):
                        status_idx = i
                        break
                
                if status_idx > 0:
                    status = parts[status_idx]
                    # Restarts is usually after status
                    restarts_str = parts[status_idx + 1] if status_idx + 1 < len(parts) else "0"
                    restarts = int(restarts_str) if restarts_str.isdigit() else 0
                    
                    metrics["pod_count"] += 1
                    metrics["total_restarts"] += restarts
                    
                    if status == "Running":
                        metrics["running_pods"] += 1
                    elif status == "Pending" or status == "ContainerCreating":
                        metrics["pending_pods"] += 1
                    elif status in ("Failed", "Error", "CrashLoopBackOff"):
                        metrics["failed_pods"] += 1
                    
                    # Track pod names
                    pod_name = parts[3] if len(parts) > 3 else parts[0]
                    metrics["pods"].append({"name": pod_name, "status": status, "restarts": restarts})
            except (ValueError, IndexError):
                continue

    def _extract_event_metrics(self, output: str, metrics: Dict[str, Any]) -> None:
        """Extract event metrics from tool output."""
        error_patterns = ["error", "failed", "backoff", "unhealthy", "killing"]
        
        for line in output.lower().split("\n"):
            if any(pattern in line for pattern in error_patterns):
                metrics["error_events"] += 1

    def _detect_deviation(
        self,
        baseline: Dict[str, Any],
        previous: Dict[str, Any],
        current: Dict[str, Any],
        thresholds: Dict[str, int],
    ) -> Optional[str]:
        """
        Detect if current metrics deviate from healthy baseline.
        
        Returns deviation description or None if healthy.
        """
        deviations = []
        
        # Check pod count
        min_pods = thresholds.get("min_pods", 1)
        if current.get("pod_count", 0) < min_pods:
            deviations.append(f"pod_count={current['pod_count']} < min={min_pods}")
        
        # Check restart delta (compared to previous, not baseline)
        max_restart_delta = thresholds.get("max_restart_delta", 0)
        restart_delta = current.get("total_restarts", 0) - previous.get("total_restarts", 0)
        if restart_delta > max_restart_delta:
            deviations.append(f"restart_delta={restart_delta} > max={max_restart_delta}")
        
        # Check pending pods
        max_pending = thresholds.get("max_pending_pods", 0)
        if current.get("pending_pods", 0) > max_pending:
            deviations.append(f"pending_pods={current['pending_pods']} > max={max_pending}")
        
        # Check failed pods
        max_failed = thresholds.get("max_failed_pods", 0)
        if current.get("failed_pods", 0) > max_failed:
            deviations.append(f"failed_pods={current['failed_pods']} > max={max_failed}")
        
        # Check for new error events
        if current.get("error_events", 0) > previous.get("error_events", 0):
            deviations.append(f"new_error_events detected")
        
        return "; ".join(deviations) if deviations else None
