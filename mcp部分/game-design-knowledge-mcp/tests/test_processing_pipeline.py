"""V2-03: staged processing, cache reuse, retry, and honest degradation."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from game_design_knowledge.index_revisions import (
    processing_manifest,
    stage_attempts,
    stage_attempt_summary,
)
from game_design_knowledge.pipeline import PIPELINE_NAMES, run_pipeline
from game_design_knowledge.processing import (
    DEFAULT_PIPELINE,
    STAGE_BY_NAME,
    StageCache,
    default_cache_root,
    downstream_stages,
)

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


class StagedProcessingTests(unittest.TestCase):
    def test_a_stage_retries_into_a_new_attempt_without_erasing_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root, index_directory = self._project(
                Path(temporary_directory), "玩家每日可参与5次。"
            )

            first = run_pipeline(project_root, index_directory)
            second = run_pipeline(
                project_root,
                index_directory,
                retry_stages=["ocr"],
                prior_history=first.history,
            )

            first_ocr = first.history["ocr"][-1]
            second_ocr = second.history["ocr"][-1]
            self.assertEqual(first_ocr.attempt_number, 1)
            self.assertEqual(second_ocr.attempt_number, 2)
            self.assertNotEqual(first_ocr.attempt_id, second_ocr.attempt_id)
            self.assertEqual(
                len(second.history["ocr"]),
                2,
                "history is append-only: the first attempt is still readable",
            )
            self.assertEqual(
                second.history["ocr"][0].attempt_id,
                first_ocr.attempt_id,
                "a retry never overwrites the attempt it replaces",
            )
            self.assertEqual(
                second.reused["source_parse"],
                first.history["source_parse"][-1].attempt_id,
                "a stage outside the retry set is carried forward, not re-run",
            )

    def test_a_retry_touches_only_the_stage_and_its_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root, index_directory = self._project(
                Path(temporary_directory), "每日开放5次"
            )
            first = run_pipeline(project_root, index_directory)
            affected = set(downstream_stages("layout"))
            self.assertEqual(
                affected,
                {
                    "layout",
                    "structure_relations",
                    "notation",
                    "statements",
                    "explanation_cache",
                },
                "everything that transitively consumes layout's output is affected",
            )

            second = run_pipeline(
                project_root,
                index_directory,
                retry_stages=["layout"],
                prior_history=first.history,
            )

            self.assertEqual(
                set(second.reused),
                {"source_parse", "ocr", "retrieval_projection"},
                "only the stages layout cannot reach are carried forward",
            )
            for stage in sorted(affected):
                self.assertNotIn(stage, second.reused)
                self.assertEqual(
                    len(second.history[stage]),
                    2,
                    f"{stage} is downstream of layout and is re-run",
                )

    def test_the_same_input_reuses_cache_and_new_bytes_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root, index_directory = self._project(workspace, "每日开放5次")
            document = project_root / "docs" / "玩法.docx"

            first = run_pipeline(project_root, index_directory)
            unchanged = run_pipeline(
                project_root, index_directory, prior_history=first.history
            )

            first_parse = first.history["source_parse"][-1]
            reused_parse = unchanged.history["source_parse"][-1]
            self.assertEqual(first_parse.fingerprint, reused_parse.fingerprint)
            self.assertTrue(
                reused_parse.cache_hit,
                "an identical input and fingerprint is served from the stage cache",
            )
            self.assertEqual(
                first_parse.output_sha256,
                reused_parse.output_sha256,
                "a cache hit returns the payload the first attempt produced",
            )

            write_docx(document, "每日开放8次")
            changed = run_pipeline(
                project_root, index_directory, prior_history=unchanged.history
            )
            changed_parse = changed.history["source_parse"][-1]

            self.assertNotEqual(first_parse.fingerprint, changed_parse.fingerprint)
            self.assertFalse(
                changed_parse.cache_hit,
                "different bytes must never be served the previous payload",
            )
            self.assertNotEqual(first_parse.output_sha256, changed_parse.output_sha256)
            self.assertNotEqual(
                first.history["ocr"][-1].fingerprint,
                changed.history["ocr"][-1].fingerprint,
                "a changed upstream invalidates the stage that consumes it",
            )

    def test_a_missing_optional_capability_is_degraded_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root, index_directory = self._project(
                Path(temporary_directory), "每日开放5次"
            )

            run = run_pipeline(project_root, index_directory)

            declared = run.history["layout"][-1]
            self.assertEqual(declared.execution_status, "unavailable")
            self.assertEqual(declared.quality_status, "rejected")
            self.assertEqual(declared.owner_ticket, "V2-05")
            self.assertEqual(declared.reason_code, "stage_not_implemented")
            self.assertIn("core processing continues", declared.detail)

            ocr = run.history["ocr"][-1]
            self.assertIn(ocr.execution_status, {"partial", "unavailable"})
            self.assertTrue(ocr.reason_chain, "the chain it walked is recorded")
            chain = {entry["engine"]: entry for entry in ocr.reason_chain}
            self.assertEqual(
                [entry["engine"] for entry in ocr.reason_chain],
                ["rapidocr", "paddleocr", "tesseract"],
                "the documented degradation order is preserved",
            )
            for engine in ("rapidocr", "paddleocr"):
                self.assertFalse(
                    chain[engine]["usable"],
                    f"{engine} is declared by this build but not usable yet",
                )
                self.assertIn("not implemented", chain[engine]["detail"])
            if ocr.execution_status == "unavailable":
                self.assertEqual(ocr.reason_code, "no_usable_engine")
                self.assertEqual(ocr.quality_status, "rejected")
            else:
                self.assertEqual(
                    ocr.execution_status,
                    "partial",
                    "a fallback engine is a degraded result, not a clean success",
                )
                self.assertTrue(ocr.fallback_used)

            self.assertEqual(
                run.projection_report.get("documents_indexed"),
                1,
                "core processing publishes the index even without the optional tiers",
            )
            self.assertEqual(run.blocking_failures(), [])

    def test_the_manifest_answers_which_runtime_model_and_fallback_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root, index_directory = self._project(
                Path(temporary_directory), "每日开放5次"
            )

            run = run_pipeline(project_root, index_directory)
            recorded = processing_manifest(index_directory / "knowledge.sqlite")

            self.assertIsNotNone(recorded)
            assert recorded is not None
            self.assertEqual(recorded["run_id"], run.run_id)
            self.assertEqual(
                recorded["configured_fingerprint"],
                run.configured_manifest.fingerprint,
            )
            self.assertEqual(
                [stage["name"] for stage in recorded["configured_manifest"]["stages"]],
                list(PIPELINE_NAMES),
            )
            self.assertEqual(recorded["profile"], run.profile)
            self.assertIn("ocr_concurrency", recorded["limits"])
            capability = recorded["capability"]
            self.assertEqual(
                sorted(capability["packs"]), ["core", "enhanced_ocr", "visual"]
            )
            self.assertTrue(
                capability["model_root"],
                "the manifest records where model artifacts would come from",
            )

            manifest_attempts = {
                (attempt["stage"], attempt["document_path"]): attempt
                for attempt in recorded["run_manifest"]["attempts"]
            }
            ocr_attempt = manifest_attempts[("ocr", "docs/玩法.docx")]
            self.assertTrue(ocr_attempt["reason_chain"])
            self.assertEqual(
                ocr_attempt["execution_status"], run.history["ocr"][-1].execution_status
            )
            self.assertEqual(
                manifest_attempts[("explanation_cache", "")]["execution_status"],
                "unavailable",
                "a stage that runs once per project is recorded once, not per document",
            )
            self.assertEqual(
                stage_attempts(index_directory / "knowledge.sqlite", stage="ocr")[0][
                    "attempt_id"
                ],
                ocr_attempt["attempt_id"],
                "the index keeps the same attempt the run recorded",
            )
            summary = stage_attempt_summary(index_directory / "knowledge.sqlite")
            self.assertEqual(
                sorted(summary["stages"]), sorted(name for name in PIPELINE_NAMES)
            )

    def test_the_cache_and_the_index_stay_separate_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root, index_directory = self._project(
                Path(temporary_directory), "每日开放5次"
            )
            run_pipeline(project_root, index_directory)

            cache_root = default_cache_root(index_directory)
            self.assertFalse(
                cache_root.is_relative_to(index_directory),
                "the published index keeps exactly the shape its readers expect",
            )
            self.assertTrue(cache_root.is_dir())
            self.assertTrue(any(cache_root.rglob("*.json")))
            self.assertFalse((index_directory / ".cache").exists())

            disposed = StageCache(cache_root).dispose()
            self.assertEqual(disposed["status"], "disposed")
            self.assertTrue(
                (index_directory / "knowledge.sqlite").is_file(),
                "disposing the cache never touches the published index",
            )

    def test_every_declared_stage_has_a_handler_and_an_owner(self) -> None:
        for stage in DEFAULT_PIPELINE:
            self.assertIn(stage.name, STAGE_BY_NAME)
            self.assertTrue(stage.owner, f"{stage.name} names the ticket that owns it")
            self.assertTrue(stage.handler, f"{stage.name} names its handler")
            if stage.upstream:
                for name in stage.upstream:
                    self.assertIn(name, PIPELINE_NAMES)

    def test_the_declared_handlers_and_the_registry_cannot_drift(self) -> None:
        from game_design_knowledge.pipeline import HANDLERS

        declared = {
            stage.name: stage.handler
            for stage in DEFAULT_PIPELINE
            if stage.name != "retrieval_projection"
        }
        self.assertEqual(
            sorted(HANDLERS),
            sorted(declared),
            "every declared stage except the projection has a registered handler",
        )
        for stage, handler in sorted(declared.items()):
            module, _, attribute = handler.partition(".")
            self.assertEqual(module, "pipeline", f"{stage}: handler must live in pipeline")
            self.assertTrue(
                getattr(HANDLERS[stage], "__name__", "") == attribute,
                f"{stage}: declared handler {handler} is not the registered callable",
            )

    @staticmethod
    def _project(workspace: Path, text: str) -> tuple[Path, Path]:
        project_root = workspace / "project"
        index_directory = project_root / ".index" / "knowledge"
        write_docx(project_root / "docs" / "玩法.docx", text)
        return project_root, index_directory


if __name__ == "__main__":
    unittest.main()
