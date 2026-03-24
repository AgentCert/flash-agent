# Flash Agent v3.0.0 — Architecture Diagram

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           FLASH AGENT v3.0.0                                    │
│                     (ITOps Kubernetes Log Metrics Agent)                         │
│                                                                                 │
│   Continuous scan loop: every SCAN_INTERVAL seconds (default 120s)              │
│   Mode: "continuous" (loop) or "cronjob" (single scan if SCAN_INTERVAL=0)       │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │    agent_workflow()      │
                    │  (Root scan orchestrator)│
                    └────────────┬────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
   ┌────▼─────┐          ┌──────▼──────┐          ┌──────▼──────┐
   │  STEP 1  │          │   STEP 2    │          │   STEP 3    │
   │ Tool     │          │  MCP Data   │          │ LLM Deep    │
   │ Selection│─────────►│  Collection │─────────►│ Analysis    │
   └──────────┘          └─────────────┘          └─────────────┘
```

---

## Detailed Flow — Step by Step

### STEP 1: Tool Selection (Agent → LLM → Decision)

```
┌──────────────┐     "Which data source?"      ┌─────────────────┐
│  Flash Agent │ ─────────────────────────────► │  LLM Gateway    │
│              │     (system prompt +           │  (Azure OpenAI  │
│              │      SCAN_QUERY)               │   / OpenRouter) │
│              │ ◄───────────────────────────── │                 │
│              │     "kubernetes" or            │  MODEL_ALIAS=   │
│              │     "prometheus"               │  gpt-4.1-mini   │
└──────┬───────┘                                └─────────────────┘
       │
       │  Decision stored:
       │  ├─ Rule ② → OTL Collector span (llm-tool-selection)
       │  └─ Rule ③ → Langfuse generation (llm-tool-selection)
       ▼
```

**Function:** `agent_request_tool_selection()`

- Sends a system prompt asking the LLM to pick `kubernetes` or `prometheus`
- LLM responds with exactly one word
- Default fallback: `kubernetes`

---

### STEP 2: MCP Data Collection (Agent → MCP Server → K8s/Prom Data)

```
┌──────────────┐                                ┌─────────────────┐
│  Flash Agent │     JSON-RPC 2.0 over HTTP     │  MCP Server     │
│              │ ─────────────────────────────►  │  (Streamable    │
│              │     1. initialize (session)     │   HTTP)         │
│              │     2. tools/call (per tool)    │                 │
│              │ ◄─────────────────────────────  │  K8s: :8085     │
│              │     SSE response with data      │  Prom: :8086    │
└──────┬───────┘                                └────────┬────────┘
       │                                                  │
       │                                          ┌───────▼────────┐
       │                                          │  Kubernetes    │
       │                                          │  API / Prom    │
       │                                          │  (actual data) │
       │                                          └────────────────┘
       │
       │  MCP data stored (3 paths):
       │  ├─ Rule ①a → JSONL file (mcp_interactions.jsonl)
       │  ├─ Rule ①b → OTL Collector span (mcp-interaction)
       │  └─ Rule ③  → Langfuse span (mcp-kubernetes-request)
       ▼
```

**Function:** `agent_call_mcp_server()`

#### Phase 1 — Discovery + Chaos Resources (6 MCP tool calls):

| # | MCP Tool              | Arguments                                | Result Key           | Purpose                          |
|---|-----------------------|------------------------------------------|----------------------|----------------------------------|
| 1 | `pods_list_in_namespace` | `namespace: litmus`                   | `pods_list_in_namespace` | Pod health & status          |
| 2 | `events_list`         | `namespace: litmus`                      | `events_list`        | K8s events (warnings/errors)     |
| 3 | `pods_top`            | `namespace: litmus`                      | `pods_top`           | CPU/memory resource usage        |
| 4 | `resources_list`      | `apiVersion: litmuschaos.io/v1alpha1, kind: ChaosEngine` | `chaosengines` | Litmus ChaosEngine CRs |
| 5 | `resources_list`      | `apiVersion: litmuschaos.io/v1alpha1, kind: ChaosResult` | `chaosresults` | Litmus ChaosResult CRs |
| 6 | `resources_list`      | `apiVersion: argoproj.io/v1alpha1, kind: Workflow`        | `argo_workflows` | Argo Workflow statuses |

#### Phase 2 — Targeted Pod Logs (up to 5 additional calls):

| # | MCP Tool    | Arguments                        | Purpose                           |
|---|-------------|----------------------------------|-----------------------------------|
| 7+| `pods_log`  | `namespace, name, [container]`   | Logs from active chaos/workflow pods |

Key pods targeted:
- **chaos-exporter** — contains `FaultName=X ResultVerdict=Pass/Fail` lines
- **chaos-operator** — reconciliation events and errors
- **Argo workflow pods** — step execution logs

#### MCP JSON-RPC Protocol:

```
Agent                              MCP Server
  │                                     │
  │──── POST /mcp ─────────────────────►│
  │     { "jsonrpc": "2.0",             │
  │       "method": "initialize",       │
  │       "params": {...} }             │
  │◄──── SSE: data: {...} ─────────────│
  │      + Mcp-Session-Id header        │
  │                                     │
  │──── POST /mcp ─────────────────────►│
  │     { "method": "tools/call",       │
  │       "params": {                   │
  │         "name": "pods_list_in_namespace",
  │         "arguments": {"namespace":"litmus"}
  │       } }                           │
  │     + Mcp-Session-Id header         │
  │◄──── SSE: data: {result: ...} ─────│
  │                                     │
  │  ... repeat for each tool ...       │
