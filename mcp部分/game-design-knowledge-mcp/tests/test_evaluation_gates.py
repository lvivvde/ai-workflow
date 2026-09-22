"""Tests for the per-layer quality gates and their error classification.

A release gate is only worth having if it can fail, so every branch - a missed
floor, an exceeded ceiling, an unmeasured layer, a thin annotation set, a
regression against a recorded baseline - is driven here with real payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Mapping

from game_design_knowledge.evaluation import load_corpus, run_evaluation
from game_design_knowledge.evaluation.gates import (
    ANNOTATION_CLASS,
    DATA_CLASS,
    DEFAULT_ERROR_CLASSES,
    DEFAULT_GATES_PATH,
    ENVIRONMENT_CLASS,
    ERROR_CLASSES,
    Gate,
    GateSet,
    RegressionLimit,
    evaluate_gates,
    load_gates,
    metric_value,
)
from game_design_knowledge.evaluation.schema import EVALUATION_LAYERS, SchemaError


PROJECT_ROOT = Path(__file__).parents[1]
CORPORA_ROOT = PROJECT_ROOT / "evaluation" / "corpora"
COMMITTED_CORPORA = ("v1_compatibility", "development_set", "golden_set")


def layer(
    name: str,
    *,
    mode: str = "component",
    status: str = "measured",
    samples: int = 1,
    notes: tuple[str, ...] = (),
    **metrics: Any,
) -> dict[str, Any]:
    """Build one layer payload the way the harness publishes it."""

    return {
        "layer": name,
        "mode": mode,
        "status": status,
        "metrics": dict(metrics),
        "sample_count": samples,
        "sample_ids": [f"sample-{index}" for index in range(samples)],
        "notes": list(notes),
    }


def gate_set(**overrides: Any) -> GateSet:
    payload: dict[str, Any] = {
        "version": "test-gates",
        "gates": [{"layer": "retrieval", "metric": "recall_at_limit", "floor": 0.6}],
    }
    payload.update(overrides)
    return GateSet.from_payload(payload, "test")


def extract_json_object(text: str, marker: str) -> Any:
    """Read one JSON object out of a stream that carries prose after it."""

    start = text.index(marker)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])
    raise AssertionError(f"no balanced JSON object after {marker!r}")


class MetricValueTests(unittest.TestCase):
    def test_a_plain_number_is_read_directly(self) -> None:
        self.assertEqual(0.5, metric_value({"recall": 0.5}, "recall"))
        self.assertEqual(3.0, metric_value({"documents_indexed": 3}, "documents_indexed"))

    def test_a_rate_mapping_is_read_as_its_rate(self) -> None:
        metrics = {
            "no_answer_false_positives": {
                "count": 1,
                "total": 4,
                "rate": 0.25,
                "interval": {"lower": 0.01, "upper": 0.7},
            }
        }

        self.assertEqual(0.25, metric_value(metrics, "no_answer_false_positives"))

    def test_a_precision_recall_f1_triple_is_read_as_its_f1(self) -> None:
        metrics = {
            "annotated_relation_scores": {
                "precision": 0.8,
                "recall": 0.6,
                "f1": 0.6857,
                "support": 5,
            }
        }

        self.assertEqual(0.6857, metric_value(metrics, "annotated_relation_scores"))

    def test_coverage_without_a_rate_is_read_as_matched_over_expected(self) -> None:
        metrics = {"required_atom_coverage": {"matched": 3, "expected": 4}}

        self.assertEqual(0.75, metric_value(metrics, "required_atom_coverage"))

    def test_coverage_over_an_empty_expected_set_is_not_a_number(self) -> None:
        metrics = {"required_atom_coverage": {"matched": 0, "expected": 0}}

        self.assertIsNone(metric_value(metrics, "required_atom_coverage"))

    def test_a_metric_the_layer_never_publishes_is_not_a_number(self) -> None:
        self.assertIsNone(metric_value({"mrr": 0.5}, "recall_at_limit"))

    def test_a_non_numeric_payload_is_not_a_number(self) -> None:
        self.assertIsNone(metric_value({"mrr": "high"}, "mrr"))
        self.assertIsNone(metric_value({"mrr": {"note": "n/a"}}, "mrr"))


class AbsoluteGateTests(unittest.TestCase):
    def test_a_metric_above_its_floor_passes(self) -> None:
        report = evaluate_gates(
            gate_set(),
            [layer("retrieval", recall_at_limit=0.75, mrr=0.6)],
        )

        self.assertTrue(report.ok)
        self.assertEqual("", report.outcomes[0].error_class)
        self.assertIn("floor 0.6", report.outcomes[0].detail)

    def test_a_metric_below_its_floor_fails_with_the_layer_error_class(self) -> None:
        report = evaluate_gates(
            gate_set(),
            [layer("retrieval", recall_at_limit=0.4, mrr=0.6)],
        )

        self.assertFalse(report.ok)
        self.assertEqual("retrieval", report.failures[0].error_class)
        self.assertIn("below the floor", report.failures[0].detail)

    def test_a_metric_above_its_ceiling_fails(self) -> None:
        judged = gate_set(
            gates=[
                {
                    "layer": "explanation",
                    "metric": "unsupported_atom_rate",
                    "ceiling": 0.0,
                }
            ]
        )

        report = evaluate_gates(judged, [layer("explanation", unsupported_atom_rate=0.1)])

        self.assertFalse(report.ok)
        self.assertEqual("explanation", report.failures[0].error_class)
        self.assertIn("above the ceiling", report.failures[0].detail)

    def test_a_metric_exactly_on_its_bound_is_inside_the_gate(self) -> None:
        judged = gate_set(
            gates=[
                {
                    "layer": "explanation",
                    "metric": "required_atom_coverage",
                    "floor": 0.8,
                    "ceiling": 1.0,
                }
            ]
        )

        report = evaluate_gates(
            judged, [layer("explanation", required_atom_coverage=0.8)]
        )

        self.assertTrue(report.ok)

    def test_an_unavailable_layer_fails_as_an_environment_problem(self) -> None:
        judged = gate_set(
            gates=[
                {
                    "layer": "ocr_transcription",
                    "metric": "cer",
                    "ceiling": 0.25,
                    "unavailable_class": ENVIRONMENT_CLASS,
                }
            ]
        )
        unavailable = layer(
            "ocr_transcription",
            status="unavailable",
            notes=("no OCR engine was found on PATH",),
        )

        report = evaluate_gates(judged, [unavailable])

        self.assertFalse(report.ok)
        self.assertEqual(ENVIRONMENT_CLASS, report.failures[0].error_class)
        self.assertIn("no OCR engine was found", report.failures[0].detail)
        self.assertTrue(any("environment" in note for note in report.notes))

    def test_a_layer_the_run_never_reports_fails_rather_than_vanishing(self) -> None:
        report = evaluate_gates(gate_set(), [layer("statement_fidelity", rate=1.0)])

        self.assertFalse(report.ok)
        self.assertEqual(ENVIRONMENT_CLASS, report.failures[0].error_class)
        self.assertIn("does not report this layer", report.failures[0].detail)

    def test_a_gate_above_the_annotated_sample_count_fails_as_annotation_work(
        self,
    ) -> None:
        judged = gate_set(
            gates=[
                {
                    "layer": "retrieval",
                    "metric": "recall_at_limit",
                    "floor": 0.6,
                    "samples_at_least": 5,
                }
            ]
        )

        report = evaluate_gates(judged, [layer("retrieval", samples=2, recall_at_limit=1.0)])

        self.assertFalse(report.ok)
        self.assertEqual(ANNOTATION_CLASS, report.failures[0].error_class)
        self.assertIn("at least 5", report.failures[0].detail)

    def test_a_gate_naming_a_metric_the_layer_omits_fails_as_annotation_work(
        self,
    ) -> None:
        report = evaluate_gates(
            gate_set(), [layer("retrieval", mrr=0.9, recall_at_1=0.9)]
        )

        self.assertFalse(report.ok)
        self.assertEqual(ANNOTATION_CLASS, report.failures[0].error_class)
        self.assertIn("reports no recall_at_limit", report.failures[0].detail)

    def test_a_mode_filtered_gate_judges_only_that_mode(self) -> None:
        judged = gate_set(
            gates=[
                {
                    "layer": "retrieval",
                    "metric": "no_answer_false_positives",
                    "ceiling": 0.0,
                    "mode": "component",
                }
            ]
        )
        layers = [
            layer("retrieval", mode="component", no_answer_false_positives=0.0),
            layer("retrieval", mode="e2e", no_answer_false_positives=1.0),
        ]

        report = evaluate_gates(judged, layers)

        self.assertTrue(report.ok)
        self.assertEqual("retrieval@component.no_answer_false_positives", report.outcomes[0].gate)

    def test_every_mode_of_a_filtered_layer_is_judged_on_its_own(self) -> None:
        judged = gate_set(gates=[{"layer": "explanation", "metric": "coverage", "floor": 0.5}])
        layers = [
            layer("explanation", mode="component", samples=2, coverage=0.9),
            layer("explanation", mode="e2e", samples=2, coverage=0.1),
        ]

        report = evaluate_gates(judged, layers)

        self.assertEqual(2, len(report.outcomes))
        self.assertEqual(
            ["explanation@e2e.coverage"], [failure.gate for failure in report.failures]
        )


class RegressionLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = gate_set(
            gates=[{"layer": "retrieval", "metric": "recall_at_limit", "floor": 0.6}],
            regression_limits=[
                {"layer": "retrieval", "metric": "recall_at_limit", "max_drop": 0.05}
            ],
        )
        self.baseline = {
            "manifest": {"run_id": "run-before", "started_at": "2026-01-01T00:00:00Z"},
            "ok": True,
            "layers": [
                layer("retrieval", mode="component", samples=5, recall_at_limit=0.8)
            ],
        }

    def test_without_a_baseline_the_drop_is_reported_as_unjudged(self) -> None:
        report = evaluate_gates(
            self.limits, [layer("retrieval", samples=5, recall_at_limit=0.8)]
        )

        self.assertTrue(report.ok)
        self.assertEqual(1, len(report.outcomes))
        self.assertIsNone(report.as_payload()["baseline"])
        self.assertTrue(any("not judged" in note for note in report.notes))
        self.assertTrue(
            any("earlier run.json" in note for note in report.notes), report.notes
        )

    def test_a_small_drop_inside_the_allowed_limit_passes(self) -> None:
        report = evaluate_gates(
            self.limits,
            [layer("retrieval", samples=5, recall_at_limit=0.78)],
            baseline=self.baseline,
        )

        self.assertTrue(report.ok)
        relative = report.outcomes[-1]
        self.assertIn("(relative)", relative.gate)
        self.assertIn("allowed drop is 0.05", relative.detail)

    def test_a_drop_past_the_allowed_limit_fails_with_the_layer_error_class(self) -> None:
        report = evaluate_gates(
            self.limits,
            [layer("retrieval", samples=5, recall_at_limit=0.5)],
            baseline=self.baseline,
        )

        self.assertFalse(report.ok)
        self.assertEqual("retrieval", report.failures[-1].error_class)
        self.assertEqual("run-before", report.as_payload()["baseline"]["run_id"])

    def test_a_mode_the_baseline_never_measured_fails_as_data(self) -> None:
        report = evaluate_gates(
            self.limits,
            [layer("retrieval", samples=5, recall_at_limit=0.9)],
            baseline={
                "manifest": {"run_id": "run-before"},
                "layers": [
                    layer("retrieval", mode="e2e", samples=5, recall_at_limit=0.9)
                ],
            },
        )

        relative = report.outcomes[-1]
        self.assertFalse(relative.ok)
        self.assertEqual(DATA_CLASS, relative.error_class)
        self.assertIn("never measured this mode", relative.detail)

    def test_a_limit_on_a_layer_the_baseline_lacks_is_not_a_silent_pass(self) -> None:
        report = evaluate_gates(
            self.limits,
            [layer("retrieval", samples=5, recall_at_limit=0.9)],
            baseline={"manifest": {"run_id": "run-before"}, "layers": []},
        )

        relative = report.outcomes[-1]
        self.assertFalse(relative.ok)
        self.assertEqual(DATA_CLASS, relative.error_class)

    def test_a_regression_limit_for_a_mode_the_run_skipped_is_not_a_silent_pass(
        self,
    ) -> None:
        limits = gate_set(
            gates=[{"layer": "retrieval", "metric": "recall_at_limit", "floor": 0.6}],
            regression_limits=[
                {
                    "layer": "retrieval",
                    "metric": "recall_at_limit",
                    "mode": "visual",
                    "max_drop": 0.05,
                }
            ],
        )

        report = evaluate_gates(
            limits,
            [layer("retrieval", samples=5, recall_at_limit=0.9)],
            baseline=self.baseline,
        )

        self.assertEqual(2, len(report.outcomes))
        self.assertFalse(report.ok)
        self.assertEqual(DATA_CLASS, report.failures[-1].error_class)
        self.assertIn("for the same mode", report.failures[-1].detail)

    def test_an_unavailable_layer_is_blamed_on_the_environment_on_both_sides(
        self,
    ) -> None:
        report = evaluate_gates(
            self.limits,
            [layer("retrieval", status="unavailable", samples=0)],
            baseline=self.baseline,
        )

        self.assertEqual(2, len(report.outcomes))
        self.assertEqual(
            {ENVIRONMENT_CLASS}, {failure.error_class for failure in report.failures}
        )
        self.assertIn("unavailable", report.failures[-1].detail)


class GateSetParsingTests(unittest.TestCase):
    def test_the_committed_gate_set_loads_and_keeps_its_identity(self) -> None:
        gate_set = load_gates(DEFAULT_GATES_PATH)

        self.assertEqual("quality-gates-0.1", gate_set.version)
        self.assertTrue(gate_set.description)
        self.assertTrue(gate_set.gates)
        self.assertTrue(gate_set.regression_limits)
        for gate in gate_set.gates:
            self.assertIn(gate.layer, EVALUATION_LAYERS)
        for limit in gate_set.regression_limits:
            self.assertIn(limit.layer, EVALUATION_LAYERS)

    def test_a_missing_gate_file_is_an_error_not_an_empty_gate_set(self) -> None:
        with self.assertRaises(SchemaError) as caught:
            load_gates(PROJECT_ROOT / "evaluation" / "no-such-gates.json")

        self.assertIn("missing", str(caught.exception))

    def test_a_gate_set_without_gates_is_refused(self) -> None:
        with self.assertRaises(SchemaError):
            GateSet.from_payload({"version": "x", "gates": []}, "test")
        with self.assertRaises(SchemaError):
            GateSet.from_payload({"version": "x"}, "test")

    def test_a_gate_without_a_bound_is_refused(self) -> None:
        with self.assertRaises(SchemaError) as caught:
            Gate.from_payload({"layer": "retrieval", "metric": "mrr"}, "test")

        self.assertIn("floor or a ceiling", str(caught.exception))

    def test_a_gate_without_a_layer_or_metric_is_refused(self) -> None:
        with self.assertRaises(SchemaError):
            Gate.from_payload({"layer": "retrieval", "floor": 0.5}, "test")

    def test_an_unknown_error_class_is_refused(self) -> None:
        with self.assertRaises(SchemaError) as caught:
            Gate.from_payload(
                {"layer": "retrieval", "metric": "mrr", "floor": 0.5, "error_class": "vibes"},
                "test",
            )

        self.assertIn("error_class", str(caught.exception))

    def test_a_non_numeric_bound_is_refused(self) -> None:
        with self.assertRaises(SchemaError):
            Gate.from_payload(
                {"layer": "retrieval", "metric": "mrr", "floor": "high"}, "test"
            )

    def test_a_gate_defaults_to_one_sample_and_its_layer_error_class(self) -> None:
        parsed = Gate.from_payload({"layer": "retrieval", "metric": "mrr", "floor": 0.5}, "t")

        self.assertEqual(1, parsed.samples_at_least)
        self.assertEqual("retrieval", parsed.resolved_error_class())
        self.assertEqual(ENVIRONMENT_CLASS, parsed.unavailable_class)

    def test_a_gate_name_carries_its_mode_when_it_has_one(self) -> None:
        parsed = Gate.from_payload(
            {"layer": "retrieval", "metric": "mrr", "floor": 0.5, "mode": "component"},
            "t",
        )

        self.assertEqual("retrieval@component.mrr", parsed.name)

    def test_a_round_trip_keeps_the_gate_set_readable(self) -> None:
        original = load_gates(DEFAULT_GATES_PATH)

        reparsed = GateSet.from_payload(original.as_payload(), "round-trip")

        self.assertEqual(original.as_payload(), reparsed.as_payload())

    def test_every_layer_has_a_default_error_class(self) -> None:
        for name in EVALUATION_LAYERS:
            with self.subTest(layer=name):
                self.assertIn(name, DEFAULT_ERROR_CLASSES)
                self.assertIn(DEFAULT_ERROR_CLASSES[name], ERROR_CLASSES)

    def test_the_error_class_table_has_no_duplicates(self) -> None:
        self.assertEqual(len(ERROR_CLASSES), len(set(ERROR_CLASSES)))
        self.assertIn(ANNOTATION_CLASS, ERROR_CLASSES)
        self.assertIn(ENVIRONMENT_CLASS, ERROR_CLASSES)

    def test_a_regression_limit_keeps_its_own_layer_error_class(self) -> None:
        limit = RegressionLimit.from_payload(
            {
                "layer": "explanation",
                "metric": "required_atom_coverage",
                "max_drop": 0.05,
                "error_class": "annotation",
            },
            "test",
        )

        self.assertEqual("annotation", limit.error_class)


class GateReportTests(unittest.TestCase):
    def test_a_failing_report_lists_one_followup_per_failure(self) -> None:
        report = evaluate_gates(
            gate_set(),
            [layer("retrieval", samples=5, recall_at_limit=0.1)],
        )

        followups = report.followups()
        self.assertEqual(1, len(followups))
        self.assertEqual("retrieval", followups[0]["layer"])
        self.assertEqual("recall_at_limit", followups[0]["metric"])
        self.assertTrue(followups[0]["task"].startswith("retrieval: retrieval.recall_at_limit"))

    def test_a_passing_report_has_a_stable_payload_shape(self) -> None:
        report = evaluate_gates(
            gate_set(), [layer("retrieval", samples=5, recall_at_limit=0.9)]
        )

        payload = report.as_payload()

        self.assertEqual(
            {"version", "ok", "outcomes", "failures", "followups", "baseline", "notes"},
            set(payload),
        )
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["failures"])
        self.assertEqual(
            {"gate", "layer", "metric", "ok", "detail", "error_class"},
            set(payload["outcomes"][0]),
        )

    def test_the_payload_is_json_serialisable(self) -> None:
        report = evaluate_gates(
            gate_set(),
            [layer("retrieval", samples=5, recall_at_limit=0.1)],
            baseline={"manifest": {"run_id": "earlier"}, "layers": []},
        )

        json.dumps(report.as_payload(), ensure_ascii=False)


class CommittedGateSetIntegrationTests(unittest.TestCase):
    """The committed gates have to name metrics the harness really publishes."""

    @classmethod
    def setUpClass(cls) -> None:
        corpora = [load_corpus(CORPORA_ROOT / name) for name in COMMITTED_CORPORA]
        cls.layers = run_evaluation(corpora).as_payload()["layers"]

    def test_every_gate_metric_is_published_by_a_measured_layer(self) -> None:
        checked = 0
        for gate in load_gates(DEFAULT_GATES_PATH).gates:
            for entry in self.layers:
                if entry["layer"] != gate.layer:
                    continue
                if gate.mode is not None and entry["mode"] != gate.mode:
                    continue
                if entry["status"] != "measured":
                    continue
                if int(entry["sample_count"]) < gate.samples_at_least:
                    continue
                with self.subTest(gate=gate.name, mode=entry["mode"]):
                    self.assertIsNotNone(
                        metric_value(dict(entry["metrics"]), gate.metric),
                        f"{gate.layer}@{entry['mode']} never publishes {gate.metric}",
                    )
                checked += 1

        self.assertGreater(checked, 0, "no committed gate was measurable at all")

    def test_the_committed_gates_judge_the_committed_corpora_without_crashing(
        self,
    ) -> None:
        report = evaluate_gates(load_gates(DEFAULT_GATES_PATH), self.layers)

        self.assertTrue(report.outcomes)
        for outcome in report.outcomes:
            self.assertIn(outcome.error_class, ("", *ERROR_CLASSES))

    def test_every_gate_outcome_details_what_was_compared(self) -> None:
        report = evaluate_gates(load_gates(DEFAULT_GATES_PATH), self.layers)

        for outcome in report.outcomes:
            with self.subTest(gate=outcome.gate):
                self.assertTrue(outcome.detail)
                self.assertIn(outcome.layer, outcome.gate)


class EvaluateCliGateTests(unittest.TestCase):
    """The evaluate tool has to expose the gate verdict and its exit code."""

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "evaluate.py"),
                "--corpus",
                str(CORPORA_ROOT / "v1_compatibility"),
                "--no-artifacts",
                *arguments,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_gates(self, directory: Path, **overrides: Any) -> Path:
        payload: Mapping[str, Any] = {
            "version": "cli-test-gates",
            "gates": [
                {
                    "layer": "source_import",
                    "metric": "documents_indexed",
                    "floor": 1,
                    "error_class": "data",
                }
            ],
        }
        payload = {**payload, **overrides}
        path = directory / "gates.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def test_a_satisfied_gate_set_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gates = self.write_gates(Path(temporary))

            completed = self.run_cli("--gates", str(gates))

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("quality gates: passed", completed.stdout)

    def test_a_missed_gate_exits_non_zero_with_its_error_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gates = self.write_gates(
                Path(temporary),
                gates=[
                    {
                        "layer": "source_import",
                        "metric": "documents_indexed",
                        "floor": 9999,
                        "error_class": "data",
                    }
                ],
            )

            completed = self.run_cli("--gates", str(gates))

        self.assertEqual(1, completed.returncode)
        self.assertIn("quality gates: FAILED", completed.stderr)
        self.assertIn("[data] data: source_import.documents_indexed", completed.stderr)

    def test_no_gates_skips_the_verdict_without_reading_the_file(self) -> None:
        completed = self.run_cli(
            "--no-gates", "--gates", str(PROJECT_ROOT / "evaluation" / "no-gates.json")
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("gates: skipped", completed.stderr)

    def test_a_baseline_run_is_echoed_back_in_the_verdict(self) -> None:
        baseline = {
            "manifest": {"run_id": "earlier-run", "started_at": "2026-01-01T00:00:00Z"},
            "ok": True,
            "layers": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            gates = self.write_gates(directory)
            baseline_path = directory / "run.json"
            baseline_path.write_text(
                json.dumps(baseline, ensure_ascii=False), encoding="utf-8"
            )

            completed = self.run_cli(
                "--gates", str(gates), "--baseline", str(baseline_path)
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        verdict = extract_json_object(completed.stdout, '{\n  "gates"')

        self.assertEqual("earlier-run", verdict["gates"]["baseline"]["run_id"])
        self.assertTrue(verdict["gates"]["ok"])


class JudgeGatesCliTests(unittest.TestCase):
    """A recorded run has to be re-judgeable without re-running the corpus."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-judge-")
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name)

    def write_run(
        self,
        layers: list[dict[str, Any]],
        *,
        ok: bool = True,
        run_id: str = "run-recorded",
    ) -> Path:
        path = self.workspace / "run.json"
        path.write_text(
            json.dumps(
                {"manifest": {"run_id": run_id}, "ok": ok, "layers": layers},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_gates(self, floor: float) -> Path:
        path = self.workspace / "gates.json"
        path.write_text(
            json.dumps(
                {
                    "version": "judge-cli-gates",
                    "gates": [
                        {
                            "layer": "retrieval",
                            "metric": "recall_at_limit",
                            "floor": floor,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def judge(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "judge_gates.py"), *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_recorded_run_that_meets_its_floor_passes(self) -> None:
        run = self.write_run([layer("retrieval", samples=5, recall_at_limit=0.9)])
        gates = self.write_gates(0.6)

        completed = self.judge("--run", str(run), "--gates", str(gates))

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("quality gates: passed", completed.stdout)
        verdict = extract_json_object(completed.stdout, '{\n  "run_id"')
        self.assertEqual("run-recorded", verdict["run_id"])
        self.assertTrue(verdict["gates"]["ok"])

    def test_a_recorded_run_below_its_floor_fails_and_can_be_written_out(self) -> None:
        run = self.write_run([layer("retrieval", samples=5, recall_at_limit=0.2)])
        gates = self.write_gates(0.6)
        verdict_path = self.workspace / "nested" / "gates.json"

        completed = self.judge(
            "--run",
            str(run),
            "--gates",
            str(gates),
            "--json-out",
            str(verdict_path),
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("quality gates: FAILED", completed.stderr)
        self.assertIn("[retrieval] retrieval: retrieval.recall_at_limit", completed.stderr)
        written = json.loads(verdict_path.read_text(encoding="utf-8"))
        self.assertFalse(written["gates"]["ok"])
        self.assertEqual(1, len(written["gates"]["failures"]))

    def test_a_run_that_failed_its_invariants_is_still_judged_and_flagged(self) -> None:
        run = self.write_run(
            [layer("retrieval", samples=5, recall_at_limit=0.9)], ok=False
        )
        gates = self.write_gates(0.6)

        completed = self.judge("--run", str(run), "--gates", str(gates))

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("failed its own invariants", completed.stderr)
        self.assertFalse(
            extract_json_object(completed.stdout, '{\n  "run_id"')["run_ok"]
        )

    def test_a_recorded_run_without_layers_is_refused(self) -> None:
        run = self.write_run([])
        gates = self.write_gates(0.6)

        completed = self.judge("--run", str(run), "--gates", str(gates))

        self.assertEqual(1, completed.returncode)
        self.assertIn("reports no layers", completed.stderr)


if __name__ == "__main__":
    unittest.main()
