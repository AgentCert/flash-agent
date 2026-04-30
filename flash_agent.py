"""
Flash Agent – FLASH-style Workflow Automation Agent
====================================================

Implements Microsoft Research's FLASH methodology for reliable
multi-step task execution with status supervision and hindsight.

FLASH Reasoning Loop (per scan):
  1. STATUS REASONING  → Determine current system state
  2. THOUGHTS          → Generate analysis approach conditioned on status
  3. ACTION            → Call MCP tools and collect data
  4. REFLECTION        → Generate hindsight from failures for future scans

Key FLASH enhancements:
  - Status Supervision: Breaks complex tasks into status-dependent steps
  - Hindsight Integration: Learns from past failures to improve reliability

Reference: https://www.microsoft.com/en-us/research/project/flash-a-reliable-workflow-automation-agent/
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from openai import AzureOpenAI, OpenAI

from config import AgentConfig
from llm.hindsight import HindsightBuilder
from llm.utils import (
    create_history_entry,
    format_analysis_for_history,
    format_mcp_result_for_history,
    trim_history_to_token_limit,
)
from mcp.client import MCPClient, generate_fallback_data

logger = logging.getLogger("flash-agent")


# ══════════════════════════════════════════════════════════════════════════════
# LLM Configuration
# ══════════════════════════════════════════════════════════════════════════════

ANALYSIS_PROMPT = """You are a FLASH-style reasoning agent. Follow this structured reasoning process:

## STEP 1: STATUS REASONING
Determine the current system status by examining the data:
- Overall operational state: healthy/degraded/critical/unknown
- Which components are affected?
- Data completeness: complete/partial/insufficient

## STEP 2: THOUGHTS (conditioned on status)
- HEALTHY: Focus on optimization and monitoring gaps
- DEGRADED: Identify root causes, prioritize by impact
- CRITICAL: Focus on immediate issues requiring urgent action
- UNKNOWN: Note data gaps preventing assessment

## STEP 3: ANALYSIS
Produce structured findings: health metrics, issues by severity, recommendations.

## OUTPUT SCHEMA
{status_reasoning: {determined_status, status_justification, data_quality}, thoughts: {key_observations[], analysis_approach}, health: {total_pods?, healthy_pods?, unhealthy_pods?, error_count, warning_count, overall_health_score:0-100}, issues: [{severity:critical|warning|info, component, category, summary, recommended_action}], insights: {summary, concerns[], recommendations[]}}

