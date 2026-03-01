"""
Flash Agent - ITOps Kubernetes Log Metrics Agent

A lightweight agent that:
1. Collects pod logs from a configured Kubernetes namespace
2. Analyzes logs using an LLM (via OpenAI-compatible API) to extract operational metrics
3. Reports traces and metrics via OpenTelemetry (OTLP)
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kubernetes import client, config
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("flash-agent")

# ---------------------------------------------------------------------------
# Configuration (populated from env vars set by the Helm chart)
# ---------------------------------------------------------------------------

AGENT_NAME = os.getenv("AGENT_NAME", "flash-agent")
AGENT_MODE = os.getenv("AGENT_MODE", "active")
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "default")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "gpt-41")

# OpenTelemetry (configure via standard OTEL_* env vars or these)
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_EXPORTER_OTLP_HEADERS = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")

# Metrics
TRACE_TAGS = [t.strip() for t in os.getenv("TRACE_TAGS", "flash-agent").split(",") if t.strip()]
LOG_TAIL_LINES = int(os.getenv("LOG_TAIL_LINES", "200"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # seconds between scans (0 = run once)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s – shutting down gracefully", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Kubernetes helpers
# ---------------------------------------------------------------------------


def init_k8s_client() -> client.CoreV1Api:
    """Initialise the Kubernetes API client (in-cluster or kubeconfig)."""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded kubeconfig from default location")
    return client.CoreV1Api()


def collect_pod_logs(v1: client.CoreV1Api, namespace: str, tail_lines: int = 200) -> List[Dict[str, Any]]:
    """Collect the last *tail_lines* of logs from every pod in *namespace*."""
    results: List[Dict[str, Any]] = []
    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
    except client.exceptions.ApiException as exc:
        logger.error("Failed to list pods in %s: %s", namespace, exc.reason)
        return results

    for pod in pods.items:
        pod_name = pod.metadata.name
        pod_status = pod.status.phase
        for container in pod.spec.containers:
            try:
                log_text = v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    container=container.name,
                    tail_lines=tail_lines,
                )
            except client.exceptions.ApiException:
                log_text = ""
            results.append({
                "pod": pod_name,
                "container": container.name,
                "namespace": namespace,
                "status": pod_status,
                "logs": log_text or "(no logs)",
            })
    logger.info("Collected logs from %d containers in namespace %s", len(results), namespace)
    return results


def collect_pod_events(v1: client.CoreV1Api, namespace: str) -> List[Dict[str, str]]:
    """Collect recent Kubernetes events for the namespace."""
    events: List[Dict[str, str]] = []
    try:
        event_list = v1.list_namespaced_event(namespace=namespace)
        for ev in event_list.items[-50:]:  # last 50
            events.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message or "",
                "object": f"{ev.involved_object.kind}/{ev.involved_object.name}",
                "time": str(ev.last_timestamp or ev.event_time or ""),
            })
    except client.exceptions.ApiException as exc:
        logger.warning("Could not fetch events: %s", exc.reason)
    return events


# ---------------------------------------------------------------------------
# LLM helpers (OpenAI-compatible API)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert IT-Operations analyst. You receive Kubernetes pod logs and events.
Your task:
1. Identify any errors, warnings, anomalies, or performance issues.
2. For each issue found, extract:
   - severity (critical / warning / info)
   - affected_pod
   - affected_container
   - category (one of: CrashLoop, OOM, ImagePull, Connectivity, Latency, ErrorRate, ConfigError, HealthCheck, ResourcePressure, Other)
   - summary (one sentence)
   - recommended_action (one sentence)
3. Produce overall health metrics:
   - total_pods, healthy_pods, unhealthy_pods
   - error_count, warning_count
   - overall_health_score (0-100)

Return ONLY valid JSON with keys: {"issues": [...], "health": {...}}"""


def get_openai_client() -> OpenAI:
    """Create an OpenAI client configured for the LLM API."""
    return OpenAI(
        api_key=OPENAI_API_KEY or "not-needed",
        base_url=OPENAI_BASE_URL,
        timeout=120.0,
    )


def call_llm(logs_payload: str) -> Optional[Dict[str, Any]]:
    """Send logs to LLM API and parse the JSON response."""
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=MODEL_ALIAS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": logs_payload},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# OpenTelemetry tracing
# ---------------------------------------------------------------------------

_tracer: Optional[trace.Tracer] = None


def init_tracing() -> Optional[trace.Tracer]:
    """Initialise OpenTelemetry tracing if OTLP endpoint is configured."""
    if not OTEL_EXPORTER_OTLP_ENDPOINT:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set – tracing disabled")
        return None

    try:
        # Parse headers from env var (format: "key1=value1,key2=value2")
        headers = {}
        if OTEL_EXPORTER_OTLP_HEADERS:
            for pair in OTEL_EXPORTER_OTLP_HEADERS.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()

        resource = Resource.create({
            SERVICE_NAME: AGENT_NAME,
            "service.version": "1.0.0",
            "deployment.environment": AGENT_MODE,
        })

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
            headers=headers if headers else None,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        logger.info("OpenTelemetry tracing initialised (endpoint=%s)", OTEL_EXPORTER_OTLP_ENDPOINT)
        return trace.get_tracer(AGENT_NAME)
    except Exception as exc:
        logger.warning("OpenTelemetry init failed: %s", exc)
        return None


