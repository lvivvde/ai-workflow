"""V2-09 at the tool surface: what a caller sees when it asks the V2 way.

The contract tests check the retrieval rules directly. These check the tool a
caller actually reaches: what it is called with, whether the V1 search tool is
still untouched, and whether a missing capability shows up in the answer
instead of disappearing from it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from mcp import Client

from game_design_knowledge.index_build import build_index_atomically
from game_design_knowledge.indexer import index_documents
from game_design_knowledge.ocr_regions import BoundingBox, OcrRegion, RegionObservation
from game_design_knowledge.server import mcp

try:
    from tests.document_fixtures import png_bytes, write_docx_with_image
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import png_bytes, write_docx_with_image


CATALOG = {
    "version": 1,
    "features": [
        {
            "key": "lucky-wheel",
            "canonical_name": "幸运转盘",
            "source": "knowledge/catalog.json",
            "aliases": [
                {
                    "name": "转盘",
                    "source": "knowledge/catalog.json",
                    "confirmed_at": "2026-08-09",
                    "confirmed_by": "测试策划",
                }
            ],
        }
    ],
}


class RetrievalMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-mcp-retrieval-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "project"
        self._saved_environment = {
            name: os.environ.get(name)
            for name in ("GAME_DESIGN_INDEX_DIR", "GAME_DESIGN_PROJECT_ROOT")
        }

    def tearDown(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _index_activity_project(self) -> Path:
        _write_docx(
            self.root / "docs" / "活动系统.docx",
            [
                ("Heading1", "活动系统"),
                ("Heading2", "幸运转盘"),
                (None, "玩家每日可参与5次。"),
            ],
        )
        catalog_path = self.root / "knowledge" / "catalog.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8"
        )
        index_directory = self.root / ".index" / "knowledge"
        build_index_atomically(self.root, index_directory)
        return index_directory

    def _configure(self, index_directory: Path, *, project_root: bool = True) -> None:
        os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(index_directory)
        if project_root:
            os.environ["GAME_DESIGN_PROJECT_ROOT"] = os.fspath(self.root)
        else:
            os.environ.pop("GAME_DESIGN_PROJECT_ROOT", None)

    # -- the tool surface -------------------------------------------------

    async def test_the_v2_tool_is_listed_next_to_the_frozen_v1_one(self) -> None:
        async with Client(mcp) as client:
            listing = await client.list_tools()

        tools = {tool.name: tool for tool in listing.tools}
        self.assertIn("retrieve_evidence", tools)
        self.assertIn("search_evidence", tools)
        schema = tools["retrieve_evidence"].input_schema
        self.assertEqual(schema["required"], ["query"])
        self.assertEqual(schema["properties"]["mode"]["default"], "auto")
        self.assertEqual(schema["properties"]["limit"]["default"], 20)
        self.assertEqual(schema["properties"]["include_candidates"]["default"], False)

    async def test_a_confirmed_alias_answers_with_the_rule_that_produced_it(
        self,
    ) -> None:
        index_directory = self._index_activity_project()
        self._configure(index_directory)

        response = await self._call("retrieve_evidence", {"query": "转盘"})

        self.assertEqual(response["status"], "found")
        self.assertEqual(response["query"], "转盘")
        self.assertEqual(response["expansions"][0]["kind"], "catalog_alias")
        self.assertEqual(
            {item["match_type"] for item in response["evidence"]},
            {"confirmed_alias"},
        )
        self.assertEqual(response["mode"], {"requested": "auto", "effective": "auto"})
        self.assertEqual(response["response_meta"]["channels"]["semantic"], "not_configured")
        self.assertLessEqual({"is_stale", "schema_version"}, set(response["index_status"]))

    async def test_an_exact_hit_comes_back_with_a_locator_a_person_can_check(
        self,
    ) -> None:
        index_directory = self._index_activity_project()
        self._configure(index_directory)

        response = await self._call(
            "retrieve_evidence", {"query": "玩家每日可参与5次。"}
        )

        evidence = response["evidence"][0]
        self.assertEqual(response["status"], "found")
        self.assertEqual(evidence["match_type"], "exact")
        self.assertEqual(evidence["locator"], {"paragraph_index": 3})
        self.assertEqual(evidence["v2"]["display_locator"]["kind"], "docx_paragraph")
        self.assertEqual(evidence["section_path"], ["活动系统", "幸运转盘"])
        self.assertEqual(
            evidence["v2"]["source_reference"]["document_type"], "docx"
        )

    async def test_the_v1_search_tool_keeps_its_frozen_shape(self) -> None:
        index_directory = self._index_activity_project()
        self._configure(index_directory)

        response = await self._call("search_evidence", {"query": "玩家每日可参与5次。"})

        self.assertEqual(
            set(response),
            {
                "status",
                "query",
                "match_type",
                "evidence",
                "conflicts",
                "limitations",
                "index_status",
            },
        )
        self.assertEqual(response["match_type"], "exact")
        self.assertNotIn("notes", response["evidence"][0])

    # -- degradation ------------------------------------------------------

    async def test_a_hybrid_request_degrades_visibly_and_still_answers(self) -> None:
        index_directory = self._index_activity_project()
        self._configure(index_directory)

        response = await self._call(
            "retrieve_evidence",
            {"query": "玩家每日可参与5次。", "mode": "hybrid"},
        )

        # A hybrid request this build cannot serve is degraded, not answered
        # silently: the status says so and the deterministic hits still arrive.
        self.assertEqual(response["status"], "degraded")
        self.assertEqual(response["mode"], {"requested": "hybrid", "effective": "auto"})
        self.assertTrue(response["evidence"])
        event = response["response_meta"]["degradation_events"][0]
        self.assertEqual(event["channel"], "semantic")
        self.assertEqual(event["reason"], "vector_capability_not_configured")
        self.assertTrue(response["response_meta"]["rebuild_vector_index_recommended"])

    async def test_an_unreadable_dictionary_degrades_without_blocking_the_search(
        self,
    ) -> None:
        index_directory = self._index_activity_project()
        state_directory = self.root / ".design-state"
        state_directory.mkdir(parents=True, exist_ok=True)
        # A dictionary this build cannot parse: an array where an object goes.
        (state_directory / "notation.json").write_text("[]", encoding="utf-8")
        self._configure(index_directory)

        response = await self._call(
            "retrieve_evidence", {"query": "玩家每日可参与5次。"}
        )

        self.assertEqual(response["status"], "degraded")
        self.assertEqual(response["evidence"][0]["text"], "玩家每日可参与5次。")
        self.assertEqual(
            response["retrieval"]["unavailable_capabilities"], ["notation_dictionary"]
        )
        event = response["response_meta"]["degradation_events"][0]
        self.assertEqual(event["channel"], "notation_dictionary")
        self.assertIn("notation_dictionary", " ".join(response["limitations"]))
        # Nothing about the fact itself changed: the same locator comes back.
        self.assertEqual(response["evidence"][0]["locator"], {"paragraph_index": 3})

    async def test_a_missing_namespace_degrades_while_fts5_still_answers(self) -> None:
        index_directory = self._index_activity_project()
        connection = sqlite3.connect(index_directory / "knowledge.sqlite")
        try:
            for table in ("ocr_regions", "ocr_runs", "structural_relations"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        finally:
            connection.close()
        self._configure(index_directory)

        response = await self._call(
            "retrieve_evidence", {"query": "玩家每日可参与5次。"}
        )

        self.assertEqual(response["status"], "degraded")
        self.assertEqual(
            [item["text"] for item in response["evidence"]],
            ["玩家每日可参与5次。"],
        )
        channels = {
            channel["channel"]: channel for channel in response["channels"]
        }
        self.assertEqual(channels["lexical"]["status"], "unavailable")
        self.assertEqual(channels["semantic"]["status"], "not_configured")

    async def test_an_unknown_mode_is_refused_instead_of_guessed(self) -> None:
        index_directory = self._index_activity_project()
        self._configure(index_directory)

        async with Client(mcp) as client:
            result = await client.call_tool(
                "retrieve_evidence", {"query": "幸运转盘", "mode": "fuzzy"}
            )

        self.assertTrue(result.is_error)
        self.assertIn("mode must be one of", result.content[0].text)

    # -- exploration ------------------------------------------------------

    async def test_exploration_candidates_stay_candidates_and_off_facts(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_docx_with_image(
            self.root / "docs" / "活动系统.docx", "战斗流程示意", png_bytes()
        )
        index_directory = self.root / ".index" / "knowledge"
        index_documents(
            self.root,
            index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": _unqualified_observation},
        )
        self._configure(index_directory)

        default = await self._call(
            "retrieve_evidence", {"query": "回合结束奖励规则"}
        )
        explored = await self._call(
            "retrieve_evidence",
            {"query": "回合结束奖励规则", "include_candidates": True},
        )

        self.assertEqual(default["status"], "not_found")
        self.assertEqual(default["candidates"], [])
        self.assertEqual(default["retrieval"]["held_back_unconfirmed"], 1)
        self.assertEqual(explored["status"], "not_found")
        self.assertEqual(explored["evidence"], [])
        candidate = explored["candidates"][0]
        self.assertEqual(candidate["namespace"], "unconfirmed_candidates")
        self.assertFalse(candidate["supports_project_fact"])
        self.assertEqual(candidate["evidence_status"], "candidate")
        self.assertTrue(
            any("探索模式" in note for note in explored["limitations"]),
            explored["limitations"],
        )

    @staticmethod
    async def _call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
        async with Client(mcp) as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise AssertionError(result.content[0].text)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)


def _unqualified_observation(path: Path) -> RegionObservation:
    """A run whose text did not clear the quality gate."""

    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status="succeeded",
        regions=(
            OcrRegion(
                index=0,
                text="回合结束奖励规则",
                bbox=BoundingBox(100.0, 0.0, 80.0, 20.0),
                reading_order=0,
                text_confidence=0.2,
                region_confidence=0.3,
                language="zh",
            ),
        ),
        language="zh",
    )


def _write_docx(path: Path, blocks: list[tuple[str | None, str]]) -> None:
    body = []
    for style, text in blocks:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>")
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{''.join(body)}</w:body>
</w:document>
"""
    relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships_xml)


if __name__ == "__main__":
    unittest.main()
