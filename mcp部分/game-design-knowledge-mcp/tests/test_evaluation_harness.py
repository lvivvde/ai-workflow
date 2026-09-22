"""Tests for the offline evaluation harness and its release-blocking invariants.

Every invariant gets a negative test that injects a real violation, because an
invariant that cannot fail is indistinguishable from no invariant at all.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from typing import Sequence

from game_design_knowledge.evaluation import (
    RELEASE_BLOCKING_INVARIANTS,
    InvariantResult,
    SampleExecution,
    SchemaError,
    evaluate_invariants,
    load_corpus,
    run_evaluation,
    violation_summary,
)
from game_design_knowledge.evaluation.corpus import refresh_manifest
from game_design_knowledge.evaluation.invariants import (
    INVARIANT_DEGRADATION_NOT_HIDDEN,
    INVARIANT_NO_SILENT_CONFLICT_RESOLUTION,
    INVARIANT_NO_UNSUPPORTED_PROJECT_FACT,
    INVARIANT_SOURCE_EVIDENCE_UNCHANGED,
    INVARIANT_SOURCE_REFERENCES_TRACEABLE,
)
from game_design_knowledge.evaluation.network_guard import (
    NetworkAccessDuringEvaluation,
    block_network,
)
from game_design_knowledge.evaluation.reporting import render_markdown
from game_design_knowledge.evaluation.schema import EVALUATION_LAYERS, Sample


PROJECT_ROOT = Path(__file__).parents[1]
CORPORA_ROOT = PROJECT_ROOT / "evaluation" / "corpora"

RUN_PAYLOAD_KEYS = frozenset(
    {
        "manifest",
        "ok",
        "failures",
        "invariants",
        "layers",
        "strata",
        "response_states",
        "network_violations",
        "notes",
    }
)

STRATUM = {
    "source_type": "docx",
    "content_type": "rule_text",
    "language": "zh",
    "visual_quality": "n/a",
    "structure_complexity": "simple",
    "notation_complexity": "none",
    "conflict_state": "none",
    "capability_pack": "core",
    "difficulty": "basic",
}


def make_sample(**overrides: object) -> Sample:
    payload: dict[str, object] = {
        "sample_id": "unit-sample",
        "stratum": dict(STRATUM),
        "documents": [
            {"path": "a.docx", "generator": {"kind": "docx", "blocks": []}}
        ],
        "tool": "search_evidence",
        "arguments": {"query": "daily limit"},
        "expected": {"response_state": "found"},
        "annotation": {
            "annotators": ["ann-1", "ann-2"],
            "adjudicated": True,
            "guide_version": "eval-guide-0.1",
        },
    }
    payload.update(overrides)
    return Sample.from_payload(payload, "unit")


def make_execution(
    sample: Sample,
    *,
    response: dict[str, object] | None = None,
    error: str | None = None,
    **overrides: object,
) -> SampleExecution:
    return SampleExecution(
        sample=sample,
        mode=str(overrides.pop("mode", "component")),
        tool=str(overrides.pop("tool", sample.tool)),
        response=response,
        error=error,
        latency_seconds=float(overrides.pop("latency_seconds", 0.01)),
        **overrides,
    )


def traceable_evidence() -> dict[str, object]:
    return {
        "evidence_id": 1,
        "source_document": "a.docx",
        "locator": {"paragraph_index": 1},
        "text": "daily limit",
    }


class InvariantTests(unittest.TestCase):
    def assert_only_failing(
        self, name: str, results: Sequence[InvariantResult]
    ) -> list[str]:
        failing = [result.name for result in results if not result.ok]
        self.assertEqual(failing, [name], violation_summary(results))
        details = [
            violation.detail for result in results if result.name == name
            for violation in result.violations
        ]
        self.assertTrue(details)
        return details

    def test_a_clean_execution_passes_every_invariant(self) -> None:
        execution = make_execution(
            make_sample(),
            response={"status": "found", "evidence": [traceable_evidence()]},
        )

        results = evaluate_invariants([execution])

        self.assertEqual(
            [result.name for result in results], list(RELEASE_BLOCKING_INVARIANTS)
        )
        self.assertEqual([], violation_summary(results))

    def test_source_evidence_unchanged_flags_a_mutated_source(self) -> None:
        execution = make_execution(
            make_sample(),
            response={"status": "not_found"},
            sources_before={"a.docx": "before"},
            sources_after={"a.docx": "after"},
        )

        results = evaluate_invariants([execution])

        details = self.assert_only_failing(
            INVARIANT_SOURCE_EVIDENCE_UNCHANGED, results
        )
        self.assertTrue(any("content changed" in detail for detail in details))
        self.assertIn("unit-sample", violation_summary(results)[0])

    def test_no_unsupported_project_fact_flags_a_dangling_support_reference(
        self,
    ) -> None:
        claim = traceable_evidence() | {"supported_by": ["evidence_id=2"]}
        execution = make_execution(
            make_sample(), response={"status": "found", "evidence": [claim]}
        )

        results = evaluate_invariants([execution])

        details = self.assert_only_failing(
            INVARIANT_NO_UNSUPPORTED_PROJECT_FACT, results
        )
        self.assertTrue(any("evidence_id=2" in detail for detail in details))

    def test_no_unsupported_project_fact_flags_an_unreviewed_transcription(
        self,
    ) -> None:
        claim = traceable_evidence() | {
            "content_layer": "transcription",
            "evidence_status": "verified",
        }
        execution = make_execution(
            make_sample(), response={"status": "found", "evidence": [claim]}
        )

        results = evaluate_invariants([execution])

        details = self.assert_only_failing(
            INVARIANT_NO_UNSUPPORTED_PROJECT_FACT, results
        )
        self.assertTrue(any("transcription" in detail for detail in details))

    def test_no_silent_conflict_resolution_flags_a_selected_winner(self) -> None:
        execution = make_execution(
            make_sample(),
            response={
                "status": "found",
                "winner": "version-b",
                "evidence": [traceable_evidence()],
            },
        )

        results = evaluate_invariants([execution])

        details = self.assert_only_failing(
            INVARIANT_NO_SILENT_CONFLICT_RESOLUTION, results
        )
        self.assertTrue(any("winner" in detail for detail in details))

    def test_source_references_traceable_flags_a_claim_without_a_source(self) -> None:
        execution = make_execution(
            make_sample(),
            response={"status": "found", "evidence": [{"evidence_id": 1}]},
        )

        results = evaluate_invariants([execution])

        details = self.assert_only_failing(
            INVARIANT_SOURCE_REFERENCES_TRACEABLE, results
        )
        self.assertTrue(any("no source reference" in detail for detail in details))
        self.assertTrue(any("no locator" in detail for detail in details))

    def test_degradation_not_hidden_flags_a_hidden_degradation(self) -> None:
        execution = make_execution(
            make_sample(),
            response={"status": "not_found", "index_status": {}},
            recorded_degradation={"ocr_unavailable": 1},
        )

        results = evaluate_invariants([execution])

        details = self.assert_only_failing(INVARIANT_DEGRADATION_NOT_HIDDEN, results)
        self.assertTrue(any("ocr_unavailable" in detail for detail in details))

    def test_degradation_not_hidden_accepts_a_reported_degradation(self) -> None:
        execution = make_execution(
            make_sample(),
            response={
                "status": "not_found",
                "index_status": {"ocr_unavailable": 1},
            },
            recorded_degradation={"ocr_unavailable": 1},
        )

        results = evaluate_invariants([execution])

        self.assertEqual([], violation_summary(results))


class NetworkGuardTests(unittest.TestCase):
    def test_non_loopback_access_is_blocked_and_recorded(self) -> None:
        violations: list[str] = []

        with block_network(violations):
            with self.assertRaises(NetworkAccessDuringEvaluation):
                socket.create_connection(("93.184.216.34", 80), timeout=0.1)

        self.assertTrue(violations)
        self.assertIn("socket.create_connection", violations[0])

    def test_loopback_stays_available_for_asyncio_self_pipes(self) -> None:
        with block_network([]):
            socket.getaddrinfo("localhost", 80)


class CorpusProtocolTests(unittest.TestCase):
    def test_a_modified_sample_invalidates_the_recorded_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "v1_compatibility"
            shutil.copytree(CORPORA_ROOT / "v1_compatibility", root)
            sample_path = sorted((root / "samples").glob("*.json"))[0]
            payload = json.loads(sample_path.read_text(encoding="utf-8"))
            payload["notes"] = "edited after the manifest was written"
            sample_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(SchemaError):
                load_corpus(root)

            refresh_manifest(root)

            reloaded = load_corpus(root)
            self.assertEqual(
                reloaded.sample(payload["sample_id"]).notes,
                "edited after the manifest was written",
            )

    def test_frozen_corpus_refuses_to_be_refreshed_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "golden_set"
            shutil.copytree(CORPORA_ROOT / "golden_set", root)

            with self.assertRaises(SchemaError):
                refresh_manifest(root)

            self.assertTrue(refresh_manifest(root, force=True).is_file())
            self.assertTrue(load_corpus(root).manifest.frozen)

    def test_high_risk_sample_needs_two_annotators_and_adjudication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "development_set"
            shutil.copytree(CORPORA_ROOT / "development_set", root)
            sample_path = root / "samples" / "dev-no-answer.json"
            payload = json.loads(sample_path.read_text(encoding="utf-8"))
            payload["annotation"] = {
                "annotators": ["ann-1"],
                "adjudicated": False,
                "guide_version": payload["annotation"]["guide_version"],
            }
            sample_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            refresh_manifest(root)

            with self.assertRaises(SchemaError):
                load_corpus(root)

            relaxed = load_corpus(root, allow_unadjudicated_high_risk=True)
            self.assertTrue(relaxed.sample("dev-no-answer").high_risk)


class EvaluationRunTests(unittest.TestCase):
    def test_run_reports_layers_separately_and_passes_the_invariants(self) -> None:
        run = run_evaluation([load_corpus(CORPORA_ROOT / "v1_compatibility")])

        self.assertEqual([], run.failure_summary())
        self.assertTrue(run.ok)
        self.assertEqual({}, run.artifacts)

        payload = run.as_payload()
        self.assertEqual(RUN_PAYLOAD_KEYS, set(payload))
        self.assertFalse([key for key in payload if "score" in key.lower()])
        self.assertEqual([], payload["network_violations"])

        statuses = {layer.status for layer in run.layer_results}
        self.assertIn("measured", statuses)
        self.assertIn("unavailable", statuses)
        covered = {(layer.layer, layer.mode) for layer in run.layer_results}
        self.assertIn(("retrieval", "component"), covered)
        self.assertIn(("retrieval", "e2e"), covered)

        ocr = [
            layer
            for layer in run.layer_results
            if layer.layer == "ocr_transcription"
        ]
        self.assertTrue(ocr)
        self.assertTrue(all(layer.notes for layer in ocr))

        report = render_markdown(
            run_id=run.run_id,
            corpus_descriptions=run.corpus_descriptions,
            layer_results=run.layer_results,
            stratum_results=run.stratum_results,
            response_states=run.response_states,
            invariant_payloads=[
                result.as_payload() for result in run.invariant_results
            ],
            environment=run.manifest.environment,
        )
        self.assertIn("No layer is blended into a single score.", report)
        self.assertIn("### layout_regions", report)
        headings = [
            report.index(f"### {layer}")
            for layer in EVALUATION_LAYERS
            if f"### {layer}" in report
        ]
        self.assertEqual(
            headings,
            sorted(headings),
            "the report must follow the protocol's layer order",
        )

    def test_the_layout_layers_are_measured_once_regions_reach_the_ruleset(
        self,
    ) -> None:
        """V2-05: the two layout layers report real counts, not a fixed gap."""

        from game_design_knowledge.evaluation.harness import _layout_layers

        layers = _layout_layers(
            "component",
            {
                "images_indexed": 10,
                "layout_runs": 10,
                "layout_images": 8,
                "layout_elements": 40,
                "layout_relations": 6,
                "layout_confirmed_relations": 4,
                "layout_candidate_relations": 2,
                "layout_uncertain_relations": 2,
                "layout_uncertainty": {"": 8, "overlapping_boxes": 2},
                "layout_rulesets": {"layout-regions-v1": 10},
                "relation_rulesets": {"flow-arrow-v1": 6},
            },
            ("dev-001",),
        )

        layout, relations = layers
        self.assertEqual(layout.layer, "layout_regions")
        self.assertEqual(layout.status, "measured")
        self.assertEqual(layout.metrics["layout_elements_per_image"], 4.0)
        self.assertEqual(
            layout.metrics["layout_uncertain_images"],
            2,
            "only images that carry an uncertainty code are counted",
        )
        self.assertEqual(
            layout.metrics["layout_rulesets"], {"layout-regions-v1": 10}
        )
        self.assertEqual(relations.layer, "reading_order_relations")
        self.assertEqual(relations.status, "measured")
        self.assertEqual(relations.metrics["layout_relations"], 6)
        self.assertEqual(relations.metrics["layout_confirmed_relations"], 4)
        self.assertEqual(
            relations.metrics["relation_rulesets"], {"flow-arrow-v1": 6}
        )

        empty = _layout_layers(
            "component",
            {"images_indexed": 3, "layout_elements": 0},
            (),
        )
        self.assertTrue(all(layer.status == "unavailable" for layer in empty))
        self.assertTrue(all(layer.notes for layer in empty))

    def test_an_unknown_mode_is_rejected_instead_of_running_nothing(self) -> None:
        with self.assertRaises(ValueError):
            run_evaluation(
                [load_corpus(CORPORA_ROOT / "golden_set")], modes=("bogus",)
            )

    def test_a_run_that_executes_no_sample_is_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "component_only"
            shutil.copytree(CORPORA_ROOT / "v1_compatibility", root)
            for sample_path in list((root / "samples").glob("*.json")):
                payload = json.loads(sample_path.read_text(encoding="utf-8"))
                payload["modes"] = ["component"]
                sample_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            refresh_manifest(root)

            run = run_evaluation([load_corpus(root)], modes=("e2e",))

        self.assertEqual((), run.items)
        self.assertEqual((), run.layer_results)
        self.assertEqual({}, run.response_states)
        self.assertFalse(run.ok)
        self.assertTrue(
            any("no sample" in line for line in run.failure_summary()),
            run.failure_summary(),
        )


class EvaluateCliTests(unittest.TestCase):
    def test_a_typo_in_modes_fails_the_gate(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "evaluate.py"),
                "--modes",
                "bogus",
                "--no-artifacts",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("bogus", completed.stderr)


if __name__ == "__main__":
    unittest.main()
