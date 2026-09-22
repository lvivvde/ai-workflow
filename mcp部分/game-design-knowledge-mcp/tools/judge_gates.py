"""Judge a recorded evaluation run against a gate set.

The evaluation tool judges the run it just produced. This entry point judges a
run that already exists, so a threshold can be revised and every recorded run
re-judged without re-running the corpus, and so a baseline runner can keep the
invariant verdict and the gate verdict apart.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from game_design_knowledge.evaluation.gates import (
    DEFAULT_GATES_PATH,
    evaluate_gates,
    load_gates,
)


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Judge a recorded run.json against a quality gate set."
    )
    parser.add_argument("--run", type=Path, required=True, help="Recorded run.json")
    parser.add_argument(
        "--gates",
        type=Path,
        default=DEFAULT_GATES_PATH,
        help="Quality gate set to judge the run with",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Earlier run.json the relative regression limits compare against",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Also write the verdict to this file instead of only printing it",
    )
    arguments = parser.parse_args()

    run = _read(arguments.run)
    layers = run.get("layers")
    if not isinstance(layers, list) or not layers:
        print(
            f"{arguments.run}: this run reports no layers, so no gate can be judged",
            file=sys.stderr,
        )
        return 1
    report = evaluate_gates(
        load_gates(arguments.gates),
        layers,
        baseline=_read(arguments.baseline) if arguments.baseline else None,
    )
    verdict = {
        "run_id": str((run.get("manifest") or {}).get("run_id") or ""),
        "run_ok": bool(run.get("ok", False)),
        "gates": report.as_payload(),
    }
    payload = json.dumps(verdict, ensure_ascii=False, indent=2)
    if arguments.json_out is not None:
        arguments.json_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if not run.get("ok", False):
        print(
            f"{verdict['run_id']}: the run failed its own invariants; the layers "
            "were judged anyway",
            file=sys.stderr,
        )
    if report.ok:
        print(f"quality gates: passed ({report.version})")
        return 0
    print("quality gates: FAILED", file=sys.stderr)
    for followup in report.followups():
        print(f"  [{followup['error_class']}] {followup['task']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
