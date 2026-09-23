"""V2-05: the reading order and the relations as the index actually keeps them.

The geometry rules are tested against the spec samples in ``test_layout.py``.
This file is about the other half of the promise: an image's layout is written
beside the transcription it came from, it survives a rebuild honestly, it is
reported by ``index_status``, and a reader can fetch it back per image.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mcp import Client

try:
    from tests.document_fixtures import arrow_png, write_docx_with_image
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import arrow_png, write_docx_with_image
from game_design_knowledge.flow_notation import (
    CLAIM_BOUNDARY,
    RELATION_RULESET_VERSION,
)
from game_design_knowledge.indexer import index_documents
from game_design_knowledge.layout import RULESET_VERSION
from game_design_knowledge.ocr_regions import (
    BoundingBox,
    OcrRegion,
    RegionObservation,
)
from game_design_knowledge.shared_index import index_status_for_database


def region(
    index: int, text: str, x: float, y: float, *, confidence: float = 0.93
) -> OcrRegion:
    """One region of the fixture picture; the ``↓`` box matches the drawn arrow."""

    return OcrRegion(
        index=index,
        text=text,
        bbox=BoundingBox(x, y, 80.0 if text != "↓" else 10.0, 20.0),
        reading_order=index,
        text_confidence=confidence,
        region_confidence=0.97,
        language="chi_sim",
    )


def flow_observation(*, provider_status: str = "succeeded") -> RegionObservation:
    """One picture of the notation spec: text, arrow block, next text."""

    regions = (
        region(0, "点击购买", 100.0, 0.0, confidence=0.93),
        region(1, "↓", 135.0, 30.0, confidence=0.9),
        region(2, "扣除钻石", 100.0, 60.0, confidence=0.91),
    )
    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status=provider_status,
        regions=regions if provider_status == "succeeded" else (),
        language="chi_sim+eng",
    )


class LayoutIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = self.root / ".index" / "knowledge"
        write_docx_with_image(
            self.root / "docs" / "玩法.docx",
            "购买按钮",
            # The arrow block's pixels point down, and the transcription below
            # reads ``↓``, so the two readings corroborate each other (issue #28).
            arrow_png("down", box=(135, 30, 10, 20), size=(240, 110)),
        )

    def index(self, observation: RegionObservation | None = None) -> dict[str, object]:
        provider = (
            {"rapidocr": lambda path: flow_observation()}
            if observation is None
            else {"rapidocr": lambda path: observation}
        )
        return index_documents(
            self.root,
            self.index_directory,
            ocr_engine="rapidocr",
            ocr_providers=provider,
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_directory / "knowledge.sqlite")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def test_a_flow_is_stored_with_its_endpoints_and_both_confidences(self) -> None:
        report = self.index()

        self.assertEqual(report["layout_elements"], 3)
        self.assertEqual(report["layout_relations"], 1)
        self.assertEqual(report["layout_confirmed_relations"], 1)

        connection = self.connect()
        run = connection.execute("SELECT * FROM layout_runs").fetchone()
        self.assertEqual(run["ruleset_version"], RULESET_VERSION)
        self.assertEqual(run["order_source"], "geometry")
        self.assertEqual(run["column_count"], 1)
        self.assertEqual(run["element_count"], 3)
        self.assertEqual(run["geometry_confidence"], 1.0)

        elements = connection.execute(
            "SELECT * FROM layout_elements ORDER BY reading_order"
        ).fetchall()
        self.assertEqual([row["region_index"] for row in elements], [0, 1, 2])
        self.assertEqual(
            [row["kind"] for row in elements], ["text", "arrow", "text"]
        )
        self.assertEqual(elements[1]["direction"], "down")
        self.assertEqual(
            [row["depth_hint"] for row in elements],
            [0, 0, 0],
            "one indent level per column start",
        )

        relation = connection.execute("SELECT * FROM structural_relations").fetchone()
        self.assertEqual(relation["kind"], "next_step")
        self.assertEqual(relation["status"], "confirmed")
        self.assertEqual(
            (relation["source_region"], relation["via_regions"], relation["target_region"]),
            (0, "[1]", 2),
            "a confirmed step can be traced back to the arrow and both nodes",
        )
        self.assertEqual(relation["rule_version"], RELATION_RULESET_VERSION)
        self.assertEqual(relation["claim_boundary"], CLAIM_BOUNDARY)
        self.assertEqual(relation["geometry_confidence"], 1.0)
        self.assertEqual(
            relation["ocr_confidence"],
            0.9,
            "the transcription layer of the weakest region it rests on",
        )
        self.assertIn("arrow block 1", relation["geometry_basis"])

    def test_an_image_the_engine_could_not_read_orders_nothing(self) -> None:
        report = self.index(flow_observation(provider_status="unavailable"))

        self.assertEqual(report["layout_elements"], 0)
        self.assertEqual(report["layout_relations"], 0)
        connection = self.connect()
        run = connection.execute("SELECT * FROM layout_runs").fetchone()
        self.assertEqual(
            run["element_count"],
            0,
            "an image with no regions still records that it was looked at",
        )
        self.assertEqual(run["ruleset_version"], RULESET_VERSION)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM layout_elements").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM structural_relations"
            ).fetchone()[0],
            0,
        )

    def test_removing_the_document_takes_its_layout_with_it(self) -> None:
        self.index()
        (self.root / "docs" / "玩法.docx").unlink()

        self.index()

        connection = self.connect()
        for table in ("layout_runs", "layout_elements", "structural_relations"):
            self.assertEqual(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                f"{table} must not outlive the document it describes",
            )

    def test_index_status_reports_the_layout_layer(self) -> None:
        self.index()

        status = index_status_for_database(self.index_directory / "knowledge.sqlite")

        self.assertEqual(status["layout_runs"], 1)
        self.assertEqual(status["layout_images"], 1)
        self.assertEqual(status["layout_elements"], 3)
        self.assertEqual(status["layout_relations"], 1)
        self.assertEqual(status["layout_confirmed_relations"], 1)
        self.assertEqual(status["layout_candidate_relations"], 0)
        self.assertEqual(status["layout_rulesets"], {RULESET_VERSION: 1})
        self.assertEqual(
            status["relation_rulesets"], {RELATION_RULESET_VERSION: 1}
        )

    def test_an_index_without_the_layout_tables_reports_zeros(self) -> None:
        self.index()
        with closing(
            sqlite3.connect(self.index_directory / "knowledge.sqlite")
        ) as connection, connection:
            for table in ("structural_relations", "layout_elements", "layout_runs"):
                connection.execute(f"DROP TABLE {table}")

        status = index_status_for_database(self.index_directory / "knowledge.sqlite")

        self.assertEqual(status["layout_runs"], 0)
        self.assertEqual(status["layout_elements"], 0)
        self.assertEqual(status["layout_relations"], 0)
        self.assertEqual(status["layout_uncertainty"], {})
        self.assertEqual(
            status["ocr_regions"],
            3,
            "the transcription the layout was read from is still reported",
        )

    def test_a_caller_can_fetch_the_order_and_the_relation_back(self) -> None:
        self.index()

        context = self._image_context()

        self.assertEqual(context["status"], "found")
        layout = context["layout"]
        self.assertEqual(layout["run"]["ruleset_version"], RULESET_VERSION)
        self.assertEqual(
            [(element["region_index"], element["kind"]) for element in layout["elements"]],
            [(0, "text"), (1, "arrow"), (2, "text")],
        )
        self.assertEqual(
            [element["depth_hint"] for element in layout["elements"]], [0, 0, 0]
        )
        relation = layout["relations"][0]
        self.assertEqual(relation["kind"], "next_step")
        self.assertEqual(relation["status"], "confirmed")
        self.assertEqual((relation["source_region"], relation["target_region"]), (0, 2))
        self.assertEqual(relation["via_regions"], [1])
        self.assertEqual(relation["claim_boundary"], CLAIM_BOUNDARY)
        self.assertEqual(
            context["ocr"]["regions"][0]["region_index"],
            0,
            "the transcription it was read from travels beside it",
        )

    def _image_context(self) -> dict[str, object]:
        previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
        os.environ["GAME_DESIGN_INDEX_DIR"] = str(self.index_directory)
        try:
            return asyncio.run(self._call_image_context())
        finally:
            if previous_index is None:
                os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
            else:
                os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

    @staticmethod
    async def _call_image_context() -> dict[str, object]:
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool("get_image_context", {"image_id": 1})
        return dict(result.structured_content)


if __name__ == "__main__":
    unittest.main()
