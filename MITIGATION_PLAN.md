# Implementation Plan: Mitigation Capability for flash-agent

End-to-end, no half-features. Eight phases — each phase is independently shippable behind the existing config knobs, and the full sequence delivers the closed loop (observe → classify → review → act → remember → reconcile).

## Action Trichotomy

| Class | Behaviour | Examples (by verb shape) |
|---|---|---|
| **soft** | Execute anytime in mitigate mode. No review. | `list`, `get`, `describe`, `read`, `query`, `search`, `watch` |
| **hard** | 2-iteration LLM review (justify + adversarial). Both must approve. | `patch`, `update`, `scale`, `restart`, `rollout`, `apply`, `cordon`, `drain`, `exec` |
| **violent** | Never executed. Filtered out of the tool list shown to the LLM. | `delete`, `destroy`, `drop`, `purge`, `wipe`, `evict --force`, `kill --grace=0` |

Scope is enforced at execution time (not just via prompt instruction) for both detection and mitigation.

---

## Phase 0 — Foundation: config, mode toggle, audit trail

**Goal:** add wiring with zero behavior change. After Phase 0, agent runs identically but new env vars are read and an audit log is written.

**New code**
- Extend `config.py:26-72` — add fields:
  - `agent_mode: Literal["observe","mitigate"] = "observe"` (from `AGENT_MODE`, default observe = today's behavior)
  - `action_patterns_soft: list[str]`, `action_patterns_hard: list[str]`, `action_patterns_violent: list[str]` — each loaded from comma-separated env vars; ship sensible defaults as module constants
  - `mitigation_review_iters: int = 2`
  - `mitigation_audit_path: str` — JSONL audit file (from `MITIGATION_AUDIT_PATH`)
  - `memory_path: str` — JSONL memory file (from `AGENT_MEMORY_PATH`)
  - `memory_ttl_days: int = 7`
  - `reviewer_model_alias: str` — model used by Phase 4's `HardActionReviewer` (from `REVIEWER_MODEL_ALIAS`). Reuses the *same* `OPENAI_BASE_URL` and `OPENAI_API_KEY` as the main agent — only the model name differs. This is the source of uncorrelated errors between proposer and reviewer: the main agent's reasoning is filtered through `model_alias`, while review is filtered through a different model on the same endpoint. If empty, falls back to `model_alias` and logs a warning at startup (degraded mode — passes still run, but reviewer errors are correlated with the proposer's). Operators should set this to a distinct model (typically smaller/cheaper) before enabling `mitigate` mode.
- Extend `AgentConfig.validate()` to check that when `agent_mode == "mitigate"`, `scope_override` is set OR the operator has acknowledged auto-discovered scope via `MITIGATION_ALLOW_DISCOVERED_SCOPE=true` (defense against acting on a misclassified scope).
- New `policy/audit.py` — append-only JSONL writer with file lock; graceful no-op if path is empty. Same shape as `MCP_INTERACTIONS_FILE` pattern at `.env.example:39`.

**Edits**
- `flash_agent.py:160-164` — log `agent_mode` at startup.

**Acceptance:** unit test that `from_env()` parses all new vars; running with default env produces byte-identical scan output to pre-change.

---

## Phase 1 — Tool classifier + violent filtering

**Goal:** every discovered tool gets an `action_class`. Violent tools never reach the LLM.

**New code**
- `policy/__init__.py` — empty marker.
- `policy/classifier.py`:
  - `ActionClass = Literal["soft","hard","violent"]`
  - Module constants `DEFAULT_PATTERNS_SOFT/HARD/VIOLENT` — regexes on tool name/description. Conservative defaults (any mutation → at least hard; any delete/destroy/drop/purge/wipe verb → violent).
  - `classify_tool(tool_def: dict, cfg: AgentConfig) -> ActionClass` — pure function. Resolution order: operator patterns first (override), then defaults. Ambiguous → `hard` (safe-by-default).
  - `is_mutation_schema(tool_def: dict) -> bool` — secondary signal: tools whose `inputSchema` lacks a `namespace` property AND match any mutation verb → escalated to `violent` (cluster-scoped mutations are the highest-blast-radius case).

**Edits**
- `flash_agent.py:728-731` — after `tools = client.list_tools()`:
  1. Classify each tool, attach `tool["_action_class"]` (same pattern as existing `tool["_mcp_url"]` at line 730).
  2. Partition into `kept` and `filtered_violent`. Audit-log every filtered entry with `reason="violent-classified"`.
  3. Return only `kept` from `_discover_mcp_tools`.
- New helper `_classify_and_filter(tools, cfg) -> tuple[list, list]` so the logic is unit-testable independent of MCP I/O.

**Edits to prompt**
- New `_render_action_policy_block(scope, cfg, kept_tools)` — describes the policy in terms of action classes by *shape*, never tool names. Inserted into `_build_system_prompt` block list at `flash_agent.py:292-301` only when `cfg.agent_mode == "mitigate"`. Observe mode keeps today's prompt.

**Acceptance:** unit tests for `classify_tool` over a corpus of synthetic MCP tool definitions (read tools, scale tools, delete tools, ambiguous tools). Integration test confirms a tool whose name matches a violent pattern never appears in the `tools=[...]` argument to the OpenAI client.

---

## Phase 2 — Scope-enforced execution gate

**Goal:** every tool call (read OR mitigate) passes through a single gate that validates scope at execution time, not just via prompt instruction.

**New code**
- `policy/gate.py`:
  - `class ExecutionGate` — holds the merged `MCPScope`, the **per-MCP `mcp_scopes` map** (passed through from `_discover_mcp_tools` so the gate can look up each tool's originating MCP scope class), the audit logger, and policy config.
  - `gate.evaluate(tool_def, args, action_class) -> GateDecision` returning `allow | block | needs_review` with a `reason` string. Takes the full `tool_def` (not just `tool_name`) so it can read `tool["_mcp_url"]` and `tool["inputSchema"]`.
  - Decision rules (applied in order):
    1. `action_class == "violent"` → `block` (defense in depth — should never reach gate if Phase 1 worked).
    2. Look up the tool's MCP scope class: `mcp_scope_class = mcp_scopes[tool["_mcp_url"]].kind`. Then:
       - **`agnostic`** (e.g. Prometheus, alertmanager): the tool has no namespace concept; scope is embedded in the call *body* (e.g. inside the PromQL query string). The existing system prompt already instructs the LLM to pin namespace selectors inside query bodies. Allow by scope — do NOT require a `namespace` argument. Fall through to the action-class check.
       - **`namespace` / `namespaces`**: if the tool's `inputSchema` declares a `namespace` property, the call's `namespace` arg must be present AND in `scope.namespaces` → otherwise `block`. If the schema declares `namespace` but the call omits it → `block`. If the schema lacks `namespace` → `block` (genuinely out-of-scope for a namespace-scoped MCP).
       - **`cluster`**: allow by scope. Fall through to the action-class check.
       - **`unknown` AND mode == `mitigate`**: `block` (refuse to act blind).
    3. After scope passes: `action_class == "soft"` → `allow`; `action_class == "hard"` → `needs_review`.
  - Every decision is audit-logged via the Phase 0 writer, including which scope-class rule fired (so operators can debug why a Prometheus query was allowed without a `namespace` arg, or why a deployment-patch was blocked).

**Edits**
- Build a `name → tool_def` map alongside the existing `clients` dict in `_discover_mcp_tools` so the ReAct loop can resolve a `tool_call.function.name` back to the full tool def (with `_mcp_url` and `_action_class` attached). Thread this map and `mcp_scopes` into the `ExecutionGate` constructor.
- `flash_agent.py:577-598` — wrap `_execute_mcp_tool` call:
  1. Resolve `tool_def = name_to_tool[tool_name]`; read `action_class = tool_def["_action_class"]` (from Phase 1).
  2. Call `gate.evaluate(tool_def, args, action_class)`.
  3. On `block` → synthesize an MCP-shape error result (`{"isError": True, "content":[{"type":"text","text":"BLOCKED: <reason>"}]}`) and feed it back to the LLM. The LLM sees a normal tool failure and can re-plan.
  4. On `needs_review` → defer to Phase 4 (until Phase 4 ships, treat as `block`).
  5. On `allow` → execute as today.

**Acceptance:** integration tests with stub MCP returning out-of-scope namespaces; confirm gate blocks. Critical regression test: stub MCP advertising scope `agnostic` (no `namespace` param on any tool) — confirm queries are *allowed* in mitigate mode (this is the regression the gate must not introduce; otherwise enabling mitigate mode blinds detection). Test the synthesized-error path keeps the ReAct loop alive (no exceptions).

---

## Phase 3 — Soft mitigation execution

**Goal:** in `mitigate` mode, soft (low-blast) mutating tools execute end-to-end. No review needed.

**New code**
- `MitigationEpisode` dataclass in `memory/episode.py` (file created now even though full memory store comes in Phase 5 — keeps the schema in one place from day one):
  - Fields: `scan_id`, `ts`, `scope_key`, `symptom_fingerprint` (nullable in Phase 3), `normalized_component: Optional[str] = None` (the target component the action was meant to affect — populated when fingerprint is set; used by Phase 6's scoped attribution), `tool`, `args`, `action_class`, `review_iterations: list = []`, `min_observe_until_ts: float = 0.0` (epoch; computed at execution time from the action-class observation window — see Phase 6), `outcome: Literal["pending","succeeded","ineffective","regressed","ambiguous"] = "pending"`, `outcome_evidence: Optional[dict] = None`.
- Episode created inline in the ReAct loop on every successful soft execution; written to audit log immediately (memory store wires in Phase 5).

**Edits**
- After successful soft execution in `flash_agent.py:585-591`, construct and audit-log a `MitigationEpisode`.
- Extend the analysis output dict at `flash_agent.py:651-666` with `mitigations_attempted: list[MitigationEpisode-as-dict]` so downstream consumers see what the agent did.

**Acceptance:** end-to-end test with a mock MCP soft tool: agent in mitigate mode calls it, episode appears in audit log and in scan result.

---

## Phase 4 — Hard action 2-pass review

**Goal:** hard actions get reasoned through twice before executing.

**New code**
- `llm/review.py` (sibling of `llm/hindsight.py`, same structural shape):
  - `class HardActionReviewer`:
    - `__init__(cfg)` — owns its own OpenAI client built against `cfg.openai_base_url` + `cfg.openai_api_key` (same endpoint as the main agent), but pinned to `cfg.reviewer_model_alias`. The model difference is what makes the reviewer's errors uncorrelated with the proposer's reasoning — both passes use the reviewer model, not the main agent's model. If `reviewer_model_alias` is empty, log a warning at init and fall back to `cfg.model_alias` (degraded mode — review still runs but loses its uncorrelated-error property).
    - `review(tool_name, args, tool_def, symptom_context, prior_evidence: Optional[str], framing: Literal["justify","challenge"]) -> ReviewVerdict` — runs a single iteration on the reviewer model, returns `APPROVED | BLOCKED` + reasoning.
    - `review_twice(...) -> list[ReviewVerdict]`:
      - Pass 1 — `framing="justify"`: "What concrete evidence in the prior tool-call trace establishes the precondition that this action is needed?" The verdict's `evidence_used` field must be non-empty (at least one citation of a prior tool result). A justification without evidence is forcibly downgraded to `BLOCKED` regardless of the model's verdict — this catches the common failure mode of acting on a hunch.
      - Pass 2 — `framing="challenge"`: "What would have to be true about cluster state for this action to make things worse, and is the trace consistent with that?"
      - Both must return `APPROVED` to proceed. Either `BLOCKED` (including the forced downgrade above) blocks execution.
  - Review prompts describe action classes by *shape*, never names. Output format is strict JSON (mirrors how analysis JSON is parsed at `flash_agent.py:769-789`).
- Verdict dataclass: `{verdict, reasoning, evidence_used: list[str], iteration: int, framing: str, model: str}` — `model` records which model alias actually produced the verdict so audit logs are unambiguous about whether the reviewer ran in primary or degraded mode.

**Edits**
- `policy/gate.py` — `needs_review` decisions now actually call `HardActionReviewer.review_twice()`.
- Review verdicts are appended to the `MitigationEpisode.review_iterations` field.
- If any iteration returns `BLOCKED`: synthesize an MCP-shape error result, log episode with `outcome="blocked-by-review"` (extend outcome enum), feed back to LLM.
- If both `APPROVED`: execute, audit-log full review trace.

**Acceptance:** test that hard tool calls invoke the reviewer twice; test that a `BLOCKED` first iteration prevents execution; test that a justify-pass verdict with empty `evidence_used` is forcibly downgraded to `BLOCKED`; test that audit-log verdicts record `model=cfg.reviewer_model_alias` when set and `model=cfg.model_alias` plus a `degraded=true` flag when unset; test the failure mode where the reviewer itself errors (treat as `BLOCKED` — fail closed).

---

## Phase 5 — Episode store + fingerprinting

**Goal:** episodes persist across scans and across pod restarts.

**New code**
- `memory/__init__.py` — marker.
- `memory/fingerprint.py`:
  - `fingerprint_issue(issue: dict, scope_key: str) -> str` — deterministic hash over `(category, normalized_component, severity, scope_key)`. Normalization: lowercase, collapse digits in pod names to `*` (so `cataloguedb-7f8d → cataloguedb-*`).
  - `fingerprint_tool_call(tool_name, args, scope) -> str` — to dedupe near-identical proposals.
- `memory/store.py`:
  - `class MemoryStore` — abstract: `append(episode)`, `find_by_fingerprint(fp, limit)`, `find_pending(scope_key)`, `update(episode)`.
  - `class FileMemoryStore(MemoryStore)` — JSONL backend at `cfg.memory_path`, file-locked appends, in-memory index built at load.
  - `class InMemoryStore(MemoryStore)` — fallback when path unset; same interface.
  - `MemoryStore.from_config(cfg)` factory.
  - TTL enforcement on read (`memory_ttl_days`) — old entries filtered, not deleted in-place (separate compaction step).
  - Schema version field on every record; mismatch → log warning + skip record.

**Edits**
- `FlashAgent.__init__` — instantiate `MemoryStore` once.
- Phase 3's episode-writing code now writes through `MemoryStore.append`.

**Acceptance:** roundtrip test (write episode, restart store, read back); concurrent-write test with two threads; TTL filtering test; schema-version-mismatch test.

---

## Phase 6 — Outcome reconciliation

**Goal:** each scan looks back at the previous scan's pending episodes and decides whether the mitigation worked.

**New code**
- `memory/reconciler.py`:
  - Per-action-class observation windows — constants in the reconciler (overridable via env):
    - pod-level mutations (`restart`, `delete-pod`, `kill`): 90s — covers restart + readiness probe
    - `scale`: 120s — new pods need to come up and pass readiness
    - `patch` / `apply` (Deployment / ConfigMap rollouts): 300s — rolling update + PDB-respecting pod replacement
    - `cordon` / `drain`: 180s — pod migration honoring `terminationGracePeriodSeconds`
    - default soft action: 60s
  - At execution time (Phase 3's episode-creation site), set `episode.min_observe_until_ts = episode.ts + window_for(action_class, tool)`. Window selection is by `tool["_action_class"]` plus a small lookup table of verb-shape hints in the tool name (already extracted by Phase 1's classifier).
  - `reconcile(store, current_issues, scope_key, now: float) -> list[updated_episode]`:
    1. Load `find_pending(scope_key)`.
    2. Build current-fingerprint set from `current_issues`, indexed by `normalized_component` (so the reconciler can ask "is *this component* still in trouble?" without scanning the whole list).
    3. For each pending episode:
       - **Window not elapsed** (`now < episode.min_observe_until_ts`) → leave as `pending`, skip. The action hasn't had time to take effect (e.g. a rolling restart still in progress, an HPA inside its scale-down stabilization window, a ConfigMap patch waiting for the next pod recycle). This episode will be re-evaluated on a later scan.
       - **Target fingerprint absent from current issues** → `succeeded`.
       - **Target fingerprint still present** → `ineffective`.
       - **Target fingerprint absent, but a new high-severity fingerprint exists on the SAME `normalized_component`** → `regressed`. Causal attribution is scoped to the targeted component; new fingerprints on *unrelated* components are NOT attributed to this episode.
       - **Target fingerprint absent, no related regression, but a *different-category* fingerprint appeared on the same component** (e.g. `crashloop` → `readiness-fail`) → `ambiguous`. Both old and new fingerprints are recorded in `outcome_evidence` so Phase 7's playbook can weight ambiguous outcomes differently from clean successes or clean failures.
    4. `store.update(...)` each non-pending transition.
    5. Return reconciled list for inclusion in this scan's output.

**Edits**
- `flash_agent.py:484-487` — call reconciliation at the **start** of a scan, against the *previous* scan's persisted fingerprint set. Each scan, at completion, writes its final issue fingerprints to the memory store keyed by `(scope_key, scan_id)`. The reconciler reads the most recent prior scan's fingerprints for this scope as `current_issues` input. This avoids splitting the ReAct loop and means Phase 7's playbook is built from already-reconciled evidence before any new mitigation is proposed.
- Window-not-elapsed episodes accumulate as `pending` across multiple scans and are reconciled on whichever later scan first satisfies `now >= min_observe_until_ts` — so a 300s ConfigMap patch issued mid-cycle gets judged on the scan ~5 minutes later, not the one immediately after.

**Acceptance:**
- 3-scan sequence with a soft action whose window is 60s and scans 90s apart: scan 1 detects issue, scan 1 mitigates (episode `pending`, `min_observe_until_ts = t+60`), scan 2 reconciles → `succeeded` if issue gone.
- Same as above but scans only 30s apart: scan 2 must leave the episode `pending` (window not elapsed); scan 3 reconciles.
- Scale-up action: target component still has the *same* issue at scan 2 because new pods are still `Pending` → episode stays `pending` while within the 120s window, then transitions to `succeeded` or `ineffective` on the next scan after the window elapses.
- New unrelated high-severity issue on a different component appears at scan 2 — confirm the prior scan's episode does NOT transition to `regressed`.
- Same component flips from crashloop to readiness-fail — confirm `ambiguous` outcome with both fingerprints in `outcome_evidence`.

---

## Phase 7 — Playbook builder + prompt injection

**Goal:** prior outcomes inform future decisions.

**New code**
- `llm/playbook.py` (parallel to `llm/hindsight.py`):
  - `class PlaybookBuilder`:
    - `summarize_for(symptom_fp: str, store: MemoryStore, max_episodes=10) -> str` — compact human-readable roll-up: per-tool-shape success rate. Returns empty string if no evidence.
    - `summarize_for_review(tool_fp, store) -> str` — prior-evidence string for the hard-action reviewer (Phase 4 takes `prior_evidence` parameter — wired here).

**Edits**
- New `_render_playbook_block(scope, current_issues, store, cfg)` — only in mitigate mode, only when there are issues. Inserted into `_build_system_prompt` block list at `flash_agent.py:292-301`.
- `HardActionReviewer.review_twice` is now called with non-empty `prior_evidence` — wires Phase 4 ↔ Phase 7.

**Acceptance:** populate store with synthetic episodes for a fingerprint; confirm playbook block contains them; confirm reviewer receives them.

---

## Phase 8 — Observability, tests, rollout

**Goal:** ship-ready quality.

**New code**
- `tests/` directory (none exists today):
  - `tests/test_classifier.py`
  - `tests/test_gate.py`
  - `tests/test_review.py` (mocked LLM)
  - `tests/test_memory.py`
  - `tests/test_reconciler.py`
  - `tests/test_fingerprint.py`
  - `tests/integration/test_mitigate_flow.py` — full scan against stub MCP + stub LLM. Covers: observe mode unchanged, soft action executed, hard action reviewed-and-approved, hard action reviewed-and-blocked, violent action filtered, out-of-scope call blocked, episode outcomes reconciled.
- pytest config in `pyproject.toml` or `pytest.ini`; add `pytest`, `pytest-asyncio`, `responses` to `requirements.txt` (dev section).

**Edits**
- Extend logging at key checkpoints (audit-log entries should also be summarized at `INFO`).
- Update `.env.example` with all new vars and concise comments.
- Update `README.md` and `FUNCTIONING.md` — new "Mitigation" section explaining the trichotomy, scope enforcement, and memory model.

**Rollout sequence (production)**
1. Deploy with `AGENT_MODE=observe` (default) — all new code present, no behavior change. Audit log fills up with classifications/episodes-would-have-been so operators can review the classifier on real traffic.
2. After classifier review, enable `AGENT_MODE=mitigate` in non-prod with explicit `AGENT_SCOPE_NAMESPACE=<staging-ns>`.
3. Promote to prod namespace-by-namespace.

---

## File layout (final)

```
flash-agent/
├── main.py                    (small edits)
├── config.py                  (extended)
├── flash_agent.py             (edits at known line ranges)
├── .env.example               (new vars)
├── requirements.txt           (add pytest, responses)
├── llm/
│   ├── hindsight.py           (unchanged)
│   ├── utils.py               (unchanged)
│   ├── review.py              (NEW — Phase 4)
│   └── playbook.py            (NEW — Phase 7)
├── mcp/
│   └── client.py              (unchanged)
├── policy/                    (NEW directory)
│   ├── __init__.py
│   ├── classifier.py          (Phase 1)
│   ├── gate.py                (Phase 2)
│   └── audit.py               (Phase 0)
├── memory/                    (NEW directory)
│   ├── __init__.py
│   ├── episode.py             (Phase 3, dataclass)
│   ├── fingerprint.py         (Phase 5)
│   ├── store.py               (Phase 5)
│   └── reconciler.py          (Phase 6)
└── tests/                     (NEW directory, Phase 8)
    ├── test_classifier.py
    ├── test_gate.py
    ├── test_review.py
    ├── test_memory.py
    ├── test_reconciler.py
    ├── test_fingerprint.py
    └── integration/
        └── test_mitigate_flow.py
```

---

## Memory model summary

Three layers, building on the existing `HindsightBuilder` pattern:

1. **Episodic trace** — structured `MitigationEpisode` records (one per executed action). Persisted JSONL.
2. **Outcome reconciliation** — at each scan start, look at last scan's pending episodes; update status against current fingerprints (succeeded / ineffective / regressed).
3. **Distilled playbook** — `PlaybookBuilder` rolls up episodes by symptom fingerprint into compact evidence strings. Injected into system prompt AND into the hard-action reviewer's prompt.

Three invariants that must hold:
- **Scope partitioning** — every episode tagged with `scope_key`; reads filtered by it. No cross-namespace contamination.
- **Fingerprint stability** — derived from structured issue dict (`category` + normalized `component`), not freeform `summary`. Otherwise reconciliation always reports `ineffective`.
- **TTL + caps** — `MEMORY_TTL_DAYS` (default 7) and per-fingerprint episode cap (20). Schema version field on every record.

---

## Code standards (enforced across all phases)

These match the conventions already in the codebase:

- `from __future__ import annotations` at the top of every new module.
- Module docstring with `===` underline, brief purpose + reference if applicable (same style as `llm/hindsight.py:1-9`).
- `logger = logging.getLogger("flash-agent")` — single logger name across the package.
- Type hints on every public function signature; `Literal[...]` for enumerated strings (matching `ScopeKind` at `mcp/client.py:28`).
- Dataclasses with `field(default_factory=...)` for mutable defaults.
- Private helpers prefixed with `_`.
- No emojis, no decorative comments. Comments only for *why*, not *what*.
- Tool selection by `inputSchema` shape — never by tool name. Same rule applies to the classifier's defaults (use verb regexes, not full names).
- Graceful degradation: every external call (LLM, MCP, file I/O) wrapped — failures fall back to safe state and log at warning. The reviewer fails closed (`BLOCKED`); the store falls back to in-memory; the classifier defaults ambiguous to `hard`.
- No backwards-compat shims. When a method signature changes (e.g., `_execute_mcp_tool` becoming gate-aware), update all callers in the same commit.
- No silent feature flags inside code — everything mode-gated via `cfg.agent_mode` read once at the top of each scan.

---

## Explicitly out of scope (so it isn't surprising later)

- Multi-agent coordination / shared memory across agent instances on the same namespace (Phase 5's file lock supports it, but no merge/conflict logic).
- Automatic rollback of hard actions when they regress. Memory will *flag* regressions; no auto-undo. That's a separate design.
- LLM-based dynamic re-classification of tools at runtime (e.g., asking the LLM to classify an ambiguous tool). The classifier is purely pattern + schema. If operators want LLM classification, they can run it offline and feed results in as `ACTION_PATTERNS_*` overrides.
- Tool-level approval-by-human-in-the-loop. The 2-iteration LLM review *is* the review mechanism; no Slack-bot/webhook approval gate.

---

## Commit sequence

One commit per phase, each commit green on its own:

1. `feat(config): add mitigation mode toggle and audit log scaffolding`
2. `feat(policy): tool classifier with violent filtering`
3. `feat(policy): scope-enforced execution gate`
4. `feat(mitigate): soft action execution path`
5. `feat(mitigate): hard action two-pass reviewer`
6. `feat(memory): persistent episode store with fingerprinting`
7. `feat(memory): outcome reconciliation pass`
8. `feat(memory): playbook builder and prompt injection`
9. `test: integration tests and rollout docs`

Each commit edits the existing files at the precise line ranges identified above, adds the new files for that phase, and ships tests for the new behavior in the same commit.

---

## Environment variables introduced

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODE` | `observe` | `observe` (today's behavior) or `mitigate` |
| `AGENT_SCOPE_NAMESPACE` | `` | Existing — explicit scope override |
| `MITIGATION_ALLOW_DISCOVERED_SCOPE` | `false` | Permit `mitigate` mode with auto-discovered (not explicit) scope |
| `MITIGATION_REVIEW_ITERS` | `2` | Number of independent review passes for hard actions |
| `MITIGATION_AUDIT_PATH` | `` | JSONL audit log path; empty = no-op |
| `REVIEWER_MODEL_ALIAS` | `` | Model used by Phase 4 `HardActionReviewer`; reuses `OPENAI_BASE_URL` + `OPENAI_API_KEY`. Empty = degrade to `MODEL_ALIAS` and log a warning at startup. Set to a distinct model (typically smaller/cheaper) so reviewer errors are uncorrelated with the proposer. |
| `ACTION_PATTERNS_SOFT` | (defaults) | Comma-separated regexes — overrides defaults |
| `ACTION_PATTERNS_HARD` | (defaults) | Comma-separated regexes — overrides defaults |
| `ACTION_PATTERNS_VIOLENT` | (defaults) | Comma-separated regexes — overrides defaults |
| `AGENT_MEMORY_PATH` | `` | JSONL episode store; empty = in-memory only |
| `MEMORY_TTL_DAYS` | `7` | Episode TTL on read |
