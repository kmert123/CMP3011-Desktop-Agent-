"""LLM-based routing: optional fast-path for query act/perception classification.

The local LLM is demoted to an optional fast-path.  Routing no longer depends on it
for correctness — if it is unavailable or slow, the regex classifier covers all cases.

The LLM contract has been updated to the two-axis schema:
  act        in {ANSWER, ACT}
  perception in {NONE, STRUCTURE, PIXELS}

entry_rung is NOT in the contract; it is derived deterministically by entry_rung_for().
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import config
import local_llm

if TYPE_CHECKING:
    from perception_target import PerceptionTarget
    from session_context import SessionContext

_VALID_ACTS  = {"ANSWER", "ACT"}
_VALID_PERCS = {"NONE", "STRUCTURE", "PIXELS"}

_DEFAULT: dict[str, Any] = {
    "act":        "ANSWER",
    "perception": "STRUCTURE",
    "confidence": 0.5,
    "action_params": None,
}

_PROMPT_TEMPLATE = """\
You are the router for a desktop screen assistant. Classify the user's query into two axes.

Context:
{context}

Target app: {process} — "{title}"

Query: {query}

Respond with ONLY a JSON object — no markdown, no explanation:
{{
  "act": "ANSWER | ACT",
  "perception": "NONE | STRUCTURE | PIXELS",
  "action_params": {{"kind": "<kind>", "target": "<optional>", "text": "<optional>"}} | null,
  "confidence": 0.0 to 1.0
}}

act guide:
- ACT:    user wants to DO something on screen (open, click, type, close)
- ANSWER: user wants information or a response

perception guide:
- NONE:      no screen content needed (general knowledge, chitchat, or action)
- STRUCTURE: accessibility tree / text is enough (reading text, summarizing, errors)
- PIXELS:    a screenshot is required (visual charts, UI layout, "what do you see")
"""


def _validate(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(_DEFAULT)

    act = str(raw.get("act", "")).upper()
    if act in _VALID_ACTS:
        result["act"] = act

    perc = str(raw.get("perception", "")).upper()
    if perc in _VALID_PERCS:
        result["perception"] = perc

    try:
        conf = float(raw.get("confidence", 0.5))
        result["confidence"] = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        pass

    ap = raw.get("action_params")
    if isinstance(ap, dict) and "kind" in ap:
        result["action_params"] = ap

    # Bias to default if confidence too low.
    if result["confidence"] < config.ROUTER_MIN_CONF:
        return dict(_DEFAULT)

    return result


def route_llm(
    query: str,
    session: "SessionContext",
    target: "PerceptionTarget | None",
) -> dict[str, Any] | None:
    """Classify query via local LLM (optional fast-path).

    Returns a validated dict with keys act/perception/confidence/action_params,
    or None if the LLM was unreachable / timed out (caller falls back to regex).
    """
    process = getattr(target, "process", "unknown") if target else "unknown"
    title   = getattr(target, "title", "") if target else ""
    context = session.to_prompt_block() or "(no context)"

    prompt = _PROMPT_TEMPLATE.format(
        context=context,
        process=process,
        title=title,
        query=query,
    )

    raw = local_llm.complete_json(prompt, config.LOCAL_LLM_TIMEOUT_MS)
    if raw is None or not isinstance(raw, dict):
        return None  # LLM unreachable or unparseable — caller falls back to regex

    return _validate(raw)
