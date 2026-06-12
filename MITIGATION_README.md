# Flash Agent — Mitigation Capability

Closed-loop remediation built on top of the existing FLASH-style scan loop.
Eight phases delivered end-to-end (see [MITIGATION_PLAN.md](MITIGATION_PLAN.md)).

---

## 1. The big picture

```
                    ┌──────────────────────────────────────────────────────┐
                    │                    FlashAgent.scan()                 │
                    └──────────────────────────────────────────────────────┘
                                            │
        ┌───────────────────────────────────┼───────────────────────────────────┐
        ▼                                   ▼                                   ▼
┌───────────────┐                  ┌───────────────┐                  ┌───────────────┐
│   DISCOVER    │                  │ REASON + ACT  │                  │  ANALYZE +    │
│   tools+scope │ ────────────────▶│  (ReAct loop) │ ────────────────▶│  RECONCILE    │
└───────────────┘                  └───────────────┘                  └───────────────┘
        │                                   │                                   │
        │                                   │                                   │
        ▼                                   ▼                                   ▼
  ┌──────────┐                      ┌──────────────┐                  ┌────────────────┐
  │CLASSIFIER│  → soft/hard/violent │ EXECUTION    │ → block / allow /│   RECONCILER   │
  │ (Phase 1)│  → drops violent     │ GATE (P2)    │   needs_review   │   (Phase 6)    │
  └──────────┘                      └──────────────┘                  └────────────────┘
                                            │                                   │
                                            │ on needs_review                   │
                                            ▼                                   │
                                    ┌──────────────┐                            │
                                    │  REVIEWER    │ ── 2-pass justify+challenge│
                                    │  (Phase 4)   │ ── both APPROVE → execute  │
                                    └──────────────┘                            │
                                            │                                   │
                                            ▼                                   │
                                    ┌──────────────┐                            │
                                    │   EPISODE    │ ── append to ──────────────┘
                                    │ (Phase 3+5)  │    MemoryStore (JSONL)
                                    └──────────────┘
                                            │
                                            ▼
                                    ┌──────────────┐
                                    │  PLAYBOOK    │ ── injected into next scan's
                                    │  (Phase 7)   │    system prompt + reviewer
                                    └──────────────┘
```

---

## 2. The action trichotomy

Every MCP tool is bucketed by *verb shape*, never by name. The classifier
runs once at discovery; the gate enforces at execution time.

```
                       ┌────────────────────────────────────────────┐
                       │             TOOL CLASSIFIED AS             │
                       └────────────────────────────────────────────┘
                                          │
       ┌──────────────────────────────────┼──────────────────────────────────┐
       ▼                                  ▼                                  ▼
  ┌──────────┐                       ┌──────────┐                       ┌──────────┐
  │   SOFT   │                       │   HARD   │                       │ VIOLENT  │
  │          │                       │          │                       │          │
  │ list,get │                       │ patch,   │                       │ delete,  │
  │ describe │                       │ update,  │                       │ destroy, │
  │ query,   │                       │ scale,   │                       │ drop,    │
  │ watch,   │                       │ restart, │                       │ purge,   │
  │ fetch    │                       │ apply,   │                       │ wipe,    │
  │          │                       │ cordon,  │                       │ evict    │
  │          │                       │ drain    │                       │ --force  │
  └──────────┘                       └──────────┘                       └──────────┘
       │                                  │                                  │
       ▼                                  ▼                                  ▼
  EXECUTE freely               2-PASS REVIEW required                FILTER OUT
  (in mitigate mode)          (justify + challenge)             (never sent to LLM)
                              both must APPROVE
```

**Safety rails (defense in depth):**
- Violent tools are removed from the OpenAI `tools=[...]` argument entirely.
- Cluster-scoped mutations (a hard verb on a schema with no `namespace`
  property) are escalated to violent automatically.
- Ambiguous tools default to **hard** — they refuse to execute without review.

---

## 3. Execution gate decision tree

