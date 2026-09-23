"""Region-level OCR through the real indexing path.

The engine calls are injected, so these tests exercise what V2-04 actually
delivers: embedded and standalone images reaching an engine, the chain walking
down when one cannot answer, the run/region/normalization rows, and the V1
counters staying exactly what they were.
"""

from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from document_fixtures import (
    png_bytes,
    write_docx,
    write_docx_with_image,
    write_jpeg,
    write_png,
    write_xlsx_with_image,
)
from game_design_knowledge.indexer import index_documents
from game_design_knowledge.ocr import RegionProvider, image_preflight
from game_design_knowledge.ocr_regions import BoundingBox, OcrRegion, RegionObservation
from game_design_knowledge.pipeline import run_pipeline
from game_design_knowledge.shared_index import index_status_for_database

try:
    from tests.host_stubs import no_ocr_engine_installed
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from host_stubs import no_ocr_engine_installed


def rapidocr_observation(
    text: str = "等级上限 １００ 级",
    *,
    confidence: float = 0.93,
    second: str | None = "成功率 50% -> 保持",
    provider_status: str = "succeeded",
    detail: str = "",
) -> RegionObservation:
    regions = [
        OcrRegion(
            index=0,
            text=text,
            bbox=BoundingBox(12.0, 24.0, 120.0, 28.0),
            reading_order=0,
            text_confidence=confidence,
            region_confidence=0.97,
            language="chi_sim",
        )
    ]
    if second is not None:
        regions.append(
            OcrRegion(
                index=1,
                text=second,
                bbox=BoundingBox(12.0, 60.0, 140.0, 28.0),
                reading_order=1,
                text_confidence=0.88,
                language="chi_sim",
            )
        )
    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status=provider_status,
        regions=tuple(regions) if provider_status == "succeeded" else (),
        language="chi_sim+eng",
        detail=detail,
    )


class ImageOcrTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = self.root / ".index" / "knowledge"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_directory / "knowledge.sqlite")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection


class EmbeddedImageTests(ImageOcrTestCase):
    def test_an_embedded_image_is_recorded_region_by_region(self) -> None:
        write_docx_with_image(self.root / "docs" / "玩法.docx", "购买按钮", png_bytes())
        providers: dict[str, RegionProvider] = {
            "rapidocr": lambda path: rapidocr_observation()
        }

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers=providers,
        )

        self.assertEqual(report["images_indexed"], 1)
        self.assertEqual(report["ocr_succeeded"], 1)
        self.assertEqual(report["ocr_regions"], 2)
        self.assertEqual(report["machine_supported_images"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["engine"], "rapidocr")
        self.assertEqual(run["engine_version"], "1.3.24")
        self.assertEqual(run["requested_engine"], "rapidocr")
        self.assertEqual(run["execution_status"], "succeeded")
        self.assertEqual(run["quality_status"], "accepted")
        self.assertEqual(run["evidence_state"], "machine-supported")
        self.assertEqual(run["region_count"], 2)
        self.assertEqual(run["fallback_used"], 0)
        regions = connection.execute(
            "SELECT * FROM ocr_regions ORDER BY reading_order"
        ).fetchall()
        self.assertEqual([row["text_raw"] for row in regions], [
            "等级上限 １００ 级",
            "成功率 50% -> 保持",
        ])
        self.assertEqual(regions[0]["text_confidence"], 0.93)
        self.assertEqual(regions[0]["region_confidence"], 0.97)
        self.assertEqual(
            regions[0]["key_mark_confidence"],
            0.93,
            "the region carries a number, so it sets the key-mark layer",
        )
        self.assertIn("120.0", regions[0]["bbox"])

    def test_an_xlsx_embedded_image_reaches_the_same_region_path(self) -> None:
        write_xlsx_with_image(
            self.root / "docs" / "数值.xlsx", "伤害曲线示意图", png_bytes()
        )

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: rapidocr_observation()},
        )

        self.assertEqual(report["images_indexed"], 1)
        self.assertEqual(report["ocr_regions"], 2)
        connection = self.connect()
        image = connection.execute("SELECT * FROM images").fetchone()
        self.assertEqual(image["sheet_name"], "数值配置")
        self.assertEqual(image["cell_anchor"], "C5")
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["execution_status"], "succeeded")
        self.assertEqual(run["region_count"], 2)

    def test_the_raw_transcription_is_kept_while_the_suggestion_is_stored_beside_it(self) -> None:
        write_docx_with_image(self.root / "docs" / "a.docx", "text", png_bytes())

        index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: rapidocr_observation(second=None)},
        )

        connection = self.connect()
        region = connection.execute("SELECT * FROM ocr_regions").fetchone()
        normalization = connection.execute("SELECT * FROM ocr_normalizations").fetchone()
        self.assertEqual(region["text_raw"], "等级上限 １００ 级")
        self.assertEqual(normalization["normalized_text"], "等级上限 100 级")
        self.assertNotEqual(normalization["normalized_text"], region["text_raw"])
        self.assertIn("fullwidth_ascii", normalization["changes"])
        self.assertEqual(
            connection.execute(
                "SELECT ocr_text FROM images"
            ).fetchone()["ocr_text"],
            "等级上限 １００ 级",
            "the V1 field carries the raw transcription, not the suggestion",
        )

    def test_no_suggestion_row_means_the_raw_text_already_is_the_reading(self) -> None:
        write_docx_with_image(self.root / "docs" / "a.docx", "text", png_bytes())

        index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: rapidocr_observation(second=None, text="等级上限 100")},
        )

        connection = self.connect()
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM ocr_normalizations").fetchone()[0], 0
        )

    def test_a_low_quality_result_stays_a_transcription(self) -> None:
        write_docx_with_image(self.root / "docs" / "a.docx", "text", png_bytes())

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={
                "rapidocr": lambda path: rapidocr_observation(confidence=0.2, second=None)
            },
        )

        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["execution_status"], "succeeded")
        self.assertEqual(run["quality_status"], "rejected")
        self.assertEqual(run["reason_code"], "below_quality_threshold")
        self.assertEqual(run["evidence_state"], "transcription")
        self.assertEqual(report["ocr_succeeded"], 1, "V1 still counts it as production")
        self.assertEqual(report["low_quality_images"], 1)
        self.assertEqual(report["machine_supported_images"], 0)


