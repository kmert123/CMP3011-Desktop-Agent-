"""Offline eval harness for Jarvis.

Runs recorded or fixture-defined cases without live perception APIs and reports:
  - routing_accuracy  : fraction of cases where act+perception axes matched
  - success_at_rung   : fraction of cases where the correct rung was reached
  - grounding_prec    : fraction of grounding cases where the top element matched
  - false_success_rate: fraction of ACT cases marked ok=True but answer_correct=False
  - escalation_rate   : fraction of cases that triggered escalation
  - perception_quality: fraction of frame-fixture golden assertions that passed

Usage
-----
  python -m eval.harness                         # query + session + frame fixtures
  python -m eval.harness --baseline metrics.json # compare against saved baseline
  python -m eval.harness --save metrics.json     # save current metrics as baseline
  python -m eval.harness --frames path/to/dir    # override frame fixtures directory
  python -m eval.harness --dump-frame            # record one live frame to frames dir
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import numpy as np

# Cases directory relative to this file.
_CASES_DIR   = Path(__file__).parent / "cases"
_SESSIONS_DIR = _CASES_DIR / "sessions"
_FRAMES_DIR  = _CASES_DIR / "frames"

# ---------------------------------------------------------------------------
# Metric thresholds — release gate refuses to pass if regression exceeds these.
# ---------------------------------------------------------------------------
_REGRESSION_THRESHOLDS: dict[str, float] = {
    "routing_accuracy":   0.05,   # max allowed drop from baseline
    "success_at_rung":    0.05,
    "grounding_prec":     0.10,
    "false_success_rate": 0.05,   # max allowed *increase* from baseline
    "perception_quality": 0.05,   # max allowed drop from baseline
}


# ---------------------------------------------------------------------------
# Data classes — query / session path
# ---------------------------------------------------------------------------

@dataclass
class EvalCase:
    id: str
    query: str
    expected_act: str               # "ANSWER" | "ACT"
    expected_perception: str        # "NONE" | "STRUCTURE" | "PIXELS"
    expected_rung: str | None       # "UIA" | "OCR" | "VISION" | None
    expected_action_kind: str | None
    app_class: str | None
    answer_correct: bool | None     # ground-truth label; None = unknown
    grounding_target: str | None    # element text that must be grounded, or None


@dataclass
class CaseResult:
    case_id: str
    query: str
    act_match: bool
    perception_match: bool
    rung_match: bool | None         # None when expected_rung is None (no perception)
    grounding_hit: bool | None      # None when grounding_target is None
    false_success: bool
    escalated: bool
    notes: str = ""


# ---------------------------------------------------------------------------
# Data classes — frame fixture path
# ---------------------------------------------------------------------------

@dataclass
class FrameFixture:
    """One captured-frame test: PNG + golden expectations from sidecar JSON."""
    id: str
    frame_path: Path
    process: str
    app_class: str | None
    bounds: tuple[int, int, int, int]   # (x, y, w, h) virtual-desktop coords
    origin: tuple[int, int]             # (ox, oy) crop origin in screen space
    dpi_scale: float
    # Goldens
    required_substrings: list[str]      # all must appear in model.full_text (case-insensitive)
    max_fragment_phrases: list[dict]    # [{"phrase": "...", "max_fragments": 1}, ...]


@dataclass
class FrameResult:
    fixture_id: str
    frame_path: str
    passed: bool
    total_assertions: int
    passed_assertions: int
    notes: list[str] = field(default_factory=list)


@dataclass
class EvalMetrics:
    total: int = 0
    routing_accuracy: float = 0.0
    success_at_rung: float = 0.0
    grounding_prec: float = 0.0
    false_success_rate: float = 0.0
    escalation_rate: float = 0.0
    perception_quality: float = 0.0   # fraction of frame-fixture assertions that passed
    frame_total: int = 0
    failures: list[dict] = field(default_factory=list)
    frame_failures: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fixture loaders — query / session
# ---------------------------------------------------------------------------

def load_query_fixtures(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[harness] skip line {lineno} in {path.name}: {exc}", file=sys.stderr)
                continue
            cases.append(EvalCase(
                id=d.get("id", f"line{lineno}"),
                query=d["query"],
                expected_act=d.get("expected_act", "ANSWER"),
                expected_perception=d.get("expected_perception", "NONE"),
                expected_rung=d.get("expected_rung"),
                expected_action_kind=d.get("expected_action_kind"),
                app_class=d.get("app_class"),
                answer_correct=d.get("answer_correct"),
                grounding_target=d.get("grounding_target"),
            ))
    return cases


def load_session_fixtures(sessions_dir: Path) -> list[EvalCase]:
    """Expand session JSON files into individual EvalCases."""
    cases: list[EvalCase] = []
    if not sessions_dir.exists():
        return cases
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[harness] skip {path.name}: {exc}", file=sys.stderr)
            continue
        session_id = data.get("session_id", path.stem)
        app_class = data.get("app_class")
        for i, turn in enumerate(data.get("turns", [])):
            cases.append(EvalCase(
                id=f"{session_id}.{i}",
                query=turn["query"],
                expected_act=turn.get("expected_act", "ANSWER"),
                expected_perception=turn.get("expected_perception", "NONE"),
                expected_rung=turn.get("expected_rung"),
                expected_action_kind=turn.get("expected_action_kind"),
                app_class=turn.get("app_class", app_class),
                answer_correct=turn.get("answer_correct"),
                grounding_target=turn.get("grounding_target"),
            ))
    return cases


# ---------------------------------------------------------------------------
# Fixture loaders — frame fixtures
# ---------------------------------------------------------------------------

def load_frame_fixtures(frames_dir: Path) -> list[FrameFixture]:
    """Load all frame fixtures from frames_dir.

    Each fixture is a pair: <name>.png + <name>.json sidecar.
    """
    fixtures: list[FrameFixture] = []
    if not frames_dir.exists():
        return fixtures

    for sidecar in sorted(frames_dir.glob("*.json")):
        frame_path = sidecar.with_suffix(".png")
        if not frame_path.exists():
            print(f"[harness] skip {sidecar.name}: no matching PNG", file=sys.stderr)
            continue
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[harness] skip {sidecar.name}: {exc}", file=sys.stderr)
            continue

        bounds_raw = meta.get("bounds", [0, 0, 0, 0])
        origin_raw = meta.get("origin", [0, 0])
        fixtures.append(FrameFixture(
            id=sidecar.stem,
            frame_path=frame_path,
            process=meta.get("process", "unknown"),
            app_class=meta.get("app_class"),
            bounds=tuple(bounds_raw),  # type: ignore[arg-type]
            origin=tuple(origin_raw),  # type: ignore[arg-type]
            dpi_scale=float(meta.get("dpi_scale", 1.0)),
            required_substrings=meta.get("golden", {}).get("required_substrings", []),
            max_fragment_phrases=meta.get("golden", {}).get("max_fragment_phrases", []),
        ))
    return fixtures


# ---------------------------------------------------------------------------
# Frame replay: run real OCR + CV + fuse() against a saved frame
# ---------------------------------------------------------------------------

def _build_stub_target(fixture: FrameFixture) -> Any:
    """Construct a minimal PerceptionTarget-like object from fixture metadata."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from perception_target import PerceptionTarget
    from app_classifier import AppClass

    app_class_obj: AppClass | None = None
    if fixture.app_class is not None:
        try:
            app_class_obj = AppClass(fixture.app_class)
        except ValueError:
            pass

    return PerceptionTarget(
        hwnd=0,
        pid=0,
        process=fixture.process,
        title="",
        bounds=fixture.bounds,
        is_self=False,
        app_class=app_class_obj,
    )