```

---

### STEP 3: LLM Deep Analysis (Agent → LLM → Structured JSON)

```
┌──────────────┐                                ┌─────────────────┐
│  Flash Agent │     Structured prompt +        │  LLM Gateway    │
│              │     MCP data payload           │                 │
│              │ ─────────────────────────────►  │  Azure OpenAI / │
│              │     (~12-15K chars of:          │  OpenRouter     │
│              │      pod status, events,        │                 │
│              │      chaos verdicts, logs,      │                 │
│              │      argo workflows)            │                 │
│              │ ◄─────────────────────────────  │                 │
│              │     Structured JSON:            │                 │
│              │     {experiment_summary,        │                 │
│              │      chaos_faults,              │                 │
│              │      workflow_steps,            │                 │
│              │      workflow_errors,           │                 │
│              │      issues, health}            │                 │
└──────┬───────┘                                └─────────────────┘
       │
       │  LLM analysis stored:
       │  ├─ Rule ② → OTL Collector span (llm-analysis)
       │  └─ Rule ③ → Langfuse generation (llm-analysis)
       ▼
```

**Function:** `agent_request_llm_analysis()`

#### Data Payload Sections Sent to LLM:

```
┌─────────────────────────────────────────────────┐
│  _build_llm_data_payload()                       │
│                                                  │
│  1. POD STATUS       — counts by status          │
│  2. EVENTS           — normal/warning breakdown  │
│  3. ARGO WORKFLOWS   — names, phases, latest     │
│  4. CHAOSRESULTS     — result CR names           │
│  5. CHAOSENGINES     — engine CR names           │
│  6. CHAOS-EXPORTER   — verdict lines (CRITICAL)  │
│     LOGS               FaultName=X               │
│                        ResultVerdict=Pass/Fail    │
│                        ProbeSuccessPercentage=X   │
│  7. CHAOS-OPERATOR   — reconciliation logs       │
│     LOGS                                         │
│  8. WORKFLOW POD     — error lines only           │
│     LOGS                                         │
│  9. PRE-EXTRACTED    — regex-parsed verdicts      │
│     VERDICTS           from chaos-exporter        │
└─────────────────────────────────────────────────┘
```

#### Post-LLM Cross-Validation:

```
┌──────────────────────────────────────────────────┐
│  _cross_validate_llm_with_verdicts()             │
│                                                  │
│  chaos-exporter logs (GROUND TRUTH)              │
│       │                                          │
│       ▼                                          │
│  Compare real verdicts vs LLM output:            │
│  • Fix fault counts (passed/failed)              │
│  • Correct individual fault verdicts             │
│  • Add missing faults LLM didn't mention         │
│  • Recalculate resilience classification         │
└──────────────────────────────────────────────────┘
```

---

### STEP 3b: Workflow Step Spans (Langfuse Child Spans)

```
┌──────────────────────────────────────────────────────────────────┐
│  _record_workflow_step_spans()                                    │
│                                                                   │
│  For each of the 13 EXPECTED_WORKFLOW_STEPS:                      │
│                                                                   │
│  ✅ Step 1:  install-application                                  │
│  ✅ Step 2:  normalize-install-application-readiness              │
│  ✅ Step 3:  apply-workload-rbac                                  │
│  ✅ Step 4:  install-agent                                        │
│  ✅ Step 5:  install-chaos-faults          (parallel)             │
│  ✅ Step 6:  load-test                     (parallel)             │
│  ✅/❌ Step 7:  pod-cpu-hog       → carts (deployment)            │
│  ✅/❌ Step 8:  pod-delete        → catalogue (deployment)        │
│  ✅/❌ Step 9:  pod-network-loss  → user-db (statefulset)         │
│  ✅/❌ Step 10: pod-memory-hog    → orders (deployment)           │
│  ✅/❌ Step 11: disk-fill         → catalogue-db (statefulset)    │
│  ✅ Step 12: cleanup-chaos-resources       (parallel)             │
│  ✅ Step 13: delete-loadtest               (parallel)             │
│                                                                   │
│  Chaos steps (7-11): verdict from chaos-exporter (real data)      │
│  Infra steps (1-6, 12-13): status from Argo workflow phase        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Storage & Observability Architecture