class StandaloneImageTests(ImageOcrTestCase):
    def test_png_and_jpeg_files_become_their_own_documents(self) -> None:
        write_png(self.root / "screens" / "等级界面.png")
        write_jpeg(self.root / "screens" / "购买弹窗.jpg")

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: rapidocr_observation()},
        )

        self.assertEqual(report["documents_indexed"], 2)
        self.assertEqual(report["images_indexed"], 2)
        connection = self.connect()
        documents = connection.execute(
            "SELECT document_type FROM documents ORDER BY path"
        ).fetchall()
        self.assertEqual([row["document_type"] for row in documents], ["image", "image"])
        images = connection.execute(
            "SELECT relationship_id, source_part, context_text FROM images ORDER BY source_part"
        ).fetchall()
        self.assertEqual([row["relationship_id"] for row in images], ["standalone", "standalone"])
        self.assertIn("screens/购买弹窗.jpg", [row["source_part"] for row in images])

    def test_a_corrupt_image_is_reported_as_corrupt_and_not_as_an_engine_failure(self) -> None:
        broken = self.root / "screens" / "broken.png"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"this is not a png at all")

        def never_called(path: Path) -> RegionObservation:
            raise AssertionError("a corrupt image must not reach an engine")

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": never_called},
        )

        self.assertEqual(report["ocr_failed"], 1)
        self.assertEqual(
            report["low_quality_images"],
            0,
            "a broken image is its own category, not low quality output",
        )
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["execution_status"], "failed")
        self.assertEqual(run["quality_status"], "rejected")
        self.assertEqual(run["reason_code"], "corrupt_image")
        self.assertEqual(run["evidence_state"], "transcription")
        self.assertEqual(run["region_count"], 0)
        self.assertEqual(run["reason_chain"], "[]")

    def test_preflight_classifies_missing_empty_and_unsupported_assets(self) -> None:
        missing = self.root / "nope.png"
        self.assertEqual(image_preflight(missing)[0], "failed")
        empty = self.root / "empty.png"
        empty.write_bytes(b"")
        self.assertEqual(image_preflight(empty)[0], "corrupt_image")
        vector = self.root / "drawing.emf"
        vector.write_bytes(b"\x01\x00\x00\x00EMF")
        self.assertEqual(image_preflight(vector)[0], "unsupported_format")
        good = self.root / "good.png"
        good.write_bytes(png_bytes())
        self.assertIsNone(image_preflight(good))


