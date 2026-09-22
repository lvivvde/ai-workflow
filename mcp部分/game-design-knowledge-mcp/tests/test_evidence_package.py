"""V2-06: the evidence package's own rules, before any index is involved.

The MCP surface is exercised in ``test_mcp_evidence_package.py``. This file
keeps the parts a reader can check without a database: what a retrieval unit id
may be, which sections exist, how an asset reference is minted, and that a
cursor can only come from a previous page.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

try:
    from tests.document_fixtures import write_png
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_png
from game_design_knowledge import indexer
from game_design_knowledge.evidence_package import (
    ASSET_REFERENCE_PREFIX,
    EVIDENCE_PACKAGE_VERSION,
    PACKAGE_SECTIONS,
    asset_reference,
    display_locator,
    evidence_package,
    looks_like_asset_reference,
    parse_unit_id,
    requested_sections,
    source_reference,
)
from game_design_knowledge.shared_index import SharedIndexRead


class UnitIdTests(unittest.TestCase):
    def test_a_unit_id_is_a_kind_and_a_row_number(self) -> None:
        self.assertEqual(parse_unit_id("evidence:12"), ("evidence", 12))
        self.assertEqual(parse_unit_id("  image:3  "), ("image", 3))

    def test_a_path_or_a_row_selector_is_never_a_unit_id(self) -> None:
        for value in (
            "docs/numbers.xlsx",
            "C:/project/docs/a.docx",
            "images",
            "evidence:1; DROP TABLE documents",
            "evidence:0",
            "evidence:abc",
            "",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_unit_id(value)


class SectionTests(unittest.TestCase):
    def test_every_layer_is_a_section_and_provenance_is_not(self) -> None:
        self.assertEqual(
            PACKAGE_SECTIONS,
            (
                "source",
                "statement",
                "transcription",
                "visual_interpretation",
                "notation",
                "explanation",
                "uncertainties",
            ),
        )

    def test_the_default_is_every_section_and_a_filter_keeps_its_order(self) -> None:
        self.assertEqual(requested_sections(None), PACKAGE_SECTIONS)
        self.assertEqual(
            requested_sections(["statement", "source", "statement"]),
            ("statement", "source"),
        )

    def test_a_section_this_build_cannot_serve_is_refused(self) -> None:
        with self.assertRaises(ValueError) as raised:
            requested_sections(["source", "causality"])

        self.assertIn("causality", str(raised.exception))
        with self.assertRaises(ValueError):
            requested_sections([])


class AssetReferenceTests(unittest.TestCase):
    def test_a_reference_is_derived_from_the_index_and_the_asset(self) -> None:
        first = asset_reference(Path("D:/project/.index/knowledge"), "assets/abc.png")

        self.assertEqual(
            first,
            asset_reference(
                Path("D:/project/.index/knowledge"), "assets\\abc.png"
            ),
        )
        self.assertNotEqual(
            first,
            asset_reference(
                Path("D:/project/.index/knowledge"), "assets/other.png"
            ),
        )
        self.assertNotEqual(
            first,
            asset_reference(Path("D:/project/.index/other"), "assets/abc.png"),
        )

    def test_a_path_is_never_mistaken_for_an_issued_reference(self) -> None:
        issued = asset_reference(Path("D:/project/.index/knowledge"), "assets/a.png")

        self.assertTrue(looks_like_asset_reference(issued))
        for value in (
            "assets/a.png",
            "D:/project/.index/knowledge/assets/a.png",
            f"{ASSET_REFERENCE_PREFIX}short",
            f"{ASSET_REFERENCE_PREFIX}{'z' * 32}",
            "",
        ):
            with self.subTest(value=value):
                self.assertFalse(looks_like_asset_reference(value))


class LocatorTests(unittest.TestCase):
    def test_a_display_locator_says_what_a_person_would_look_for(self) -> None:
        paragraph = display_locator(
            "docx", {"paragraph_index": 12}, section_path=["Gameplay", "Round"]
        )
        self.assertEqual(paragraph["kind"], "docx_paragraph")
        self.assertEqual(paragraph["label"], "paragraph 12")
        self.assertEqual(paragraph["section_path"], ["Gameplay", "Round"])

        cell = display_locator("xlsx", {"sheet_name": "Numbers", "cell_reference": "B3"})
        self.assertEqual(cell["kind"], "xlsx_cell")
        self.assertEqual(cell["label"], "Numbers!B3")

    def test_a_narrowed_reference_names_the_region_it_came_from(self) -> None:
        reference = source_reference(
            logical_document_id="doc-1",
            source_revision_id="src-1",
            parse_revision_id="par-1",
            path="docs/gameplay.docx",
            document_type="docx",
            locator={"paragraph_index": 2},
            region={"region_index": 4},
        )

        self.assertEqual(reference["region"], {"region_index": 4})
        self.assertEqual(reference["locator"]["paragraph_index"], 2)
        self.assertEqual(reference["schema_version"], "source-reference-v1")


class CursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = self.root / ".index" / "knowledge"

    def test_a_cursor_must_come_from_a_previous_page(self) -> None:
        write_png(self.root / "docs" / "png" / "flow.png")
        indexer.index_documents(self.root, self.index_directory)
        with SharedIndexRead(self.index_directory / "knowledge.sqlite") as index:
            with self.assertRaises(ValueError) as raised:
                evidence_package(index, unit_id="image:1", cursor="page-two")
            self.assertIn("next_cursor", str(raised.exception))

            package = evidence_package(index, unit_id="image:1", cursor="0")

        self.assertEqual(package["schema_version"], EVIDENCE_PACKAGE_VERSION)
        self.assertEqual(package["page"]["cursor"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