```
                    ┌───────────────────────────────────────────────┐
                    │              FLASH AGENT                       │
                    │                                               │
                    │  ┌─────────┐  ┌──────────┐  ┌─────────────┐ │
                    │  │ Step 1  │  │ Step 2   │  │  Step 3     │ │
                    │  │ Tool    │  │ MCP Data │  │  LLM        │ │
                    │  │ Select  │  │ Collect  │  │  Analysis   │ │
                    │  └────┬────┘  └───┬──┬───┘  └──┬──────┬──┘ │
                    │       │           │  │         │      │     │
                    └───────┼───────────┼──┼─────────┼──────┼─────┘
                            │           │  │         │      │
              ┌─────────────┘    ┌──────┘  │   ┌─────┘      │
              │                  │         │   │            │
              ▼                  ▼         ▼   ▼            ▼
    ┌──────────────┐   ┌──────────────┐  ┌──────────────────────┐
    │  Rule ②      │   │  Rule ①a     │  │  Rule ③              │
    │  OTL Spans   │   │  JSONL File  │  │  Langfuse SDK        │
    │              │   │              │  │  (Direct Client)     │
    │ • llm-tool-  │   │ mcp_inter-  │  │                      │
    │   selection  │   │ actions.     │  │ • Root span          │
    │ • llm-       │   │ jsonl       │  │ • MCP request span   │
    │   analysis   │   │              │  │ • LLM generation     │
    │ • mcp-       │   │ One JSON    │  │   (tool-selection)   │
    │   interaction│   │ record per  │  │ • LLM generation     │
    │              │   │ MCP call    │  │   (analysis)         │
    └──────┬───────┘   └─────────────┘  │ • 13 workflow step   │
           │                             │   child spans        │
           │  Rule ①b                    └──────────┬───────────┘
           │  (MCP spans                            │
           │   also via OTL)                        │
           │                                        │
           ▼                                        ▼
    ┌──────────────────────────────────────────────────┐
    │                  LANGFUSE CLOUD                    │
    │             https://cloud.langfuse.com             │
    │                                                    │
    │  Session: flash-agent-litmus                       │
    │                                                    │
    │  Trace: agent-scan (scan-id)                       │
    │  ├── llm-tool-selection (generation)               │
    │  ├── mcp-kubernetes-request (span)                 │
    │  ├── llm-analysis (generation)                     │
    │  ├── ✅ Step 1: install-application                │
    │  ├── ✅ Step 2: normalize-readiness                │
    │  ├── ...                                           │
    │  ├── ❌ Step 9: pod-network-loss → Fail            │
    │  ├── ...                                           │
    │  └── ✅ Step 13: delete-loadtest                   │
    └──────────────────────────────────────────────────┘
```

---

## Langfuse Trace JSON Structure

Each scan produces a trace in Langfuse with this hierarchy:

