"""
Fingerprinting – Stable issue and tool-call identifiers
=========================================================

Two fingerprint functions:

  fingerprint_issue(issue, scope_key) → str
      Stable hash of (category, normalized_component, severity, scope_key).
      Used by the reconciler to ask "is the same problem still present?"
      across scans.

  fingerprint_tool_call(tool_name, args, scope_key) → str
      Stable hash of (tool_name, normalized_args, scope_key). Used to dedupe
      near-identical proposals.

The component-name normalization collapses Kubernetes generation suffixes so
a recreated pod with a new hash still maps to the same component.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

# Captures a digit-run inside a hyphen-separated segment.
_DIGIT_PART = re.compile(r"^[a-z0-9]+\d[a-z0-9]*$", re.IGNORECASE)


def normalize_component(name: Optional[str]) -> Optional[str]:
    """
    Collapse Kubernetes generation suffixes.

    Examples:
        cataloguedb-7f8d4c-q9w2     → cataloguedb-*-*
        front-end                   → front-end
        api-deployment-5d9b8        → api-deployment-*
    """
    if not name:
        return name
    parts = name.split("-")
    if len(parts) <= 1:
        return name
    out: List[str] = []
    for part in parts:
        # A segment containing BOTH digits and letters AND short → generation
        # hash. A pure-letter segment, or a long segment, is part of the name.
        if (
            part
            and any(ch.isdigit() for ch in part)
            and any(ch.isalpha() for ch in part)
            and len(part) <= 10
            and _DIGIT_PART.match(part)
        ):
            out.append("*")
        elif part.isdigit() and len(part) <= 10:
            out.append("*")
        else:
            out.append(part)
    return "-".join(out)


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint_issue(issue: Dict[str, Any], scope_key: str) -> str:
    """
    Deterministic hash for an issue.

    Composes ``(category, normalized_component, severity, scope_key)``. The
    summary string is intentionally excluded — it is freeform and would make
    every scan produce a different fingerprint.
    """
    category = (issue.get("category") or "").strip().lower()
    severity = (issue.get("severity") or "").strip().lower()
    component = normalize_component(str(issue.get("component") or "")) or ""
    payload = json.dumps(
        {"category": category, "component": component, "severity": severity, "scope": scope_key},
        sort_keys=True,
        ensure_ascii=False,
    )
    return _hash(payload)


def fingerprint_tool_call(
    tool_name: str,
    args: Dict[str, Any],
    scope_key: str,
) -> str:
    """
    Deterministic hash for a tool call. Used to dedupe proposals.

    The component-shaped arg (``name``, ``pod``, ``deployment``, ``service``,
    ``workload``) is normalized so re-running the same action on a recreated
    pod still hashes equal.
    """
    normalized_args: Dict[str, Any] = {}
    for k, v in args.items():
        if k in ("name", "pod", "deployment", "service", "workload") and isinstance(v, str):
            normalized_args[k] = normalize_component(v) or ""
        else:
            normalized_args[k] = v
    payload = json.dumps(
        {"tool": tool_name, "args": normalized_args, "scope": scope_key},
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return _hash(payload)


def fingerprints_for_issues(issues: List[Dict[str, Any]], scope_key: str) -> List[str]:
    """Convenience: produce a fingerprint per issue in a scan's issue list."""
    return [fingerprint_issue(i, scope_key) for i in issues]