class DegradationTests(ImageOcrTestCase):
    def test_the_chain_falls_through_to_the_next_engine_and_says_so(self) -> None:
        write_png(self.root / "screens" / "a.png")

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={
                "rapidocr": lambda path: rapidocr_observation(
                    provider_status="failed", detail="onnxruntime crashed"
                ),
                "paddleocr": lambda path: rapidocr_observation(second=None),
            },
        )

        self.assertEqual(report["ocr_succeeded"], 1)
        self.assertEqual(report["fallback_images"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["requested_engine"], "rapidocr")
        self.assertEqual(run["engine"], "paddleocr")
        self.assertEqual(run["tier"], "enhanced")
        self.assertEqual(run["fallback_used"], 1)
        self.assertIn("rapidocr", run["reason_chain"])
        self.assertIn("paddleocr", run["reason_chain"])

    def test_the_compatibility_fallback_can_be_refused(self) -> None:
        write_png(self.root / "screens" / "a.png")

        # Only the compatibility tier could answer here, and it is switched off.
        with no_ocr_engine_installed():
            report = index_documents(
                self.root,
                self.index_directory,
                ocr_engine="rapidocr",
                ocr_providers={"tesseract": lambda path: rapidocr_observation()},
                allow_compatibility_fallback=False,
            )

        self.assertEqual(report["ocr_unavailable"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["execution_status"], "unavailable")
        self.assertEqual(run["reason_code"], "no_usable_engine")
        self.assertIn("disabled by configuration", run["reason_chain"])

    def test_a_slow_engine_times_out_without_losing_the_image(self) -> None:
        write_png(self.root / "screens" / "a.png")

        def slow(path: Path) -> RegionObservation:
            time.sleep(0.5)
            return rapidocr_observation()

        with mock.patch.dict(os.environ, {"GAME_DESIGN_OCR_TIMEOUT": "0.05"}):
            report = index_documents(
                self.root,
                self.index_directory,
                ocr_engine="rapidocr",
                ocr_providers={"rapidocr": slow},
            )

        self.assertEqual(report["images_indexed"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        image = connection.execute("SELECT * FROM images").fetchone()
        self.assertIsNotNone(image, "the image row survives the timeout")
        self.assertEqual(run["reason_code"], "ocr_timeout")
        self.assertEqual(run["execution_status"], "failed")
        self.assertEqual(run["quality_status"], "rejected")

    def test_a_missing_language_pack_is_reported_as_such(self) -> None:
        write_png(self.root / "screens" / "a.png")

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={
                "rapidocr": lambda path: RegionObservation(
                    engine="rapidocr",
                    provider_status="missing_language",
                    detail="Failed loading language 'chi_sim'",
                    language="chi_sim+eng",
                )
            },
        )

        self.assertEqual(report["ocr_unavailable"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["reason_code"], "missing_language_pack")
        self.assertEqual(run["evidence_state"], "transcription")

    def test_an_unknown_provider_status_does_not_break_the_build(self) -> None:
        write_png(self.root / "screens" / "a.png")

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: {"provider_status": "probably_fine"}},
        )

        self.assertEqual(report["ocr_failed"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["execution_status"], "failed")
        self.assertIn("probably_fine", run["detail"])

    def test_missing_model_files_are_reported_without_downloading_anything(self) -> None:
        write_png(self.root / "screens" / "a.png")

        report = index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={
                "rapidocr": lambda path: RegionObservation(
                    engine="rapidocr",
                    provider_status="models_missing",
                    detail="no local RapidOCR ONNX model files were found",
                )
            },
        )

        self.assertEqual(report["ocr_unavailable"], 1)
        connection = self.connect()
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["reason_code"], "models_not_installed")


class V1CompatibilityTests(ImageOcrTestCase):
    def test_without_an_engine_the_v1_behaviour_is_unchanged(self) -> None:
        write_docx_with_image(self.root / "docs" / "a.docx", "text", png_bytes())

        report = index_documents(self.root, self.index_directory)

        self.assertEqual(report["ocr_succeeded"], 0)
        self.assertEqual(report["ocr_failed"], 0)
        self.assertEqual(report["ocr_unavailable"], 1)
        connection = self.connect()
        image = connection.execute("SELECT * FROM images").fetchone()
        self.assertEqual(image["ocr_status"], "unavailable")
        self.assertEqual(image["ocr_text"], "")
        self.assertIn("not found on PATH", image["ocr_error"])
        run = connection.execute("SELECT * FROM ocr_runs").fetchone()
        self.assertEqual(run["execution_status"], "unavailable")
        self.assertEqual(run["reason_chain"], "[]", "no chain is walked in V1 mode")
        self.assertEqual(run["engine"], "tesseract")

    def test_index_status_keeps_the_v1_fields_and_adds_the_detail(self) -> None:
        write_png(self.root / "screens" / "a.png")
        index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: rapidocr_observation()},
        )

        status = index_status_for_database(self.index_directory / "knowledge.sqlite")

        for field in ("ocr_succeeded", "ocr_failed", "ocr_unavailable", "images_indexed"):
            self.assertIn(field, status)
        self.assertEqual(status["schema_version"], 6)
        self.assertEqual(status["ocr_runs"], 1)
        self.assertEqual(status["ocr_regions"], 2)
        self.assertEqual(status["ocr_normalizations"], 1)
        self.assertEqual(status["ocr_machine_supported"], 1)
        self.assertEqual(status["ocr_engines"], {"rapidocr": 1})
        self.assertEqual(status["ocr_execution_statuses"], {"succeeded": 1})