```
          ┌─────────────────────────┐
          │ gate.evaluate(tool,args)│
          └─────────────────────────┘
                       │
                       ▼
              ╔═════════════════╗
              ║ action_class    ║─── violent ──▶  BLOCK (rule: violent-block)
              ╚═════════════════╝
                       │ soft / hard
                       ▼
              ╔═════════════════╗
              ║ MCP scope kind  ║─── agnostic ─▶  (no scope constraint)
              ╚═════════════════╝                       │
                       │ namespace / cluster / unknown  │
                       ▼                                ▼
        ┌──────────────────────────┐         ┌──────────────────┐
        │  namespace-scoped MCP    │         │  cluster MCP →   │
        │                          │         │  fall through    │
        │ • schema has `namespace`?│         └──────────────────┘
        │ • args contain it?       │                  │
        │ • is it in allowed set?  │                  │
        └──────────────────────────┘                  │
            │block on any failure│                    │
            └──────┬─────────────┘                    │
                   │                                   │
                   ▼                                   ▼
        ╔═════════════════════════╗
        ║ action_class dispatch   ║
        ╠═════════════════════════╣
        ║ soft  → ALLOW           ║
        ║ hard  → NEEDS_REVIEW    ║  (Phase 4 takes over)
        ║       (in observe mode →║
        ║        BLOCK)           ║
        ╚═════════════════════════╝
```

Every decision is audit-logged with the rule that fired, so operators can
trace exactly why a call was let through or blocked.

---

## 4. Two-pass adversarial review

Hard actions never execute without the reviewer model (a *different* model
on the same OpenAI endpoint) approving twice.

```
   ┌─────────────────────────────┐
   │  HARD action proposed       │
   │  e.g. scale_dep(replicas=5) │
   └─────────────────────────────┘
                  │
                  ▼
   ┌─────────────────────────────────────────────────┐
   │ PASS 1 — JUSTIFY                                │
   │ "What concrete evidence in the trace            │
   │  establishes the precondition for this action?" │
   └─────────────────────────────────────────────────┘
                  │
       ┌──────────┴──────────┐
       ▼                     ▼
   APPROVED               BLOCKED  ─────────▶ EXECUTION BLOCKED
   + evidence_used                          (episode outcome:
       │                                     blocked-by-review)
       ▼
   (if evidence_used == [] → FORCED DOWNGRADE → BLOCKED)
       │
       ▼
   ┌─────────────────────────────────────────────────┐
   │ PASS 2 — CHALLENGE                              │
   │ "What would have to be true about cluster state │
   │  for this action to make things worse, and is   │
   │  the trace consistent with that scenario?"      │
   └─────────────────────────────────────────────────┘
                  │
       ┌──────────┴──────────┐
       ▼                     ▼
   APPROVED               BLOCKED  ─────────▶ EXECUTION BLOCKED
       │
       ▼
   ┌─────────────────┐
   │ EXECUTE via MCP │
   └─────────────────┘
```

**Why a different model for the reviewer?** It's the only mechanism that
yields uncorrelated errors between proposer and reviewer. Two passes of the
*same* model on the *same* prompt would be ~deterministic. The reviewer
inherits `OPENAI_BASE_URL` and `OPENAI_API_KEY` from the main agent — only
`REVIEWER_MODEL_ALIAS` differs (typically a smaller/cheaper model).

---

## 5. The closed loop — observe → act → remember → reconcile

