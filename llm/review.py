"""
Hard Action Reviewer – Two-Pass LLM Review
============================================

Every hard action goes through two reasoning passes on the *reviewer model*
before execution:

  Pass 1 (``justify``)  — must cite concrete evidence from prior tool results.
                          A justification with empty ``evidence_used`` is
                          forcibly downgraded to BLOCKED.
  Pass 2 (``challenge``) — adversarial: "what would have to be true for this
                          action to make things worse?". Either pass returning
                          BLOCKED blocks execution.

The reviewer reuses the same ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` as
the main agent, but pins to ``cfg.reviewer_model_alias``. That model
difference is what makes reviewer errors uncorrelated with the proposer's
reasoning. If ``reviewer_model_alias`` is empty the reviewer falls back to
``cfg.model_alias`` — degraded mode that still runs but loses its
uncorrelated-error property.

Failures fail closed: any exception → BLOCKED.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from openai import AzureOpenAI, OpenAI

from config import AgentConfig
from memory.episode import ReviewVerdict

logger = logging.getLogger("flash-agent")


_REVIEW_SYSTEM_PROMPT = """You are an independent action reviewer for an ITOps agent.

The agent has proposed a HARD (mutating) action on Kubernetes infrastructure.
Your job is to decide whether to APPROVE or BLOCK the action based on the
evidence available in the trace.

You are not the proposer. You do not have the proposer's reasoning. You see
only the action and the tool-call trace that led up to it.

A HARD action is recoverable but stateful (patch, update, scale, restart,
rollout, apply, cordon, drain, exec). VIOLENT actions (delete/destroy/drop/
purge/wipe/force-evict) are filtered out before reaching you — they are
NEVER your concern.

You MUST respond with valid JSON only:
{
  "verdict": "APPROVED" | "BLOCKED",
  "reasoning": "<one paragraph>",
  "evidence_used": ["<citation from trace>", "<citation from trace>", ...]
}

Rules:
  - Cite evidence by quoting or paraphrasing prior tool results from the trace.
    Empty ``evidence_used`` is not acceptable for an APPROVED verdict.
  - Prefer the smallest reversible action. A larger action than the evidence
    supports → BLOCKED.
  - If the trace does not establish the precondition for this action → BLOCKED.
  - If the action could plausibly worsen state in a way the trace doesn't
    rule out → BLOCKED.
  - Do not approve cluster-wide changes based on per-namespace evidence.
"""


_JUSTIFY_FRAMING = """## Review Pass: JUSTIFY

The agent proposes the following HARD action:

  Tool: {tool_name}
  Args: {tool_args}
  Tool description: {tool_description}

Symptom context (from this scan):
{symptom_context}

