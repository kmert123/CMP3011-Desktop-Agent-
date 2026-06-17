"""Rule-based intent classifier — maps a query to Act + Perception axes.

Two orthogonal fields replace the old Intent quartet:
  act        in {ANSWER, ACT}     — what the system should DO after classification
  perception in {NONE, STRUCTURE, PIXELS}  — what kind of screen info is needed

Mapping from old intents:
  ACTION     → act=ACT,    perception=NONE
  TEXT       → act=ANSWER, perception=STRUCTURE
  VISUAL     → act=ANSWER, perception=PIXELS
  NO_CONTEXT → act=ANSWER, perception=NONE

entry_rung is no longer stored in ClassifyResult; it is derived deterministically
from (perception, app_class) by the router.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Act(enum.Enum):
    ANSWER = "answer"   # produce a text/image answer to the user
    ACT    = "act"      # execute a side-effecting action (click, open, type…)


class Perception(enum.Enum):
    NONE      = "none"       # no screen reading required
    STRUCTURE = "structure"  # accessibility-tree text is sufficient
    PIXELS    = "pixels"     # a screenshot / visual representation is required


# ---------------------------------------------------------------------------
# Backward-compat shim: Intent enum consumed by gemini.py / main.py / router.py
# ---------------------------------------------------------------------------

class Intent(enum.Enum):
    NO_CONTEXT = "no_context"
    TEXT       = "text"
    VISUAL     = "visual"
    ACTION     = "action"

    @staticmethod
    def from_axes(act: Act, perception: Perception) -> "Intent":
        if act == Act.ACT:
            return Intent.ACTION
        if perception == Perception.NONE:
            return Intent.NO_CONTEXT
        if perception == Perception.PIXELS:
            return Intent.VISUAL
        return Intent.TEXT


@dataclass
class ClassifyResult:
    act: Act
    perception: Perception
    matched_rule: str
    high_conf: bool = False    # True → skip LLM, use regex result directly
    needs_focus: bool = False  # True → query references a specific on-screen element
                               #         by deixis or spatial phrase; resolve via
                               #         get_element_at_cursor / get_focused_element /
                               #         resolve_reference / resolve_reference_vlm
                               #         before answering or acting.

    @property
    def intent(self) -> Intent:
        """Backward-compat: map axes to the old Intent enum."""
        return Intent.from_axes(self.act, self.perception)


# ---------------------------------------------------------------------------
# Rule table — (act, perception, rule_name, regex_pattern)
# Evaluated in order; first match wins.
# ---------------------------------------------------------------------------

_RULES: list[tuple[Act, Perception, str, str]] = [
    # --- ACT / NONE — side-effecting actions (high-conf fast-path) ---
    (Act.ACT, Perception.NONE, "open_launch",   r"^(open|launch|start|run)\b"),
    (Act.ACT, Perception.NONE, "close_kill",    r"^(close|quit|exit|kill)\b"),
    (Act.ACT, Perception.NONE, "click_press",   r"^(click|press|tap|hit)\b"),
    (Act.ACT, Perception.NONE, "type_input",    r"^(type|enter|input)\b"),
    (Act.ACT, Perception.NONE, "set_clipboard", r"\bset\s+clipboard\b|\bcopy\s+to\s+clipboard\b"),

    # --- ANSWER / PIXELS — explicitly visual queries (high-conf fast-path) ---
    (Act.ANSWER, Perception.PIXELS, "on_screen",     r"\bon[\s-]?screen\b"),
    (Act.ANSWER, Perception.PIXELS, "looking_at",    r"what\s+am\s+i\s+looking\s+at"),
    (Act.ANSWER, Perception.PIXELS, "what_see",      r"\bwhat\s+(do\s+you\s+see|can\s+you\s+see)\b"),
    (Act.ANSWER, Perception.PIXELS, "this_chart",    r"\bthis\s+(chart|graph|diagram|plot)\b"),
    (Act.ANSWER, Perception.PIXELS, "describe_this", r"\bdescribe\s+(this|what)\b"),
    (Act.ANSWER, Perception.PIXELS, "this_ui",       r"\bthis\s+(ui|interface|screen|window)\b"),
    (Act.ANSWER, Perception.PIXELS, "this_image",    r"\bthis\s+(image|screenshot|picture)\b"),

    # --- ANSWER / NONE — general knowledge, no screen context needed ---
    (Act.ANSWER, Perception.NONE, "define",    r"^(what\s+is|what\'s|define|who\s+is|who\'s)\b"),
    (Act.ANSWER, Perception.NONE, "write_a",   r"^(write|draft|compose)\s+(a|an|me)\b"),
    (Act.ANSWER, Perception.NONE, "timer",     r"\b(set\s+a\s+timer|remind\s+me\s+in|alarm)\b"),
    (Act.ANSWER, Perception.NONE, "convert",   r"^(convert|calculate|compute|how\s+many)\b"),
    (Act.ANSWER, Perception.NONE, "translate", r"^(translate|say\s+in)\b"),
    (Act.ANSWER, Perception.NONE, "weather",   r"\bweather\b|\btemperature\b|\bforecast\b"),

    # --- ANSWER / STRUCTURE — on-screen text queries ---
    (Act.ANSWER, Perception.STRUCTURE, "summarize",   r"\b(summarize|summarise)\s+(what|this|the)\b"),
    (Act.ANSWER, Perception.STRUCTURE, "fix_error",   r"\b(fix|debug|resolve)\s+(this|the)\s+(error|bug|issue)\b"),
    (Act.ANSWER, Perception.STRUCTURE, "explain_code",r"\b(explain|describe)\s+(this|the)\s+code\b"),
    (Act.ANSWER, Perception.STRUCTURE, "what_reading",r"\bwhat\s+am\s+i\s+reading\b"),
    (Act.ANSWER, Perception.STRUCTURE, "this_text",   r"\bthis\s+(text|article|document|doc|page)\b"),
    (Act.ANSWER, Perception.STRUCTURE, "error_msg",   r"\b(error\s+message|stack\s+trace|exception)\b"),
    (Act.ANSWER, Perception.STRUCTURE, "this_error",  r"\bwhat\s+(does\s+this\s+error|is\s+this\s+error)\b"),
    # "what does the text on my screen say", "read the screen text", "the text on my screen" —
    # explicit requests to read ALL visible text. Routes to STRUCTURE (fusion runs OCR) and
    # skips the timeout-prone LLM router so a full-text read is deterministic.
    (Act.ANSWER, Perception.STRUCTURE, "screen_text",
     r"\btext\s+(?:on|in)\b.*\bscreen\b|\bread\b.*\b(?:screen|text)\b|\bwhat\s+does\s+the\s+text\b"),
]

_COMPILED = [
    (act, perc, name, re.compile(pat, re.IGNORECASE))
    for act, perc, name, pat in _RULES
]

# Rules confident enough to skip the local LLM entirely.
# All ACT rules (safety-critical) + highest-confidence PIXELS phrases.
_HIGH_CONF_RULES: frozenset[str] = frozenset({
    "open_launch", "close_kill", "click_press", "type_input", "set_clipboard",
    "on_screen", "looking_at", "what_see",
    "screen_text",
})

# ---------------------------------------------------------------------------
# Deictic / referring-expression detection (needs_focus flag)
#
# These patterns indicate the query references a specific on-screen element —
# by pure deixis ("this", "that"), selection state ("highlighted", "selected"),
# cursor position ("what I'm pointing at"), or spatial phrase ("the X at the
# top right").  Any rule match sets needs_focus=True on the result.
#
# Rules are evaluated independently of the main _RULES table; they annotate
# the result produced by the main table rather than replacing it.
# High-conf deictic rules are obvious enough to skip the LLM on their own.
# ---------------------------------------------------------------------------

# (rule_name, regex_pattern)
_DEICTIC_RULES: list[tuple[str, str]] = [
    # Pure deixis — "this", "that", "it" as direct object of an info verb
    ("deixis_this",      r"\b(tell\s+me\s+about|explain|describe|what\s+is|what\'s|info\s+on|more\s+(?:info|detail)s?\s+(?:about|on))\s+(this|that|it)\b"),
    # "more info on this / more details about that"
    ("deixis_more_info", r"\bmore\s+(?:info(?:rmation)?|detail)s?\s+(?:about|on)\s+(this|that)\b"),
    # Highlighted / selected state
    ("selected_text",    r"\b(the\s+)?(highlighted|selected|chosen)\s+\w+"),
    # "what I have selected" / "what have I selected" / "what's selected" / "what is selected"
    ("what_selected",    r"\bwhat\s+(?:(?:i\s+have|have\s+i|i\'ve|is)\s+)?(?:selected|highlighted)\b"),
    ("whats_selected",   r"\bwhat\'s\s+(selected|highlighted)\b"),
    # Cursor-deictic
    ("pointing_at",      r"\bwhat\s+i\'?m\s+(?:pointing|hovering|mousing)\s+(at|over)\b"),
    ("under_cursor",     r"\b(under|at)\s+(the\s+)?cursor\b"),
    # Focused / active element
    ("focused_elem",     r"\b(the\s+)?(focused|active|current)\s+(field|element|control|input|box)\b"),
    # Spatial descriptors: "the X at the top/bottom/left/right/top-right …"
    ("spatial_the",      r"\bthe\s+\w[\w\s]{0,20}?\s+(?:at|in|on)\s+the\s+(top|bottom|left|right|top[\s-]left|top[\s-]right|bottom[\s-]left|bottom[\s-]right|center|centre|middle)\b"),
    # "the top/bottom/left/right X" shorthand
    ("spatial_adj",      r"\bthe\s+(top|bottom|left|right|upper|lower)\s+\w+\b"),
    # Relational: "below/above/next to/left of/right of <element>"
    ("relational",       r"\b(below|above|under|over|next\s+to|beside|left\s+of|right\s+of)\s+\w"),
]

_DEICTIC_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in _DEICTIC_RULES
]

# Deictic rules unambiguous enough to also set high_conf=True.
_DEICTIC_HIGH_CONF: frozenset[str] = frozenset({
    "deixis_more_info",
    "what_selected",
    "whats_selected",
    "pointing_at",
    "under_cursor",
})


def classify_intent(query: str) -> ClassifyResult:
    q = query.strip()

    # Main axis classification.
    result: ClassifyResult | None = None
    for act, perc, rule, rx in _COMPILED:
        if rx.search(q):
            result = ClassifyResult(
                act=act,
                perception=perc,
                matched_rule=rule,
                high_conf=(rule in _HIGH_CONF_RULES),
            )
            break
    if result is None:
        # Default bias: ANSWER / STRUCTURE (UIA entry) — more context is safer.
        result = ClassifyResult(
            act=Act.ANSWER,
            perception=Perception.STRUCTURE,
            matched_rule="default_bias",
            high_conf=False,
        )

    # Deictic / referring-expression pass — orthogonal to act/perception axes.
    for dname, drx in _DEICTIC_COMPILED:
        if drx.search(q):
            result.needs_focus = True
            if dname in _DEICTIC_HIGH_CONF:
                result.high_conf = True
            break  # one match is enough to set the flag

    return result


if __name__ == "__main__":
    tests = [
        # Existing axis tests
        "open notepad",
        "what am I looking at?",
        "what is machine learning?",
        "summarize what I'm reading",
        "fix this error",
        "explain this code",
        "click the Save button",
        "what's the weather like?",
        "set clipboard to hello world",
        "something completely random",
        "describe this chart",
        "what's on screen?",
        # screen_text rule
        "what do you know about the text on my screen?",
        "what does the text on my screen say",
        "read the screen text",
        "read me the text on the screen",
        # Deictic / referring-expression tests
        "more info on this",
        "tell me about this",
        "what I'm pointing at",
        "what's selected",
        "what have I selected",
        "the highlighted text",
        "the button at the top right",
        "the top button",
        "below the username field",
        "explain that",
        "what is this?",
        "under the cursor",
        "the focused input",
    ]
    print(f"{'query':48}  {'act':8}  {'perc':12}  {'focus':5}  {'hconf':5}  rule")
    print("-" * 110)
    for q in tests:
        r = classify_intent(q)
        print(
            f"{q!r:48}  {r.act.value:8}  {r.perception.value:12}  "
            f"{'Y' if r.needs_focus else '-':5}  {'Y' if r.high_conf else '-':5}  "
            f"{r.matched_rule}"
        )