def report_trace(
    tracer: trace.Tracer,
    analysis: Dict[str, Any],
    namespace: str,
    duration_sec: float,
    pod_count: int,
):
    """Create an OpenTelemetry trace with the analysis results."""
    health = analysis.get("health", {})
    issues = analysis.get("issues", [])

    with tracer.start_as_current_span(
        f"{AGENT_NAME}/scan",
        attributes={
            "agent.name": AGENT_NAME,
            "k8s.namespace": namespace,
            "pod.count": pod_count,
            "scan.duration_sec": round(duration_sec, 2),
            "tags": ",".join(TRACE_TAGS),
        },
    ) as scan_span:
        # Add health metrics as span attributes
        scan_span.set_attribute("health.score", health.get("overall_health_score", -1))
        scan_span.set_attribute("health.total_pods", health.get("total_pods", 0))
        scan_span.set_attribute("health.healthy_pods", health.get("healthy_pods", 0))
        scan_span.set_attribute("health.unhealthy_pods", health.get("unhealthy_pods", 0))
        scan_span.set_attribute("health.error_count", health.get("error_count", 0))
        scan_span.set_attribute("health.warning_count", health.get("warning_count", 0))
        scan_span.set_attribute("issue.count", len(issues))

        # Child span for LLM analysis
        with tracer.start_as_current_span(
            "log-analysis",
            attributes={
                "llm.model": MODEL_ALIAS,
                "llm.health_score": health.get("overall_health_score", -1),
                "llm.issue_count": len(issues),
            },
        ):
            pass  # LLM call already completed

        # Add events for each issue detected
        for idx, issue in enumerate(issues):
            scan_span.add_event(
                f"issue-{idx}",
                attributes={
                    "severity": issue.get("severity", "unknown"),
                    "affected_pod": issue.get("affected_pod", "unknown"),
                    "affected_container": issue.get("affected_container", "unknown"),
                    "category": issue.get("category", "Other"),
                    "summary": issue.get("summary", ""),
                    "recommended_action": issue.get("recommended_action", ""),
                },
            )

    logger.info(
        "Trace exported: health_score=%s, issues=%d",
        health.get("overall_health_score", "N/A"),
        len(issues),
    )


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------


def run_scan(v1: client.CoreV1Api, tracer: Optional[trace.Tracer]) -> Dict[str, Any]:
    """Execute a single scan cycle."""
    start = time.time()
    logger.info("=== Scan started for namespace '%s' ===", K8S_NAMESPACE)

    # 1. Collect data
    pod_logs = collect_pod_logs(v1, K8S_NAMESPACE, tail_lines=LOG_TAIL_LINES)
    events = collect_pod_events(v1, K8S_NAMESPACE)

    if not pod_logs:
        logger.warning("No pods found in namespace %s – skipping analysis", K8S_NAMESPACE)
        return {"health": {"overall_health_score": 100, "total_pods": 0}, "issues": []}

    # 2. Build payload for LLM
    payload_parts = [f"=== Kubernetes Namespace: {K8S_NAMESPACE} ===\n"]
    for entry in pod_logs:
        payload_parts.append(
            f"\n--- Pod: {entry['pod']} | Container: {entry['container']} | Status: {entry['status']} ---\n"
            f"{entry['logs'][:4000]}\n"  # truncate per container to stay within context
        )
    if events:
        payload_parts.append("\n=== Recent Events ===\n")
        for ev in events:
            payload_parts.append(f"[{ev['type']}] {ev['reason']}: {ev['message']} ({ev['object']})\n")

    logs_payload = "".join(payload_parts)

    # 3. Analyse with LLM
    logger.info("Sending %d chars to LLM for analysis …", len(logs_payload))
    analysis = call_llm(logs_payload)

    duration = time.time() - start

    if analysis is None:
        logger.error("LLM analysis failed – skipping trace export")
        return {"health": {"overall_health_score": -1}, "issues": []}

    # 4. Export trace
    if tracer:
        report_trace(tracer, analysis, K8S_NAMESPACE, duration, len(pod_logs))

    # 5. Log summary
    health = analysis.get("health", {})
    issues = analysis.get("issues", [])
    logger.info(
        "Scan complete in %.1fs – health_score=%s, issues=%d, pods=%d",
        duration,
        health.get("overall_health_score", "?"),
        len(issues),
        len(pod_logs),
    )
    for issue in issues:
        logger.info(
            "  [%s] %s/%s – %s",
            issue.get("severity", "?").upper(),
            issue.get("affected_pod", "?"),
            issue.get("affected_container", "?"),
            issue.get("summary", ""),
        )

    return analysis


def main():
    logger.info("Flash Agent v1.0.0 starting – agent=%s, namespace=%s, model=%s", AGENT_NAME, K8S_NAMESPACE, MODEL_ALIAS)

    v1 = init_k8s_client()
    tracer = init_tracing()

    if SCAN_INTERVAL <= 0:
        # Run once (CronJob mode)
        run_scan(v1, tracer)
    else:
        # Continuous loop (Deployment mode)
        logger.info("Running in continuous mode – scan every %ds", SCAN_INTERVAL)
        while not _shutdown:
            try:
                run_scan(v1, tracer)
            except Exception as exc:
                logger.exception("Scan failed: %s", exc)
            # Wait, but check for shutdown every second
            for _ in range(SCAN_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)

    # Flush any pending spans
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
    logger.info("Flash Agent shutting down")


if __name__ == "__main__":
    main()