def run_frame_fixture(fixture: FrameFixture) -> FrameResult:
    """Load PNG, run OCR + CV adapters + fuse(), assert goldens."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))

    import cv2
    notes: list[str] = []
    total_assertions = len(fixture.required_substrings) + len(fixture.max_fragment_phrases)

    try:
        frame = cv2.imread(str(fixture.frame_path))
        if frame is None:
            return FrameResult(
                fixture_id=fixture.id,
                frame_path=str(fixture.frame_path),
                passed=False,
                total_assertions=total_assertions,
                passed_assertions=0,
                notes=[f"cv2.imread returned None for {fixture.frame_path.name}"],
            )

        origin = fixture.origin

        from adapters.ocr_adapter import read_ocr
        from adapters.cv_adapter import read_cv
        from fusion import fuse

        ocr_elems = read_ocr(frame, origin)
        cv_elems  = read_cv(frame, origin)
        target    = _build_stub_target(fixture)
        model     = fuse(target, uia=[], ocr=ocr_elems, cv=cv_elems, frame=frame)
        full_text = model.full_text.lower()

    except Exception as exc:
        return FrameResult(
            fixture_id=fixture.id,
            frame_path=str(fixture.frame_path),
            passed=False,
            total_assertions=total_assertions,
            passed_assertions=0,
            notes=[f"pipeline error: {exc}"],
        )

    passed_count = 0

    # Assert required substrings
    for substr in fixture.required_substrings:
        if substr.lower() in full_text:
            passed_count += 1
        else:
            notes.append(f"missing substring: {substr!r}")

    # Assert max-fragmentation: phrase must not be split across more line elements than allowed.
    text_lines = [e.text for e in model.elements if e.text and e.source in ("uia", "ocr")]
    for spec in fixture.max_fragment_phrases:
        phrase = spec.get("phrase", "")
        max_frags = spec.get("max_fragments", 1)
        if not phrase:
            passed_count += 1
            continue
        phrase_lower = phrase.lower()
        fragments = sum(1 for line in text_lines if phrase_lower in line.lower())
        # Also check by scanning across consecutive lines joined
        joined = " ".join(text_lines).lower()
        if phrase_lower in joined:
            if fragments == 0:
                fragments = 1  # phrase spans join boundary; count as 1 fragment
        if fragments <= max_frags:
            passed_count += 1
        else:
            notes.append(f"fragmentation: {phrase!r} split across {fragments} elements (max {max_frags})")

    passed = (passed_count == total_assertions) and not notes
    return FrameResult(
        fixture_id=fixture.id,
        frame_path=str(fixture.frame_path),
        passed=passed,
        total_assertions=total_assertions,
        passed_assertions=passed_count,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Offline router stub — runs classify_intent only (no live perception)
# ---------------------------------------------------------------------------

def _classify_offline(case: EvalCase) -> dict[str, Any]:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))

    from classify import classify_intent

    result = classify_intent(case.query)
    act_str  = result.act.name
    perc_str = result.perception.name

    from router import entry_rung_for
    from app_classifier import AppClass
    app_class_obj: AppClass | None = None
    if case.app_class is not None:
        try:
            app_class_obj = AppClass(case.app_class)
        except ValueError:
            pass
    rung_obj = entry_rung_for(result.perception, app_class_obj)
    rung_str = rung_obj.name if rung_obj is not None else None

    grounding_ok: bool | None = None
    if case.grounding_target is not None:
        grounding_ok = (act_str == "ACT" and perc_str != "NONE")

    return {
        "act": act_str,
        "perception": perc_str,
        "rung": rung_str,
        "escalated": False,
        "grounding_ok": grounding_ok,
    }


# ---------------------------------------------------------------------------
# Run a single query/session case
# ---------------------------------------------------------------------------

def run_case(case: EvalCase) -> CaseResult:
    try:
        out = _classify_offline(case)
    except Exception as exc:
        return CaseResult(
            case_id=case.id, query=case.query,
            act_match=False, perception_match=False,
            rung_match=False if case.expected_rung else None,
            grounding_hit=None, false_success=False, escalated=False,
            notes=f"classify error: {exc}",
        )

    act_match        = out["act"] == case.expected_act
    perception_match = out["perception"] == case.expected_perception

    rung_match: bool | None = None
    if case.expected_rung is not None:
        rung_match = out["rung"] == case.expected_rung

    grounding_hit: bool | None = None
    if case.grounding_target is not None:
        grounding_hit = out.get("grounding_ok", False)

    false_success = (
        case.expected_act == "ACT"
        and case.answer_correct is False
        and False  # offline: no live action ran
    )

    return CaseResult(
        case_id=case.id, query=case.query,
        act_match=act_match,
        perception_match=perception_match,
        rung_match=rung_match,
        grounding_hit=grounding_hit,
        false_success=false_success,
        escalated=out["escalated"],
    )


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    results: list[CaseResult],
    cases: list[EvalCase],
    frame_results: list[FrameResult],
) -> EvalMetrics:
    total = len(results)

    routing_correct = 0
    rung_results:    list[CaseResult] = []
    ground_results:  list[CaseResult] = []
    act_cases:       list[CaseResult] = []
    escalated_count  = 0
    false_succs      = 0

    if total > 0:
        routing_correct = sum(1 for r in results if r.act_match and r.perception_match)
        rung_results    = [r for r in results if r.rung_match is not None]
        ground_results  = [r for r in results if r.grounding_hit is not None]
        act_cases       = [r for r in results
                           if any(c.id == r.case_id and c.expected_act == "ACT" for c in cases)]
        false_succs     = sum(1 for r in act_cases if r.false_success)
        escalated_count = sum(1 for r in results if r.escalated)

    rung_correct   = sum(1 for r in rung_results   if r.rung_match)
    ground_correct = sum(1 for r in ground_results if r.grounding_hit)

    failures = [
        {"id": r.case_id, "query": r.query, "notes": r.notes or _failure_notes(r)}
        for r in results
        if not (r.act_match and r.perception_match)
        or r.rung_match is False
        or r.grounding_hit is False
    ]

    # Perception quality: fraction of individual assertions that passed.
    frame_total = len(frame_results)
    if frame_total > 0:
        total_assertions  = sum(f.total_assertions  for f in frame_results)
        passed_assertions = sum(f.passed_assertions for f in frame_results)
        perc_quality = passed_assertions / total_assertions if total_assertions > 0 else 1.0
    else:
        perc_quality = 1.0  # no frame fixtures = no degradation

    frame_failures = [
        {"id": f.fixture_id, "frame": f.frame_path, "notes": f.notes}
        for f in frame_results
        if not f.passed
    ]

    return EvalMetrics(
        total=total,
        routing_accuracy=routing_correct / total if total > 0 else 1.0,
        success_at_rung=rung_correct / len(rung_results) if rung_results else 1.0,
        grounding_prec=ground_correct / len(ground_results) if ground_results else 1.0,
        false_success_rate=false_succs / len(act_cases) if act_cases else 0.0,
        escalation_rate=escalated_count / total if total > 0 else 0.0,
        perception_quality=round(perc_quality, 4),
        frame_total=frame_total,
        failures=failures,
        frame_failures=frame_failures,
    )


def _failure_notes(r: CaseResult) -> str:
    parts: list[str] = []
    if not r.act_match:
        parts.append("act mismatch")
    if not r.perception_match:
        parts.append("perception mismatch")
    if r.rung_match is False:
        parts.append("rung mismatch")
    if r.grounding_hit is False:
        parts.append("grounding miss")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------

def check_regression(current: EvalMetrics, baseline: EvalMetrics) -> list[str]:
    """Return regression messages. Empty list = no regression."""
    regressions: list[str] = []
    checks = [
        ("routing_accuracy",   current.routing_accuracy,   baseline.routing_accuracy,   -1),
        ("success_at_rung",    current.success_at_rung,    baseline.success_at_rung,    -1),
        ("grounding_prec",     current.grounding_prec,     baseline.grounding_prec,     -1),
        ("false_success_rate", current.false_success_rate, baseline.false_success_rate, +1),
        ("perception_quality", current.perception_quality, baseline.perception_quality, -1),
    ]
    for name, cur, base, direction in checks:
        threshold = _REGRESSION_THRESHOLDS.get(name, 0.05)
        delta = (cur - base) * direction   # positive = worse
        if delta > threshold:
            regressions.append(
                f"{name}: {base:.3f} -> {cur:.3f}  (d={delta:+.3f}, threshold={threshold:.3f})"
            )
    return regressions


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def print_report(metrics: EvalMetrics, baseline: EvalMetrics | None = None) -> None:
    print(f"\n{'-'*62}")
    print(f"  Jarvis Eval Harness  ({metrics.total} query cases, {metrics.frame_total} frame cases)")
    print(f"{'-'*62}")
    rows = [
        ("routing_accuracy",   metrics.routing_accuracy),
        ("success_at_rung",    metrics.success_at_rung),
        ("grounding_prec",     metrics.grounding_prec),
        ("false_success_rate", metrics.false_success_rate),
        ("escalation_rate",    metrics.escalation_rate),
        ("perception_quality", metrics.perception_quality),
    ]
    for name, val in rows:
        baseline_str = ""
        if baseline is not None:
            bval = getattr(baseline, name, None)
            if bval is not None:
                delta = val - bval
                baseline_str = f"  (baseline {bval:.3f}, d={delta:+.3f})"
        print(f"  {name:<24}  {val:.3f}{baseline_str}")

    if metrics.failures:
        print(f"\n  Query failures ({len(metrics.failures)}):")
        for f in metrics.failures[:20]:
            print(f"    [{f['id']}] {f['query']!r}  ->  {f['notes']}")
        if len(metrics.failures) > 20:
            print(f"    ... and {len(metrics.failures)-20} more")

    if metrics.frame_failures:
        print(f"\n  Frame failures ({len(metrics.frame_failures)}):")
        for f in metrics.frame_failures[:20]:
            ns = "; ".join(f["notes"]) if isinstance(f["notes"], list) else str(f["notes"])
            print(f"    [{f['id']}] {ns}")
        if len(metrics.frame_failures) > 20:
            print(f"    ... and {len(metrics.frame_failures)-20} more")
    print()


# ---------------------------------------------------------------------------
# --dump-frame: record one live frame to the frames directory
# ---------------------------------------------------------------------------

def dump_frame(frames_dir: Path) -> None:
    """Capture the current foreground window and save PNG + sidecar JSON.

    Reuses capture.py logic; debug_overlay is NOT called (no ScreenModel yet).
    The saved sidecar has empty golden expectations for the user to fill in.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))

    import time
    import cv2
    from capture import capture_target
    from perception_target import capture_foreground_target

    target = capture_foreground_target()
    frame, origin, dpi_scale, stale = capture_target(target)

    frames_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    stem = f"frame_{ts}"

    png_path  = frames_dir / f"{stem}.png"
    json_path = frames_dir / f"{stem}.json"

    cv2.imwrite(str(png_path), frame)

    app_class_val = target.app_class.value if target.app_class is not None else None
    meta = {
        "process":   target.process,
        "app_class": app_class_val,
        "bounds":    list(target.bounds),
        "origin":    list(origin),
        "dpi_scale": dpi_scale,
        "stale":     stale,
        "golden": {
            "required_substrings": [],
            "max_fragment_phrases": []
        },
    }
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[dump-frame] saved {png_path.name} + {json_path.name}  (stale={stale})")
    print(f"[dump-frame] fill in golden.required_substrings in {json_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis eval harness")
    parser.add_argument("--baseline", metavar="FILE",
                        help="Path to baseline metrics JSON for regression check")
    parser.add_argument("--save", metavar="FILE",
                        help="Save current metrics to this JSON file")
    parser.add_argument("--cases", metavar="FILE",
                        help="Path to queries.jsonl (default: eval/cases/queries.jsonl)")
    parser.add_argument("--sessions", metavar="DIR",
                        help="Path to sessions directory (default: eval/cases/sessions/)")
    parser.add_argument("--frames", metavar="DIR",
                        help="Path to frame fixtures directory (default: eval/cases/frames/)")
    parser.add_argument("--dump-frame", action="store_true",
                        help="Capture current foreground window and save as a frame fixture, then exit")
    args = parser.parse_args(argv)

    # --dump-frame is a dev utility; it writes a fixture then exits immediately.
    if args.dump_frame:
        frames_dir = Path(args.frames) if args.frames else _FRAMES_DIR
        dump_frame(frames_dir)
        return 0

    queries_path  = Path(args.cases)    if args.cases    else _CASES_DIR   / "queries.jsonl"
    sessions_path = Path(args.sessions) if args.sessions else _SESSIONS_DIR
    frames_path   = Path(args.frames)   if args.frames   else _FRAMES_DIR

    # Load query / session cases
    cases: list[EvalCase] = []
    if queries_path.exists():
        cases += load_query_fixtures(queries_path)
    cases += load_session_fixtures(sessions_path)

    # Load frame fixtures
    frame_fixtures = load_frame_fixtures(frames_path)

    if not cases and not frame_fixtures:
        print("[harness] No cases found. Add fixtures to eval/cases/queries.jsonl or eval/cases/frames/.")
        return 0

    results       = [run_case(c) for c in cases]
    frame_results = [run_frame_fixture(ff) for ff in frame_fixtures]
    metrics       = compute_metrics(results, cases, frame_results)

    baseline: EvalMetrics | None = None
    if args.baseline:
        try:
            raw = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            baseline = EvalMetrics(
                **{k: v for k, v in raw.items() if k not in ("failures", "frame_failures")},
                failures=raw.get("failures", []),
                frame_failures=raw.get("frame_failures", []),
            )
        except Exception as exc:
            print(f"[harness] Could not load baseline: {exc}", file=sys.stderr)

    print_report(metrics, baseline)

    if args.save:
        out = asdict(metrics)
        Path(args.save).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"[harness] Metrics saved to {args.save}")

    if baseline is not None:
        regressions = check_regression(metrics, baseline)
        if regressions:
            print("REGRESSION DETECTED -- release gate failed:")
            for r in regressions:
                print(f"  {r}")
            return 1
        else:
            print("Release gate: PASSED (no regression)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
