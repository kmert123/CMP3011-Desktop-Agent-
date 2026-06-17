"""Pretty-print the last N turns from ~/.jarvis/traces.jsonl.

Usage:
    python tools/trace_view.py              # last 3 turns
    python tools/trace_view.py --last 10    # last 10 turns
    python tools/trace_view.py --turn <id>  # specific turn_id (compact)
    python tools/trace_view.py --full <id>  # full replay of a specific turn_id
    python tools/trace_view.py --full-last  # full replay of the most recent turn
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


_TRACES_PATH = Path.home() / ".jarvis" / "traces.jsonl"


def _read_traces(n: int) -> list[dict]:
    if not _TRACES_PATH.exists():
        return []
    lines = _TRACES_PATH.read_text(encoding="utf-8").splitlines()
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records[-n:]


def _find_turn(turn_id: str) -> dict | None:
    if not _TRACES_PATH.exists():
        return None
    for line in _TRACES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("turn_id") == turn_id:
                return rec
        except json.JSONDecodeError:
            pass
    return None


def _stage(trace: dict, name: str) -> dict | None:
    for s in trace.get("stages", []):
        if s.get("stage") == name:
            return s
    return None


def _all_stages(trace: dict, name: str) -> list[dict]:
    return [s for s in trace.get("stages", []) if s.get("stage") == name]


def _print_turn(trace: dict) -> None:
    tid = trace.get("turn_id", "?")
    print(f"\n{'='*70}")
    print(f"turn_id : {tid}")

    inp = _stage(trace, "INPUT")
    if inp:
        print(f"query   : {inp.get('query', '')!r}")
        proc = inp.get("process", "")
        title = inp.get("title", "")
        ac = inp.get("app_class", "")
        print(f"target  : {proc} | {ac} | {title!r}")

    cl = _stage(trace, "CLASSIFY")
    if cl:
        print(f"classify: act={cl.get('act')}  perception={cl.get('perception_mode')}  "
              f"intent={cl.get('intent')}  cache={cl.get('used_cache')}")

    perc = _stage(trace, "PERCEPTION")
    if perc:
        print(f"percept : rung={perc.get('rung')}  source={perc.get('source')}  "
              f"chars={perc.get('chars')}  elems={perc.get('element_count')}  "
              f"ok={perc.get('ok')}  stale={perc.get('stale')}  cache={perc.get('used_cache')}")

    detail = _stage(trace, "PERCEPTION_DETAIL")
    if detail:
        print(f"  detail: uia={detail.get('uia_count')}  ocr={detail.get('ocr_count')}  "
              f"cv={detail.get('cv_count')}  fused={detail.get('fused_count')}  "
              f"full_text_chars={detail.get('full_text_chars')}")

    focus = _stage(trace, "FOCUS")
    if focus:
        print(f"focus   : source={focus.get('source')}  conf={focus.get('confidence'):.2f}  "
              f"ambiguous={focus.get('ambiguous')}  text={focus.get('resolved_text')!r}")

    prompt = _stage(trace, "PROMPT")
    if prompt:
        print(f"prompt  : source={prompt.get('answer_source_expected')}  "
              f"screen_chars={prompt.get('screen_block_chars')}  "
              f"history={prompt.get('history_turns')}  image={prompt.get('image_attached')}")

    tools = _all_stages(trace, "TOOLS")
    for t in tools:
        print(f"tool    : {t.get('name')}({t.get('args', {})})  "
              f"→ {t.get('result_summary', '')!r}  budget_left={t.get('budget_remaining')}")

    ans = _stage(trace, "ANSWER")
    if ans:
        print(f"answer  : source={ans.get('answer_source')}  chars={ans.get('chars')}  "
              f"escalated={ans.get('escalated')}→{ans.get('escalated_rung')}")

    out = _stage(trace, "OUTCOME")
    if out:
        latency = out.get("latency_ms")
        src = out.get("answer_source", "?")
        lat_str = f"{latency}ms" if latency is not None else "?"
        print(f"outcome : source={src}  latency={lat_str}")


def _sep(char: str = "─", width: int = 72) -> None:
    print(char * width)


def _field(label: str, value: object, absent: str = "(absent)") -> None:
    v = value if value is not None else absent
    print(f"{label:<26}{v}")


def _block(label: str, text: str | None) -> None:
    """Print a labelled multi-line body block between delimiter lines."""
    _sep()
    print(f"  {label}")
    _sep()
    if text:
        print(text)
    else:
        print("(absent)")
    _sep()


def _print_full(trace: dict) -> None:
    """Full replay renderer — no truncation, section headers, copy-friendly."""
    absent = "(absent)"

    print("\n" + "=" * 72)
    _field("turn_id", trace.get("turn_id"))
    _field("wake_ts", trace.get("wake_ts"))

    # ── 1. REQUEST ──────────────────────────────────────────────────────────
    print("\n── REQUEST " + "─" * 61)
    inp = _stage(trace, "INPUT")
    if inp:
        _field("  query", inp.get("query", absent))
        _field("  process", inp.get("process", absent))
        _field("  app_class", inp.get("app_class", absent))
        _field("  title", inp.get("title", absent))
    else:
        print(f"  INPUT stage {absent}")

    # ── 2. PATH ──────────────────────────────────────────────────────────────
    print("\n── PATH " + "─" * 64)

    cl = _stage(trace, "CLASSIFY")
    if cl:
        print("  CLASSIFY")
        _field("    act", cl.get("act"))
        _field("    perception_mode", cl.get("perception_mode"))
        _field("    intent", cl.get("intent"))
        _field("    used_cache", cl.get("used_cache"))

    perc = _stage(trace, "PERCEPTION")
    if perc:
        print("  PERCEPTION")
        _field("    rung", perc.get("rung"))
        _field("    source", perc.get("source"))
        _field("    chars", perc.get("chars"))
        _field("    element_count", perc.get("element_count"))
        _field("    ok", perc.get("ok"))
        _field("    stale", perc.get("stale"))
        _field("    used_cache", perc.get("used_cache"))

    detail = _stage(trace, "PERCEPTION_DETAIL")
    if detail:
        print("  PERCEPTION_DETAIL")
        _field("    uia_count", detail.get("uia_count"))
        _field("    ocr_count", detail.get("ocr_count"))
        _field("    cv_count", detail.get("cv_count"))
        _field("    fused_count", detail.get("fused_count"))
        _field("    full_text_chars", detail.get("full_text_chars"))
        uia_s = detail.get("uia_sample")
        ocr_s = detail.get("ocr_sample")
        _field("    uia_sample", uia_s if uia_s is not None else absent)
        _field("    ocr_sample", ocr_s if ocr_s is not None else absent)
        fts = detail.get("fused_text_sample")
        if fts is not None:
            print("    fused_text_sample:")
            for ln in (fts or "(empty)").splitlines():
                print(f"      {ln}")
        else:
            _field("    fused_text_sample", absent)
        if detail.get("vision_fallback") is not None:
            _field("    vision_fallback", detail.get("vision_fallback"))
            _field("    vision_refs", detail.get("vision_refs"))

    focus = _stage(trace, "FOCUS")
    if focus:
        print("  FOCUS")
        _field("    source", focus.get("source"))
        _field("    confidence", focus.get("confidence"))
        _field("    ambiguous", focus.get("ambiguous"))
        _field("    resolved_text", focus.get("resolved_text"))

    tools = _all_stages(trace, "TOOLS")
    for t in tools:
        print("  TOOL")
        _field("    name", t.get("name"))
        _field("    args", t.get("args"))
        _field("    result_summary", t.get("result_summary"))
        _field("    budget_remaining", t.get("budget_remaining"))

    # ── 3. SYSTEM PROMPT ─────────────────────────────────────────────────────
    prompt = _stage(trace, "PROMPT")
    sp = prompt.get("system_prompt") if prompt else None
    print()
    _block("SYSTEM PROMPT", sp)

    # ── 4. PROMPT SENT ───────────────────────────────────────────────────────
    pt = prompt.get("prompt_text") if prompt else None
    _block("PROMPT SENT", pt)
    if prompt:
        _field("  image_attached", prompt.get("image_attached"))
        _field("  history_turns", prompt.get("history_turns"))
        _field("  screen_block_chars", prompt.get("screen_block_chars"))

    # ── 5. ANSWER ────────────────────────────────────────────────────────────
    ans = _stage(trace, "ANSWER")
    at = ans.get("answer_text") if ans else None
    print()
    _block("ANSWER", at)
    if ans:
        _field("  answer_source", ans.get("answer_source"))
        _field("  escalated", ans.get("escalated"))
        _field("  escalated_rung", ans.get("escalated_rung"))

    # ── 6. OUTCOME ───────────────────────────────────────────────────────────
    out = _stage(trace, "OUTCOME")
    if out:
        print("\n── OUTCOME " + "─" * 61)
        _field("  answer_source", out.get("answer_source"))
        latency = out.get("latency_ms")
        _field("  latency_ms", f"{latency}ms" if latency is not None else absent)

    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description="View Jarvis turn traces")
    parser.add_argument("--last", type=int, default=3, metavar="N",
                        help="Show last N turns (default: 3)")
    parser.add_argument("--turn", type=str, default=None, metavar="ID",
                        help="Show a specific turn_id (compact)")
    parser.add_argument("--full", type=str, default=None, metavar="ID",
                        help="Full replay of a specific turn_id")
    parser.add_argument("--full-last", action="store_true",
                        help="Full replay of the most recent turn")
    args = parser.parse_args()

    if args.full:
        trace = _find_turn(args.full)
        if trace is None:
            print(f"turn_id {args.full!r} not found in {_TRACES_PATH}")
            return
        _print_full(trace)
    elif args.full_last:
        traces = _read_traces(1)
        if not traces:
            print(f"No traces found at {_TRACES_PATH}")
            return
        _print_full(traces[-1])
    elif args.turn:
        trace = _find_turn(args.turn)
        if trace is None:
            print(f"turn_id {args.turn!r} not found in {_TRACES_PATH}")
            return
        _print_turn(trace)
    else:
        traces = _read_traces(args.last)
        if not traces:
            print(f"No traces found at {_TRACES_PATH}")
            return
        for trace in traces:
            _print_turn(trace)
    print()


if __name__ == "__main__":
    main()
