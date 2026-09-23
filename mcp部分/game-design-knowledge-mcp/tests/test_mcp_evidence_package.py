"""V2-06: the evidence package through MCP, on a real index.

Every test here builds an index from the shared fixtures and then talks to the
server the way an AI client would: one retrieval unit at a time, an asset asked
for by reference, and a manifest asked for by tool. What is checked is the
promise the ticket makes -- layers stay apart, a derived item resolves to a
revision *and* a region, a page never splits a statement from its source, and
the V1 response stays exactly as it was unless V2 metadata is asked for.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mcp import Client

try:
    from tests.document_fixtures import arrow_png, png_bytes, write_docx_with_image
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import arrow_png, png_bytes, write_docx_with_image
from game_design_knowledge.evidence_package import EVIDENCE_PACKAGE_VERSION
from game_design_knowledge.indexer import index_documents
from game_design_knowledge.ocr_regions import (
    BoundingBox,
    OcrRegion,
    RegionObservation,
)
from game_design_knowledge.pipeline import run_pipeline


PARAGRAPH = "Tap the fire button to shoot"


def region(index: int, text: str, x: float, y: float) -> OcrRegion:
    return OcrRegion(
        index=index,
        text=text,
        bbox=BoundingBox(x, y, 80.0 if text != "↓" else 10.0, 20.0),
        reading_order=index,
        text_confidence=0.93,
        region_confidence=0.97,
        language="eng",
    )


def flow_observation() -> RegionObservation:
    """One picture of the notation spec: text, arrow block, next text."""

    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status="succeeded",
        regions=(
            region(0, "Start round", 100.0, 0.0),
            region(1, "↓", 135.0, 30.0),
            region(2, "Score a point", 100.0, 60.0),
        ),
        language="eng",
    )


def arrow_run_observation() -> RegionObservation:
    """Two arrow blocks in a row: geometry that may never be confirmed."""

    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status="succeeded",
        regions=(
            region(0, "Start round", 100.0, 0.0),
            region(1, "↓", 135.0, 30.0),
            region(2, "↓", 135.0, 60.0),
            region(3, "Score a point", 100.0, 90.0),
        ),
        language="eng",
    )


class EvidencePackageMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = self.root / ".index" / "knowledge"
        write_docx_with_image(
            self.root / "docs" / "docx" / "loop.docx",
            PARAGRAPH,
            # The drawn arrow points down and the transcription reads ``↓``, so
            # the step ``flow_observation`` describes is corroborated (issue #28).
            arrow_png("down", box=(135, 30, 10, 20), size=(240, 110)),
        )
        index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": lambda path: flow_observation()},
        )
        self._saved_environment = {
            name: os.environ.get(name)
            for name in ("GAME_DESIGN_INDEX_DIR", "GAME_DESIGN_PROJECT_ROOT")
        }
        os.environ["GAME_DESIGN_INDEX_DIR"] = str(self.index_directory)
        os.environ["GAME_DESIGN_PROJECT_ROOT"] = str(self.root)
        self.addCleanup(self._restore_environment)

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _call(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        return asyncio.run(self._call_async(tool, arguments))

    @staticmethod
    async def _call_async(
        tool: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return (await client.call_tool(tool, arguments)).structured_content

    def test_a_document_unit_keeps_its_statement_and_names_its_missing_layers(
        self,
    ) -> None:
        package = self._call("get_evidence_package", {"unit_id": "evidence:1"})

        self.assertEqual(package["status"], "found")
        self.assertEqual(package["schema_version"], EVIDENCE_PACKAGE_VERSION)
        self.assertFalse(package["index_status"]["is_stale"])

        statement = package["sections"]["statement"]
        self.assertEqual(len(statement), 1)
        self.assertEqual(statement[0]["text"], PARAGRAPH)
        self.assertEqual(statement[0]["authority"], "document")
        self.assertEqual(
            statement[0]["source_reference"],
            package["source_reference"],
        )

        # A quoted document block has no image layer, and saying so is not the
        # same as saying the picture was empty. The explanation layer, though,
        # is built from the layers this unit does carry, so it is served rather
        # than named as missing.
        codes = {entry["code"] for entry in package["unavailable"]}
        self.assertIn("unit_is_not_an_image", codes)
        self.assertNotIn("no_explanation_profile", codes)
        self.assertEqual(package["sections"]["transcription"], [])
        explanation = package["sections"]["explanation"]
        self.assertEqual(len(explanation), 1)
        self.assertEqual(explanation[0]["kind"], "explanation")
        self.assertEqual(explanation[0]["expand"]["tool"], "explain_evidence")
        self.assertEqual(
            explanation[0]["expand"]["arguments"]["unit_id"], package["unit_id"]
        )
        self.assertTrue(explanation[0]["explanation"]["atoms"])
        self.assertIn(
            PARAGRAPH, " ".join(atom["text"] for atom in explanation[0]["explanation"]["atoms"])
        )

        provenance = package["provenance"]
        self.assertTrue(provenance["source_revision_id"].startswith("src-"))
        self.assertTrue(provenance["parse_revision_id"].startswith("par-"))
        self.assertEqual(
            provenance["source_revision_id"],
            package["source_reference"]["source_revision_id"],
        )

    def test_a_derived_item_resolves_to_its_revision_and_its_region(self) -> None:
        package = self._call("get_evidence_package", {"unit_id": "image:1"})

        self.assertEqual(package["status"], "found")
        reference = package["source_reference"]
        self.assertTrue(reference["parse_revision_id"])

        regions = [
            item
            for item in package["sections"]["transcription"]
            if item["kind"] == "ocr_region"
        ]
        self.assertEqual([item["region"]["region_index"] for item in regions], [0, 1, 2])
        for item in regions:
            self.assertEqual(
                item["source_reference"]["source_revision_id"],
                reference["source_revision_id"],
            )
            self.assertEqual(
                item["source_reference"]["parse_revision_id"],
                reference["parse_revision_id"],
            )
            self.assertIsNotNone(item["source_reference"]["region"]["bbox"])

        elements = [
            item
            for item in package["sections"]["visual_interpretation"]
            if item["kind"] == "layout_element"
        ]
        self.assertEqual(len(elements), 3)
        self.assertEqual(elements[1]["element"]["direction"], "down")
        self.assertEqual(elements[0]["source_reference"]["region"]["region_index"], 0)

        relations = package["sections"]["notation"]
        self.assertEqual(len(relations), 1)
        relation = relations[0]["relation"]
        self.assertEqual(relation["kind"], "next_step")
        self.assertEqual(relation["status"], "confirmed")
        self.assertEqual((relation["source_region"], relation["target_region"]), (0, 2))
        self.assertTrue(relations[0]["claim_boundary"])
        self.assertEqual(
            relation["rule_version"],
            package["provenance"]["ruleset_versions"]["structural_relation"],
        )

    def test_a_package_pages_without_splitting_an_item_from_its_source(self) -> None:
        seen: list[dict[str, object]] = []
        cursor = ""
        pages = 0
        while True:
            package = self._call(
                "get_evidence_package",
                {"unit_id": "image:1", "limit": 2, "cursor": cursor},
            )
            pages += 1
            self.assertGreater(pages, 0)
            for items in package["sections"].values():
                seen.extend(items)
            page = package["page"]
            self.assertEqual(page["returned_items"], len(
                [item for items in package["sections"].values() for item in items]
            ))
            if not page["has_more"]:
                self.assertEqual(page["next_cursor"], "")
                total = page["total_items"]
                break
            cursor = page["next_cursor"]
            self.assertTrue(cursor)
            self.assertLess(pages, 20)

        self.assertGreater(total, 2)
        self.assertEqual(len(seen), total)
        for item in seen:
            if item["kind"] == "uncertainty":
                # An uncertainty names the layer it is about; it is not a fact
                # read out of the source, so it has no source reference to lose.
                self.assertTrue(item["code"])
                continue
            self.assertIn("source_reference", item)
            self.assertTrue(item["source_reference"]["source_revision_id"])
        # The explanation layer's own uncertainties live inside its atoms; at
        # this level the package only lists the ones a layer had to hedge about,
        # and this index has none. Its explanation is present all the same.
        explanation = [item for item in seen if item["kind"] == "explanation"]
        self.assertEqual(len(explanation), 1)
        self.assertEqual(explanation[0]["expand"]["tool"], "explain_evidence")
        by_kind = Counter(item["kind"] for item in seen)
        self.assertEqual(by_kind["ocr_region"], 3)
        self.assertEqual(by_kind["layout_element"], 3)
        self.assertEqual(by_kind["structural_relation"], 1)
        self.assertEqual(
            [
                kind
                for kind, count in by_kind.items()
                if count != 1 and kind not in {"ocr_region", "layout_element"}
            ],
            [],
        )

    def test_an_uncertainty_names_its_layer_and_not_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index_directory = root / ".index" / "knowledge"
            write_docx_with_image(
                root / "docs" / "docx" / "ambiguous.docx",
                PARAGRAPH,
                png_bytes(),
            )
            index_documents(
                root,
                index_directory,
                ocr_engine="rapidocr",
                ocr_providers={"rapidocr": lambda path: arrow_run_observation()},
            )

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            previous_root = os.environ.get("GAME_DESIGN_PROJECT_ROOT")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(index_directory)
            os.environ["GAME_DESIGN_PROJECT_ROOT"] = str(root)
            try:
                package = self._call("get_evidence_package", {"unit_id": "image:1"})
            finally:
                self._restore_pair(previous_index, previous_root)

        uncertainties = [
            item
            for item in package["sections"]["uncertainties"]
            if item["kind"] == "uncertainty"
        ]
        self.assertTrue(uncertainties)
        for item in uncertainties:
            self.assertTrue(item["code"])
            self.assertTrue(item["section"])
            # An uncertainty is about a layer, not a fact read out of the
            # source, so it names that layer instead of borrowing a locator.
            self.assertNotIn("source_reference", item)
        self.assertEqual(
            sorted(
                relation["relation"]["status"]
                for relation in package["sections"]["notation"]
            ),
            ["candidate", "candidate"],
        )

    def test_v2_metadata_is_opt_in_and_the_v1_hit_is_unchanged(self) -> None:
        plain = self._call("search_evidence", {"query": "fire"})

        self.assertEqual(plain["status"], "found")
        self.assertTrue(plain["evidence"])
        for hit in plain["evidence"]:
            self.assertNotIn("v2", hit)
            for removed in (
                "source_sha256",
                "logical_document_id",
                "source_revision_id",
                "parse_revision_id",
            ):
                self.assertNotIn(removed, hit)

        hydrated = self._call(
            "search_evidence", {"query": "fire", "include_v2_metadata": True}
        )

        first = hydrated["evidence"][0]
        self.assertEqual(first["v2"]["schema_version"], EVIDENCE_PACKAGE_VERSION)
        self.assertTrue(first["v2"]["unit_id"].startswith("evidence:"))
        self.assertTrue(first["v2"]["source_reference"]["parse_revision_id"])
        self.assertEqual(first["v2"]["display_locator"]["kind"], "docx_paragraph")
        self.assertEqual(
            first["v2"]["expand"]["arguments"]["unit_id"], first["v2"]["unit_id"]
        )

    def test_an_asset_is_handed_out_by_reference_and_never_by_path(self) -> None:
        package = self._call("get_evidence_package", {"unit_id": "image:1"})
        reference = package["asset_reference"]

        with closing(sqlite3.connect(self.index_directory / "knowledge.sqlite")) as db:
            stored = db.execute("SELECT asset_path, sha256 FROM images").fetchone()

        refused = self._call("get_asset", {"asset_reference": stored[0]})
        self.assertEqual(refused["status"], "not_found")
        self.assertIsNone(refused["asset"])

        found = self._call(
            "get_asset", {"asset_reference": reference, "include_content": True}
        )
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["asset"]["sha256"], stored[1])
        self.assertEqual(found["asset"]["image_id"], 1)
        self.assertTrue(Path(found["asset"]["path"]).is_file())
        self.assertTrue(Path(found["asset"]["path"]).is_relative_to(self.index_directory))

        content = found["content"]
        self.assertTrue(content["included"])
        self.assertEqual(content["sha256"], stored[1])
        self.assertTrue(content["sha256_matches_index"])

    def test_a_batch_keeps_every_package_that_resolved(self) -> None:
        batch = self._call(
            "get_evidence_packages",
            {"unit_ids": ["evidence:1", "image:1", "evidence:9999", "docs/a.docx"]},
        )

        self.assertEqual(batch["status"], "partial")
        self.assertEqual(batch["requested"], 4)
        self.assertEqual(batch["resolved"], 2)
        self.assertEqual(
            sorted(item["unit_id"] for item in batch["unresolved"]),
            ["docs/a.docx", "evidence:9999"],
        )
        self.assertEqual(
            sorted(item["status"] for item in batch["unresolved"]),
            ["invalid", "not_found"],
        )
        self.assertEqual(
            sorted(package["unit_id"] for package in batch["packages"]),
            ["evidence:1", "image:1"],
        )

    def test_the_manifest_and_degradation_are_reported_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index_directory = root / ".index" / "knowledge"
            write_docx_with_image(
                root / "docs" / "docx" / "loop.docx", PARAGRAPH, png_bytes()
            )
            run = run_pipeline(root, index_directory)
            run.raise_for_blocking_failures()

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            previous_root = os.environ.get("GAME_DESIGN_PROJECT_ROOT")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(index_directory)
            os.environ["GAME_DESIGN_PROJECT_ROOT"] = str(root)
            try:
                manifest = self._call("get_processing_manifest", {})
                status = self._call("index_status", {})
            finally:
                self._restore_pair(previous_index, previous_root)

        self.assertEqual(manifest["status"], "recorded")
        self.assertTrue(manifest["manifest"]["run_id"])
        self.assertEqual(manifest["stages"], status["processing"]["attempts"])
        self.assertEqual(
            manifest["degradation"]["recorded_attempts"],
            manifest["stages"]["attempts"],
        )
        # No OCR engine was available, so the geometry stages say so instead of
        # quietly disappearing.
        self.assertTrue(manifest["degradation"]["degraded"])
        self.assertTrue(manifest["degradation"]["degraded_stages"])

    @staticmethod
    def _restore_pair(index_value: str | None, root_value: str | None) -> None:
        for name, value in (
            ("GAME_DESIGN_INDEX_DIR", index_value),
            ("GAME_DESIGN_PROJECT_ROOT", root_value),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