```json
{
  "trace_id": "<deterministic from scan_id>",
  "session_id": "flash-agent-litmus",
  "name": "agent-scan (scan-id-suffix)",
  "metadata": {
    "agent": "flash-agent",
    "namespace": "litmus",
    "scan_number": 5,
    "scan_interval": 120,
    "tags": ["flash-agent", "litmus", "scan-5", "litmus-chaos"]
  },
  "output": {
    "experiment": {
      "workflow_name": "sock-shop-trace-XXXX-epoch",
      "workflow_phase": "Succeeded",
      "total_faults_executed": 5,
      "faults_passed": 3,
      "faults_failed": 2,
      "overall_resilience": "partially-resilient"
    },
    "chaos_faults_summary": [
      {"fault": "pod-cpu-hog", "verdict": "Pass", "probe_pct": "100"},
      {"fault": "pod-network-loss", "verdict": "Fail", "probe_pct": "0"}
    ],
    "health_score": 85,
    "issue_count": 3
  },
  "observations": [
    {
      "name": "llm-tool-selection",
      "type": "generation",
      "model": "gpt-4.1-mini",
      "input": [{"role": "user", "content": "...routing prompt..."}],
      "output": "kubernetes",
      "metadata": {"decision": "kubernetes", "duration_sec": 4.68}
    },
    {
      "name": "mcp-kubernetes-request",
      "type": "span",
      "input": {"server": "kubernetes", "tools": ["pods_list...", "events...", ...]},
      "output": {
        "pods": {"total": 74, "by_status": {"Running": 7, "Completed": 63}},
        "chaosresults": {"count": 21, "results": [...]},
        "argo_workflows": {"count": 9, "latest": "sock-shop-trace-..."},
        "pods_log": {"chaos-exporter-...": {"faults_reporting": [...], "latest_verdicts": {...}}}
      }
    },
    {
      "name": "llm-analysis",
      "type": "generation",
      "model": "gpt-4.1-mini",
      "input": {"prompt": "Chaos experiment analysis", "mcp_data_chars": 12000},
      "output": {
        "experiment": {...},
        "chaos_faults": [...],
        "health_score": 85
      },
      "usage": {"input": 4500, "output": 1200}
    },
    {
      "name": "✅ Step 1: install-application",
      "type": "span",
      "input": {"step_number": 1, "type": "infrastructure"},
      "output": {"phase": "Succeeded"}
    },
    {
      "name": "❌ Step 9: pod-network-loss → statefulset/user-db [Fail]",
      "type": "span",
      "level": "WARNING",
      "input": {"fault_type": "pod-network-loss", "target": "statefulset/user-db", "probe": "check-cards-access-url"},
      "output": {"verdict": "Fail", "probe_success_percentage": "0", "verdict_source": "chaos-exporter logs (real)"}
    }
  ]
}
```

---

## Scan Loop Timing

```
    ┌──────────────────────────────────────────────────────────────┐
    │                    main() entry point                         │
    │                                                              │
    │  if SCAN_INTERVAL > 0:        ← Continuous mode              │
    │    while not _shutdown:                                       │
    │      ┌────────────────────┐                                   │
    │      │  agent_workflow()  │ ← ~30-180s per scan               │
    │      └────────┬───────────┘                                   │
    │               │                                               │
    │      sleep(SCAN_INTERVAL)    ← 120s (2 minutes)               │
    │               │                                               │
    │      ┌────────▼───────────┐                                   │
    │      │  agent_workflow()  │ ← next scan                       │
    │      └────────────────────┘                                   │
    │               │                                               │
    │      ... repeats until SIGTERM/SIGINT ...                     │
    │                                                              │
    │  if SCAN_INTERVAL == 0:       ← CronJob mode                 │
    │    agent_workflow() once       (single scan, then exit)       │
    └──────────────────────────────────────────────────────────────┘
```

---

## Configuration (.env)

| Variable          | Current Value                          | Purpose                              |
|-------------------|----------------------------------------|--------------------------------------|
| `SCAN_INTERVAL`   | `120`                                  | Seconds between scans (2 min)        |
| `K8S_MCP_URL`     | `http://localhost:8085/mcp`            | Kubernetes MCP server                |
| `PROM_MCP_URL`    | `http://localhost:8086/mcp`            | Prometheus MCP server                |
| `MODEL_ALIAS`     | `gpt-4.1-mini`                         | LLM model for analysis               |
| `OPENAI_BASE_URL` | `https://agentcert.openai.azure.com`   | LLM gateway endpoint                 |
| `K8S_NAMESPACE`   | `litmus`                               | Target namespace to scan              |
| `LANGFUSE_HOST`   | `https://cloud.langfuse.com`           | Trace destination                    |

---

## Error Handling & Fallbacks

```
MCP Server unreachable?
  └── generate_mcp_fallback_data() → synthetic data → LLM still runs

LLM returns no valid JSON?
  └── analysis = None → empty result with health_score = -1
  └── workflow step spans still recorded from MCP data

LLM returns wrong schema?
  └── _schema_mismatch: true → raw result stored in trace

LLM fault verdicts wrong?
  └── _cross_validate_llm_with_verdicts() corrects from chaos-exporter ground truth
```
