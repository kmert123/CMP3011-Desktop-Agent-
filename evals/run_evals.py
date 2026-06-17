#!/usr/bin/env python3
"""Offline eval harness — calls classify_intent only. No Gemini, no real actions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "jarvis"))

from classify import classify_intent  # noqa: E402

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_BASELINE_FILE = Path(__file__).parent / "baseline.json"

# Maps classify matched_rule → action kind for dry-mode action scoring.
# "type_input" and "close_kill" have no corresponding actions.py kind — intentional gaps.
_RULE_TO_KIND: dict[str, str] = {
    "open_launch":   "open_app",
    "click_press":   "click_element",
    "set_clipboard": "set_clipboard",
    "type_input":    "type_input",
    "close_kill":    "close_app",
}


def load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(_FIXTURE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[warn] skipping {path.name}: {exc}", file=sys.stderr)
            continue
        # Skip internal-only metadata keys
        fixtures.append({k: v for k, v in data.items() if not k.startswith("_")})
    return fixtures


def score_one(fixture: dict) -> dict:
    query = fixture["query"]
    exp_intent = fixture["expected_intent"].upper()
    exp_rung_raw = fixture.get("expected_rung")
    exp_rung = exp_rung_raw.upper() if exp_rung_raw else None
    exp_action = fixture.get("expected_action")

    r = classify_intent(query)
    actual_intent = r.intent.value.upper()
    actual_rung = r.entry_rung.name.upper() if r.entry_rung else None

    intent_ok = actual_intent == exp_intent

    # Rung: None == None → not applicable (both correctly absent)
    if exp_rung is None and actual_rung is None:
        rung_ok = None
    else:
        rung_ok = actual_rung == exp_rung

    # Action: scored only when fixture provides expected_action
    if exp_action is None:
        action_ok = None
    else:
        expected_kind = (exp_action.get("kind") or "").lower()
        actual_kind = _RULE_TO_KIND.get(r.matched_rule, "")
        action_ok = actual_kind == expected_kind

    return {
        "query": query,
        "intent_ok": intent_ok,
        "rung_ok": rung_ok,
        "action_ok": action_ok,
        "actual_intent": actual_intent,
        "actual_rung": actual_rung,
        "matched_rule": r.matched_rule,
        "expected_intent": exp_intent,
        "expected_rung": exp_rung,
    }


def _fmt(v: bool | None) -> str:
    if v is None:
        return "n/a "
    return "PASS" if v else "FAIL"


def _pct(scores: list[bool]) -> float:
    return 100.0 * sum(scores) / len(scores) if scores else 0.0


def run(args: argparse.Namespace) -> int:
    fixtures = load_fixtures()
    if not fixtures:
        print(f"No fixtures found in {_FIXTURE_DIR}")
        return 1

    results = [score_one(f) for f in fixtures]

    # Per-fixture table
    col_q = 46
    header = f"{'Query':<{col_q}} {'Intent':<6} {'Rung':<6} {'Action':<6} Rule"
    print(header)
    print("-" * len(header))
    for res in results:
        q = (res["query"][:col_q - 2] + "..") if len(res["query"]) > col_q else res["query"]
        i_flag = _fmt(res["intent_ok"])
        r_flag = _fmt(res["rung_ok"])
        a_flag = _fmt(res["action_ok"])
        detail = res["matched_rule"]
        if not res["intent_ok"]:
            detail += f"  [got {res['actual_intent']}, expected {res['expected_intent']}]"
        if res["rung_ok"] is False:
            detail += f"  [rung got {res['actual_rung']}, expected {res['expected_rung']}]"
        print(f"{q:<{col_q}} {i_flag:<6} {r_flag:<6} {a_flag:<6} {detail}")

    print()

    # Aggregate
    intent_scores = [r["intent_ok"] for r in results]
    rung_scores   = [r["rung_ok"]   for r in results if r["rung_ok"]   is not None]
    action_scores = [r["action_ok"] for r in results if r["action_ok"] is not None]

    intent_pct = _pct(intent_scores)
    rung_pct   = _pct(rung_scores)
    action_pct = _pct(action_scores)

    print(f"Intent accuracy:    {sum(intent_scores)}/{len(intent_scores)}  ({intent_pct:.1f}%)")
    if rung_scores:
        print(f"Rung accuracy:      {sum(rung_scores)}/{len(rung_scores)}  ({rung_pct:.1f}%)")
    if action_scores:
        print(f"Action match rate:  {sum(action_scores)}/{len(action_scores)}  ({action_pct:.1f}%)")

    if args.baseline:
        payload = {
            "intent_accuracy":   intent_pct,
            "rung_accuracy":     rung_pct,
            "action_match_rate": action_pct,
            "n_fixtures":        len(fixtures),
        }
        _BASELINE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nBaseline written: {_BASELINE_FILE}")
        return 0

    # Regression guard
    if _BASELINE_FILE.exists():
        baseline = json.loads(_BASELINE_FILE.read_text(encoding="utf-8"))
        floor = baseline.get("intent_accuracy", 0.0) - 2.0
        if intent_pct < floor:
            print(
                f"\nREGRESSION: intent accuracy {intent_pct:.1f}% is more than 2 points "
                f"below baseline {baseline['intent_accuracy']:.1f}%"
            )
            return 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Jarvis offline classifier evals")
    parser.add_argument(
        "--baseline", action="store_true",
        help="Write current scores to baseline.json instead of comparing"
    )
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
