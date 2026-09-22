"""Run the offline evaluation harness against the committed corpora."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from game_design_knowledge.evaluation import load_corpus, run_evaluation
from game_design_knowledge.evaluation.corpus import refresh_manifest
from game_design_knowledge.evaluation.gates import (
    DEFAULT_GATES_PATH,
    evaluate_gates,
    load_gates,
)
from game_design_knowledge.evaluation.schema import EVALUATION_MODES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPORA = (
    PROJECT_ROOT / "evaluation" / "corpora" / "v1_compatibility",
    PROJECT_ROOT / "evaluation" / "corpora" / "development_set",
    PROJECT_ROOT / "evaluation" / "corpora" / "golden_set",
)


def _modes(value: str) -> tuple[str, ...]:
    """Parse ``--modes`` and refuse a typo instead of silently running nothing."""

    selected = tuple(token.strip() for token in value.split(",") if token.strip())
    if not selected:
        raise argparse.ArgumentTypeError("at least one mode is required")
    unknown = sorted(set(selected) - EVALUATION_MODES)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown modes {unknown}; choose from {sorted(EVALUATION_MODES)}"
        )
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize evaluation corpora, build a disposable index, and report "
            "layered results without blending them into a single score."
        )
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        action="append",
        dest="corpora",
        help="Corpus directory; repeat to combine corpora (default: committed set)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=PROJECT_ROOT / "evaluation" / "runs",
        help="Directory that receives run.json, item_results.json and report.md",
    )
    parser.add_argument(
        "--hardware-profile",
        default="baseline",
        choices=("baseline", "recommended", "visual"),
    )
    parser.add_argument(
        "--modes",
        type=_modes,
        default=("component", "e2e"),
        help="Comma-separated subset of component,e2e",
    )
    parser.add_argument(
        "--refresh-manifest",
        action="store_true",
        help="Recompute corpus sample fingerprints instead of running the harness",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow refreshing a frozen corpus; this invalidates earlier reports",
    )
    parser.add_argument(
        "--no-artifacts",
        action="store_true",
        help="Run without writing run artifacts",
    )
    parser.add_argument(
        "--gates",
        type=Path,
        default=DEFAULT_GATES_PATH,
        help="Quality gate set to judge the run with",
    )
    parser.add_argument(
        "--no-gates",
        action="store_true",
        help="Report the layers without judging any gate",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Earlier run.json the relative regression limits compare against",
    )
    arguments = parser.parse_args()

    corpora_paths = tuple(arguments.corpora or DEFAULT_CORPORA)

    if arguments.refresh_manifest:
        for corpus_path in corpora_paths:
            manifest_path = refresh_manifest(corpus_path, force=arguments.force)
            print(f"refreshed {manifest_path}")
        return 0

    corpora = [load_corpus(path) for path in corpora_paths]
    run = run_evaluation(
        corpora,
        runs_directory=None if arguments.no_artifacts else arguments.runs_dir,
        hardware_profile=arguments.hardware_profile,
        modes=arguments.modes,
    )
    print(json.dumps(run.as_payload(), ensure_ascii=False, indent=2))

    if arguments.no_gates:
        print("\ngates: skipped (--no-gates)", file=sys.stderr)
        return 0 if run.ok else 1
    baseline = (
        json.loads(arguments.baseline.read_text(encoding="utf-8"))
        if arguments.baseline is not None
        else None
    )
    report = evaluate_gates(
        load_gates(arguments.gates),
        run.as_payload()["layers"],
        baseline=baseline,
    )
    print(json.dumps({"gates": report.as_payload()}, ensure_ascii=False, indent=2))

    failures = run.failure_summary()
    if failures:
        print("\nFAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"\n{run.run_id}: all release-blocking invariants passed")
    if report.ok:
        print("quality gates: passed")
        return 0
    print("quality gates: FAILED", file=sys.stderr)
    for followup in report.followups():
        print(f"  [{followup['error_class']}] {followup['task']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