```
                                                                    ┌─────────────┐
                                                                    │ PLAYBOOK at │
                                                                    │ scan N+1    │
                                                                    │ start uses  │
                                                                    │ reconciled  │
                                                                    │ outcomes    │
                                                                    └─────────────┘
                                                                          ▲
                                                                          │
   SCAN N                                                                 │
   ┌────────────────────────────────────────────────────────────────────┐ │
   │                                                                    │ │
   │  1.  Discover tools, classify, filter violents                     │ │
   │  2.  Build playbook block from PRIOR reconciled episodes ──────────┘ │
   │  3.  ReAct loop:                                                     │
   │       └▶ LLM proposes tool calls                                     │
   │            ├▶ soft  → gate ALLOW → MCP exec      → episode (pending) │
   │            ├▶ hard  → gate NEEDS_REVIEW                              │
   │            │            └▶ Reviewer (2 pass) APPROVED → MCP exec     │
   │            │                                          → episode      │
   │            └▶ hard  → Reviewer BLOCKED → episode (blocked-by-review) │
   │                                                                      │
   │  4.  Build analysis JSON (issues, health, ...)                       │
   │                                                                      │
   │  5.  Stamp episodes with target issue's symptom_fingerprint          │
   │      (matches by normalized_component — e.g. cataloguedb-* )         │
   │                                                                      │
   │  6.  RECONCILE — for each pending episode from prior scan(s)         │
   │      whose observation window has elapsed:                           │
   │                                                                      │
   │      ┌──────────────────────────────────────────────────────────┐    │
   │      │ target fingerprint absent      → succeeded                │    │
   │      │ target fingerprint present     → ineffective              │    │
   │      │ target absent + new high-sev   → regressed                │    │
   │      │   issue on SAME component                                 │    │
   │      │ target absent + new lower-sev  → ambiguous                │    │
   │      │   issue on SAME component                                 │    │
   │      │ different component issues     → NOT attributed           │    │
   │      └──────────────────────────────────────────────────────────┘    │
   │                                                                      │
   │  7.  Persist scan's issue fingerprints + structured issues           │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                          ┌──────────────────────────┐
                          │ MemoryStore (JSONL)      │
                          │  • episodes              │
                          │  • fingerprint snapshots │
                          │  Schema-versioned, TTL'd │
                          └──────────────────────────┘
```

**Per-action observation windows** decide when an episode is ripe to judge:

| Verb shape | Window | Why |
|---|---|---|
| `restart` / `kill` / `delete-pod` / `recreate` | 90 s | Pod restart + readiness |
| `scale` | 120 s | New pods need to come up |
| `patch` / `apply` / `update` / `set` | 300 s | PDB-respecting rolling update |
| `cordon` / `drain` / `evict` | 180 s | Pod migration grace period |
| any soft | 60 s | Should propagate quickly |

A pending episode whose window hasn't elapsed stays `pending` and gets
re-evaluated on whichever later scan first satisfies `now >= min_observe_until_ts`.

---

## 6. Module layout

```
flash-agent/
├── flash_agent.py            (main orchestrator — gated_execute, reconcile,
│                              scan_trace, playbook injection)
├── config.py                 (AgentConfig + mitigation env knobs + defaults)
├── main.py                   (entry point — unchanged)
│
├── policy/                   ← Action enforcement
│   ├── audit.py              ─ Phase 0  append-only JSONL writer
│   ├── classifier.py         ─ Phase 1  soft/hard/violent + filter
│   └── gate.py               ─ Phase 2  scope + action-class enforcement
│
├── memory/                   ← Persistent learning loop
│   ├── episode.py            ─ Phase 3  MitigationEpisode dataclass + schema
│   ├── fingerprint.py        ─ Phase 5  stable issue / tool-call hashing
│   ├── store.py              ─ Phase 5  FileMemoryStore + InMemoryStore
│   └── reconciler.py         ─ Phase 6  outcome attribution
│
├── llm/
│   ├── hindsight.py          (existing FLASH reflection — unchanged)
│   ├── review.py             ─ Phase 4  2-pass HardActionReviewer
│   └── playbook.py           ─ Phase 7  prior-outcome roll-up into prompts
│
├── mcp/
│   └── client.py             (existing MCP JSON-RPC client — unchanged)
│
└── tests/                    ─ Phase 8  unit + integration tests
    ├── test_audit.py             3 tests
    ├── test_classifier.py       27 tests
    ├── test_config.py            5 tests
    ├── test_fingerprint.py      11 tests
    ├── test_gate.py             15 tests
    ├── test_memory.py           10 tests
    ├── test_playbook.py          5 tests
    ├── test_reconciler.py        7 tests
    ├── test_review.py            7 tests
    └── integration/
        └── test_mitigate_flow.py 7 tests
                              ─────
                              ★ 95 tests total — all pass
```