class PipelineOcrTests(ImageOcrTestCase):
    def test_the_pipeline_records_the_selection_and_the_runtime_fallthrough(self) -> None:
        write_docx(self.root / "docs" / "a.docx", "hello")
        write_png(self.root / "screens" / "a.png")

        # With no engine installed the run reports the whole chain as walked and
        # unavailable, while the injected provider still produces the regions.
        with no_ocr_engine_installed():
            run = run_pipeline(
                self.root,
                self.index_directory,
                ocr_providers={"rapidocr": lambda path: rapidocr_observation(second=None)},
            )

        ocr = run.history["ocr"][-1]
        self.assertEqual(
            [entry["engine"] for entry in ocr.reason_chain],
            ["rapidocr", "paddleocr", "tesseract"],
        )
        self.assertEqual(ocr.execution_status, "unavailable")
        self.assertEqual(ocr.reason_code, "no_usable_engine")
        connection = self.connect()
        region = connection.execute("SELECT * FROM ocr_regions").fetchone()
        self.assertIsNotNone(
            region,
            "the published index carries the regions the chain produced",
        )


class PaddleLanguageTests(unittest.TestCase):
    """The enhanced engine is configured with Tesseract codes, not its own.

    PaddleOCR has no language called ``chi``: ``ch`` is the model that reads
    Chinese and Latin script, and anything this build does not know is passed
    through so the engine names the language it cannot serve instead of a
    different one being transcribed silently.
    """

    def paddle_language(self, configured: str) -> str:
        from game_design_knowledge.ocr import (
            LANGUAGE_ENVIRONMENT_VARIABLE,
            _paddle_language,
        )

        with mock.patch.dict(
            os.environ, {LANGUAGE_ENVIRONMENT_VARIABLE: configured}
        ):
            return _paddle_language()

    def test_the_default_language_set_maps_to_the_chinese_model(self) -> None:
        self.assertEqual(self.paddle_language("chi_sim+eng"), "ch")
        self.assertEqual(self.paddle_language("eng+chi_sim"), "ch")
        self.assertEqual(self.paddle_language(""), "ch")

    def test_the_documented_languages_map_to_paddleocr_codes(self) -> None:
        for configured, expected in (
            ("eng", "en"),
            ("chi_tra", "chinese_cht"),
            ("jpn", "japan"),
            ("kor", "korean"),
            ("jpn+eng", "japan"),
        ):
            with self.subTest(language=configured):
                self.assertEqual(self.paddle_language(configured), expected)

    def test_a_language_this_build_does_not_know_is_passed_through(self) -> None:
        self.assertEqual(self.paddle_language("xyz"), "xyz")


if __name__ == "__main__":
    unittest.main()