Prior evidence (from this scan's trace, if any):
{prior_evidence}

Question:
  What concrete evidence in the prior tool-call trace establishes the
  precondition that this action is needed?

  Your ``evidence_used`` field MUST list specific citations from the trace.
  A justification without trace citations is not acceptable — return BLOCKED
  with reasoning if the trace does not contain such evidence.
"""


_CHALLENGE_FRAMING = """## Review Pass: CHALLENGE

The agent proposes the following HARD action:

  Tool: {tool_name}
  Args: {tool_args}
  Tool description: {tool_description}

Symptom context (from this scan):
{symptom_context}

Prior evidence (from this scan's trace, if any):
{prior_evidence}

Question:
  What would have to be true about cluster state for this action to make
  things worse? Is the trace consistent with any such scenario?

  Treat this as adversarial. Look for missing evidence, ambiguous symptoms,
  cascade risks, capacity assumptions, and side effects on dependents.
  If any plausible worse-state scenario is consistent with the trace, return
  BLOCKED.
"""


def _create_openai_client(cfg: AgentConfig) -> OpenAI:
    """Build a reviewer client. Same endpoint as the main agent, different model."""
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


def _parse_verdict_json(content: str) -> Dict[str, Any]:
    """Extract a JSON verdict from the model response, tolerating code fences."""
    text = (content or "").strip()
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            text = text[start:end].strip()
    # Some models prepend prose; isolate the JSON object.
    if not text.startswith("{"):
        brace = text.find("{")
        if brace >= 0:
            text = text[brace:]
    return json.loads(text)


class HardActionReviewer:
    """Two-pass adversarial reviewer for hard mutations."""

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._degraded = not bool(cfg.reviewer_model_alias)
        self._review_model = cfg.reviewer_model_alias or cfg.model_alias
        if self._degraded:
            logger.warning(
                "HardActionReviewer running in DEGRADED mode — REVIEWER_MODEL_ALIAS "
                "is empty, falling back to MODEL_ALIAS=%s. Reviewer errors are now "
                "correlated with the proposer's reasoning.",
                cfg.model_alias,
            )
        self._client: Optional[OpenAI] = None

    # Lazy client so test mocks can inject a stub before any call.
    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = _create_openai_client(self.cfg)
        return self._client

    def set_client(self, client: OpenAI) -> None:
        """Test hook: inject a stub OpenAI-compatible client."""
        self._client = client

    def review(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_def: Dict[str, Any],
        symptom_context: str,
        prior_evidence: Optional[str],
        framing: Literal["justify", "challenge"],
        iteration: int,
    ) -> ReviewVerdict:
        """Run a single review pass and return its verdict."""
        template = _JUSTIFY_FRAMING if framing == "justify" else _CHALLENGE_FRAMING
        user_prompt = template.format(
            tool_name=tool_name,
            tool_args=json.dumps(tool_args, default=str),
            tool_description=tool_def.get("description", ""),
            symptom_context=(symptom_context or "(none)")[:4000],
            prior_evidence=(prior_evidence or "(no prior evidence supplied)")[:4000],
        )

        try:
            client = self._get_client()
            response = client.chat.completions.create(
                model=self._review_model,
                messages=[
                    {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
            )
            content = response.choices[0].message.content or ""
            parsed = _parse_verdict_json(content)
        except Exception as exc:
            logger.warning(
                "Reviewer call failed (framing=%s, iter=%d): %s — failing closed",
                framing,
                iteration,
                exc,
            )
            return ReviewVerdict(
                verdict="BLOCKED",
                reasoning=f"reviewer error: {exc}",
                evidence_used=[],
                iteration=iteration,
                framing=framing,
                model=self._review_model,
                degraded=self._degraded,
            )

        raw_verdict = str(parsed.get("verdict", "BLOCKED")).upper()
        verdict: Literal["APPROVED", "BLOCKED"] = (
            "APPROVED" if raw_verdict == "APPROVED" else "BLOCKED"
        )
        reasoning = str(parsed.get("reasoning", ""))
        evidence_used = parsed.get("evidence_used", [])
        if not isinstance(evidence_used, list):
            evidence_used = [str(evidence_used)]
        else:
            evidence_used = [str(e) for e in evidence_used]

        # Forced downgrade: a justify pass that says APPROVED without citing
        # any evidence from the trace is the classic "acting on a hunch"
        # failure mode. Catch it.
        if framing == "justify" and verdict == "APPROVED" and not evidence_used:
            logger.info(
                "Forced downgrade: justify pass approved without evidence — BLOCKED"
            )
            return ReviewVerdict(
                verdict="BLOCKED",
                reasoning=(
                    "FORCED DOWNGRADE — justify pass returned APPROVED with empty "
                    "evidence_used; review requires at least one trace citation. "
                    f"Original reasoning: {reasoning}"
                ),
                evidence_used=[],
                iteration=iteration,
                framing=framing,
                model=self._review_model,
                degraded=self._degraded,
            )

        return ReviewVerdict(
            verdict=verdict,
            reasoning=reasoning,
            evidence_used=evidence_used,
            iteration=iteration,
            framing=framing,
            model=self._review_model,
            degraded=self._degraded,
        )

    def review_twice(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        tool_def: Dict[str, Any],
        symptom_context: str = "",
        prior_evidence: Optional[str] = None,
    ) -> List[ReviewVerdict]:
        """
        Run both review passes. Returns the verdict list in order.

        The caller approves only if EVERY verdict in the returned list is
        ``APPROVED``. A single BLOCKED → caller must block.

        The number of passes is governed by ``cfg.mitigation_review_iters``,
        with framing rotating ``justify → challenge → justify → ...``.
        """
        verdicts: List[ReviewVerdict] = []
        iters = max(1, int(self.cfg.mitigation_review_iters))
        framings: List[Literal["justify", "challenge"]] = []
        for i in range(iters):
            framings.append("justify" if i % 2 == 0 else "challenge")

        for idx, framing in enumerate(framings, start=1):
            verdict = self.review(
                tool_name=tool_name,
                tool_args=tool_args,
                tool_def=tool_def,
                symptom_context=symptom_context,
                prior_evidence=prior_evidence,
                framing=framing,
                iteration=idx,
            )
            verdicts.append(verdict)
            # Short-circuit on first BLOCKED — no point running further passes
            # when the action is already denied.
            if verdict.verdict == "BLOCKED":
                break
        return verdicts