---

## 7. Configuration

| Env var | Default | Effect |
|---|---|---|
| `AGENT_MODE` | `observe` | `observe` (today) or `mitigate` (closes the loop) |
| `AGENT_SCOPE_NAMESPACE` | *(empty)* | Explicit scope — required in mitigate mode unless ack'd |
| `MITIGATION_ALLOW_DISCOVERED_SCOPE` | `false` | Permit mitigate with auto-discovered scope |
| `MITIGATION_REVIEW_ITERS` | `2` | Number of reviewer passes for hard actions |
| `MITIGATION_AUDIT_PATH` | *(empty)* | JSONL audit file path — empty disables |
| `REVIEWER_MODEL_ALIAS` | *(empty)* | Reviewer model. Empty = degraded mode (warns at startup) |
| `ACTION_PATTERNS_SOFT/HARD/VIOLENT` | *(defaults)* | Comma-separated regex overrides |
| `AGENT_MEMORY_PATH` | *(empty)* | JSONL episode store — empty = in-memory only |
| `MEMORY_TTL_DAYS` | `7` | Episode TTL on read |

### Mode change is zero-risk by default

```
                ┌───────────────────────────────────┐
                │  Default boot (no env changes)    │
                │                                   │
                │  AGENT_MODE=observe               │
                │  • All new code is wired          │
                │  • Audit log: disabled            │
                │  • Memory: in-memory              │
                │  • Reviewer: never called         │
                │  • Behavior: byte-identical       │
                │              to pre-mitigation    │
                └───────────────────────────────────┘
                                │
                                │  flip AGENT_MODE=mitigate
                                │  + set AGENT_SCOPE_NAMESPACE=<ns>
                                │  + set REVIEWER_MODEL_ALIAS=<smaller-model>
                                │  + set MITIGATION_AUDIT_PATH=/var/log/audit.jsonl
                                │  + set AGENT_MEMORY_PATH=/var/lib/agent/memory.jsonl
                                ▼
                ┌───────────────────────────────────┐
                │  Closed-loop mode                 │
                │                                   │
                │  Soft tools execute, hard tools   │
                │  reviewed, episodes recorded,     │
                │  next-scan reconciliation feeds   │
                │  the playbook.                    │
                └───────────────────────────────────┘
```

---

## 8. Rollout sequence

```
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 1  Deploy with AGENT_MODE=observe (default)                     │
│         • All new code present, no behavior change                   │
│         • Audit log fills with classifications so operators can      │
│           verify the classifier on real traffic before flipping mode │
└──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 2  Enable AGENT_MODE=mitigate in NON-PROD                       │
│         • Set AGENT_SCOPE_NAMESPACE=<staging-ns> explicitly          │
│         • Watch audit log: classifier accuracy, gate decisions,      │
│           reviewer verdicts, reconciliation outcomes                 │
└──────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 3  Promote namespace-by-namespace to prod                       │
│         • One namespace at a time; deliberate; reversible            │
│         • Memory store accumulates per-namespace playbook            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 9. Running the tests

```bash
cd flash-agent
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/
```

Expected output: `95 passed`.

---

## 10. What's intentionally out of scope

- Multi-agent shared memory / cross-pod episode merging.
- Automatic rollback of hard actions when they regress (memory *flags*
  regressions; no auto-undo).
- LLM-based dynamic re-classification of tools at runtime.
- Human-in-the-loop approval gates — the 2-pass LLM review *is* the review
  mechanism. Operators wanting Slack/webhook approval should add it
  externally.