## RULES
1. ALWAYS start with status reasoning
2. Thoughts MUST be conditioned on determined status
3. Base analysis ONLY on provided data
"""


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


def _build_llm_payload(mcp_data: Dict[str, Any]) -> str:
    """Format MCP tool results into a structured prompt for the LLM."""
    sections: List[str] = []
    
    mcp_servers = mcp_data.get("mcp_servers", [])
    tools_list = ", ".join(mcp_data.get("_mcp_data_keys", []))
    sections.append(
        f"Data source : MCP Tool Responses ({len(mcp_servers)} servers)\n"
        f"Timestamp   : {datetime.now(timezone.utc).isoformat()}\n"
        f"Tools       : {tools_list}\n"
    )

    data = mcp_data.get("data", {})
    for tool_name, tool_result in data.items():
        if isinstance(tool_result, dict) and tool_result.get("error"):
            sections.append(f"## {tool_name.upper()} (ERROR)\n{tool_result['error']}")
        else:
            result_str = json.dumps(tool_result, indent=2, default=str)
            if len(result_str) > 5000:
                result_str = result_str[:5000] + "\n... (truncated)"
            sections.append(f"## {tool_name.upper()}\n{result_str}")

    return "\n\n".join(sections)


def _request_llm_analysis(
    cfg: AgentConfig,
    mcp_data: Dict[str, Any],
    hindsight: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Send MCP data to the LLM for analysis.
    
    Args:
        cfg: Agent configuration
        mcp_data: MCP data collection result
        hindsight: Optional hindsight reflection to inject
        
    Returns:
        Parsed analysis dict or None on failure
    """
    payload_text = _build_llm_payload(mcp_data)

    if hindsight:
        prompt = (
            f"INSTRUCTIONS:\n{ANALYSIS_PROMPT}\n\n"
            f"HINDSIGHT REFLECTION (from previous analysis):\n{hindsight}\n\n"
            f"DATA TO ANALYSE:\n{payload_text}"
        )
        logger.info("Hindsight injected into analysis prompt (%d chars)", len(hindsight))
    else:
        prompt = f"INSTRUCTIONS:\n{ANALYSIS_PROMPT}\n\nDATA TO ANALYSE:\n{payload_text}"

    logger.info("Requesting LLM analysis (%d chars)", len(prompt))
    t0 = time.time()

    try:
        client = _create_openai_client(cfg)
        resp = client.chat.completions.create(
            model=cfg.model_alias,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        
        output = resp.choices[0].message.content or ""
        result = json.loads(output.strip())
        
        # Log FLASH reasoning steps
        status = result.get("status_reasoning", {})
        health = result.get("health", {})
        issues = result.get("issues", [])
        logger.info(
            "LLM analysis complete | status=%s | health=%s | issues=%d | %.2fs",
            status.get("determined_status", "?"),
            health.get("overall_health_score", "N/A"),
            len(issues),
            time.time() - t0,
        )
        return result

    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON: %s", exc)
    except Exception as exc:
        logger.error("LLM analysis failed: %s", exc)
    
    return None


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

    # ══════════════════════════════════════════════════════════════════════════
    # Public Interface
    # ══════════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════════════
    # Private: Scan Orchestration
    # ══════════════════════════════════════════════════════════════════════════

    def _execute_scan_steps(
        self,
        scan_query: str,
        scan_id: str,
        scan_start: float,
    ) -> Dict[str, Any]:
        """
        Execute FLASH reasoning loop for one scan cycle.

        FLASH Steps:
          1. Collect data from MCP servers (preparation for status reasoning)
          2. Check if hindsight reflection is needed (from past failures)
          3. Send to LLM which performs:
             - Status Reasoning (determine current state)
             - Thoughts (conditioned on status)
             - Analysis (structured output)
          4. Update history for future hindsight generation
        """
        # ── Record query in history ──────────────────────────────────────────
        self._add_to_history("user", f"Query: {scan_query}", {"scan_id": scan_id})

        # ── Step 1: MCP Data Collection (all servers) ───────────────────────
        mcp_data = self._collect_mcp_data(
            query=scan_query,
            scan_id=scan_id,
        )

        if mcp_data.get("error"):
            logger.warning(
                "MCP returned an error – analysis will reflect degraded data"
            )

        # ── Record MCP data in history ───────────────────────────────────────
        mcp_summary = format_mcp_result_for_history(mcp_data)
        self._add_to_history("tool", mcp_summary, {"mcp_servers": len(self.cfg.mcp_urls)})

        # ── Step 2: Hindsight Check (FLASH enhancement) ─────────────────────
        hindsight = self._generate_hindsight_if_needed(
            mcp_data=mcp_data,
            scan_id=scan_id,
        )

        # ── Step 3: LLM Analysis ────────────────────────────────────────────
        analysis = _request_llm_analysis(
            cfg=self.cfg,
            mcp_data=mcp_data,
            hindsight=hindsight,
        )

        if analysis is None:
            logger.error("LLM analysis failed – returning empty result")
            self._add_to_history("assistant", "Analysis failed", {"error": True})
            return {"health": {"overall_health_score": -1}, "issues": []}

        duration = time.time() - scan_start

        # ── Human-readable summary ───────────────────────────────────────────
        health = analysis.get("health", {})
        issues = analysis.get("issues", [])
        logger.info(
            "\u2550\u2550\u2550 Scan complete | scan_id=%s | %.1fs | servers=%d | "
            "health=%s | issues=%d \u2550\u2550\u2550",
            scan_id,
            duration,
            len(self.cfg.mcp_urls),
            health.get("overall_health_score", "?"),
            len(issues),
        )
        for issue in issues:
            logger.info(
                "  [%s] %s/%s — %s",
                issue.get("severity", "?").upper(),
                issue.get("affected_pod", "?"),
                issue.get("affected_container", "?"),
                issue.get("summary", ""),
            )

        # ── Record analysis in history ───────────────────────────────────────
        analysis_summary = format_analysis_for_history(analysis)
        self._add_to_history("assistant", analysis_summary, {"scan_id": scan_id})

        # ── Add hindsight metadata to result ─────────────────────────────────
        if hindsight:
            analysis["hindsight_reflection"] = {
                "generated": True,
                "content": hindsight,
                "scan_id": scan_id,
            }
            self._last_hindsight = hindsight
        else:
            analysis["hindsight_reflection"] = {"generated": False}

        return analysis

    # ══════════════════════════════════════════════════════════════════════════
    # Private: Hindsight Reflection (FLASH enhancement)
    # ══════════════════════════════════════════════════════════════════════════

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

    def _generate_hindsight_if_needed(
        self,
        mcp_data: Dict[str, Any],
        scan_id: str = "",
    ) -> str | None:
        """
        Check if hindsight should be generated and generate it.
        
        Hindsight is generated when:
        - There are errors in MCP data
        - History shows repeated issues
        - This is not the first scan (need history context)
        
        Returns:
            Hindsight string if generated, None otherwise
        """
        # Skip on first scan (no history yet)
        if len(self.history) < 2:
            return None
        
        # Check for MCP errors that warrant reflection
        data = mcp_data.get("data", {})
        has_mcp_errors = any(
            isinstance(v, dict) and v.get("error")
            for v in data.values()
        )
        
        # Check for warning patterns in recent history
        has_warning_patterns = self._detect_warning_patterns()
        
        if not (has_mcp_errors or has_warning_patterns):
            logger.debug("No hindsight needed - no errors or warning patterns")
            return None
        
        logger.info(
            "Hindsight triggered: mcp_errors=%s warning_patterns=%s",
            has_mcp_errors,
            has_warning_patterns,
        )
        
        # Build failure context
        failure_context = {
            "mcp_errors": [
                {"tool": k, "error": str(v.get("error"))}
                for k, v in data.items()
                if isinstance(v, dict) and v.get("error")
            ],
            "scan_number": self._scan_counter,
            "history_length": len(self.history),
        }
        
        # Prepare history for hindsight (with token trimming)
        trimmed_history = trim_history_to_token_limit(
            self.history,
            max_tokens=50000,
        )
        
        # Generate hindsight
        current_state = format_mcp_result_for_history(mcp_data, max_length=3000)
        hindsight = self.hindsight_builder.develop_hindsight(
            current_input=current_state,
            history=trimmed_history,
            failure_context=failure_context,
            scan_id=scan_id,
        )
        
        return hindsight

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

    # ══════════════════════════════════════════════════════════════════════════
    # Private: MCP Data Collection
    # ══════════════════════════════════════════════════════════════════════════

    def _collect_mcp_data(
        self,
        query: str,
        scan_id: str = "",
    ) -> Dict[str, Any]:
        """
        Fetch operational data from all configured MCP servers.
        
        Steps:
          1. Iterate over each MCP server URL from config
          2. Initialize session and discover available tools
          3. Call each tool with auto-populated arguments
          4. Aggregate results from all servers for LLM analysis
        """
        t0 = time.time()
        all_results: Dict[str, Any] = {}
        all_tools: List[str] = []
        errors: List[str] = []

        for mcp_url in self.cfg.mcp_urls:
            logger.info("Agent → MCP Server | url=%s", mcp_url)
            
            try:
                client = MCPClient(mcp_url, self.cfg.agent_name, self.cfg.mcp_timeout)
                session_id = client.initialize()
                logger.info("MCP session initialized | session_id=%s | url=%s", session_id, mcp_url)

                # Discover available tools
                available_tools = client.list_tools()
                tool_names = [t.get("name", "") for t in available_tools]
                logger.info("MCP tools discovered from %s: %s", mcp_url, tool_names)
                all_tools.extend(tool_names)

                # Call each discovered tool with auto-populated arguments
                for tool_def in available_tools:
                    tool_name = tool_def.get("name", "")
                    if not tool_name:
                        continue
                    
                    # Build arguments from tool's input schema
                    tool_args = self._build_tool_arguments(tool_def, query)
                    
                    try:
                        result = client.call_tool(tool_name, tool_args)
                        all_results[tool_name] = result
                        logger.info(
                            "  MCP tool '%s' → %d chars",
                            tool_name,
                            len(json.dumps(result)),
                        )
                    except Exception as exc:
                        logger.warning("  MCP tool '%s' failed: %s", tool_name, exc)
                        all_results[tool_name] = {"error": str(exc)}

            except requests.exceptions.Timeout:
                logger.error("MCP %s timed out after %ds", mcp_url, self.cfg.mcp_timeout)
                errors.append(f"Timeout: {mcp_url}")
            except requests.exceptions.ConnectionError as exc:
                logger.error("MCP %s unreachable: %s", mcp_url, exc)
                errors.append(f"Connection error: {mcp_url}")
            except requests.exceptions.HTTPError as exc:
                logger.error("MCP %s HTTP error: %s", mcp_url, exc)
                errors.append(f"HTTP error: {mcp_url}")
            except Exception as exc:
                logger.error("MCP %s error: %s", mcp_url, exc)
                errors.append(f"Error: {mcp_url}: {exc}")

        duration = time.time() - t0

        response_payload: Dict[str, Any] = {
            "mcp_servers": self.cfg.mcp_urls,
            "data": all_results,
            "_mcp_duration_sec": round(duration, 2),
            "_mcp_data_keys": list(all_results.keys()),
        }
        
        if errors:
            response_payload["error"] = errors

        logger.info(
            "MCP collection complete | %.2fs | servers=%d | tools=%s",
            duration,
            len(self.cfg.mcp_urls),
            list(all_results.keys()),
        )

        return response_payload

    def _build_tool_arguments(
        self,
        tool_def: Dict[str, Any],
        query: str,
    ) -> Dict[str, Any]:
        """
        Build tool arguments from the tool's inputSchema.
        
        Auto-populates common parameters like namespace from agent config,
        and includes the query for tools that accept it.
        """
        args: Dict[str, Any] = {}
        input_schema = tool_def.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        required = input_schema.get("required", [])
        
        for param_name, param_def in properties.items():
            param_type = param_def.get("type", "string")
            
            # Auto-populate known parameters from agent config
            if param_name in ("query", "promql", "user_query"):
                args[param_name] = query
            elif param_name == "agent_name":
                args[param_name] = self.cfg.agent_name
            elif param_name in required:
                # Required param with no known value - use default or empty
                default = param_def.get("default")
                if default is not None:
                    args[param_name] = default
                elif param_type == "boolean":
                    args[param_name] = False
                elif param_type == "integer":
                    args[param_name] = 0
                elif param_type == "array":
                    args[param_name] = []
                else:
                    args[param_name] = ""
        
        return args
