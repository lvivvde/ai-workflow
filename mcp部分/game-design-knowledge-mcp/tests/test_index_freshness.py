from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mcp import Client

from game_design_knowledge.freshness import (
    CORE_DIMENSIONS,
    DIMENSIONS,
    OPTIONAL_DIMENSIONS,
    freshness_report,
)
from game_design_knowledge.ingest import rebuild_shared_index
from game_design_knowledge.snapshots import CURRENT_NAME

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


class FreshnessReportTests(unittest.TestCase):
    def test_every_layer_reports_its_own_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            document = project_root / "docs" / "玩法.docx"
            write_docx(document, "每日开放5次")
            rebuild_shared_index(project_root, index_directory)

            fresh = freshness_report(project_root, index_directory)
            self.assertEqual(sorted(fresh["dimensions"]), sorted(DIMENSIONS))
            self.assertTrue(fresh["is_fresh"], fresh["dimensions"])
            self.assertEqual(fresh["worst_state"], "fresh")
            self.assertEqual(fresh["status"], "fresh")
            for dimension in CORE_DIMENSIONS:
                self.assertEqual(fresh["dimensions"][dimension]["state"], "fresh")
            for dimension in OPTIONAL_DIMENSIONS:
                self.assertEqual(
                    fresh["dimensions"][dimension]["suggested_action"],
                    "not_configured",
                )
                self.assertTrue(
                    fresh["dimensions"][dimension]["optional"],
                    "a layer this version does not build yet is marked optional",
                )

            write_docx(document, "每日开放8次")
            changed = freshness_report(project_root, index_directory)

            self.assertFalse(changed["is_fresh"])
            self.assertEqual(changed["worst_state"], "stale")
            self.assertEqual(changed["blocking_dimensions"], ["source", "parse", "lexical_index"])
            self.assertEqual(changed["dimensions"]["source"]["state"], "stale")
            self.assertEqual(changed["dimensions"]["parse"]["state"], "stale")
            self.assertEqual(changed["dimensions"]["lexical_index"]["state"], "stale")
            self.assertEqual(
                changed["dimensions"]["durable_state"]["state"],
                "fresh",
                "a changed source file does not make the durable state itself stale",
            )
            self.assertEqual(
                changed["dimensions"]["lexical_index"]["suggested_action"],
                "rebuild_index",
            )

    def test_missing_layers_are_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")

            report = freshness_report(project_root, index_directory)

            self.assertFalse(report["is_fresh"])
            self.assertEqual(report["dimensions"]["source"]["state"], "missing")
            self.assertEqual(report["dimensions"]["durable_state"]["state"], "missing")
            self.assertEqual(report["dimensions"]["lexical_index"]["state"], "missing")

    def test_an_index_without_a_local_pointer_still_covers_the_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            rebuild_shared_index(project_root, index_directory)
            (index_directory / CURRENT_NAME).unlink()

            report = freshness_report(project_root, index_directory)

            self.assertTrue(report["is_fresh"], report["dimensions"])
            self.assertIn(
                "no local publication pointer",
                report["dimensions"]["lexical_index"]["detail"],
            )
            self.assertEqual(report["dimensions"]["parse"]["state"], "fresh")
            self.assertEqual(report["dimensions"]["source"]["state"], "fresh")

    def test_a_pointer_that_disagrees_with_the_index_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            rebuild_shared_index(project_root, index_directory)
            current_path = index_directory / CURRENT_NAME
            pointer = json.loads(current_path.read_text(encoding="utf-8"))
            pointer["current"]["parse_revisions"] = {"doc-edited": "par-edited"}
            current_path.write_text(json.dumps(pointer), encoding="utf-8")

            dimension = freshness_report(project_root, index_directory)["dimensions"][
                "lexical_index"
            ]

            self.assertEqual(dimension["state"], "stale")
            self.assertIn("disagree", dimension["detail"])

    def test_an_old_schema_index_is_incompatible_not_silently_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            rebuild_shared_index(project_root, index_directory)
            database_path = index_directory / "knowledge.sqlite"
            connection = sqlite3.connect(database_path)
            with connection:
                connection.execute("PRAGMA user_version = 2")
            connection.close()

            dimension = freshness_report(project_root, index_directory)["dimensions"][
                "lexical_index"
            ]

            self.assertEqual(dimension["state"], "incompatible")
            self.assertEqual(dimension["suggested_action"], "migrate_index")
            self.assertEqual(dimension["active_version"], "schema 2")


class FreshnessToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory(prefix="gdk-freshness-")
        workspace = Path(self._workspace.name)
        self.project_root = workspace / "project"
        self.index_directory = self.project_root / ".index" / "knowledge"
        write_docx(self.project_root / "docs" / "玩法.docx", "每日开放5次")
        rebuild_shared_index(self.project_root, self.index_directory)
        self._saved_environment = {
            name: os.environ.get(name)
            for name in (
                "GAME_DESIGN_INDEX_DIR",
                "GAME_DESIGN_PROJECT_ROOT",
                "GAME_DESIGN_STATE_DIR",
            )
        }
        os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(self.index_directory)
        os.environ["GAME_DESIGN_PROJECT_ROOT"] = os.fspath(self.project_root)
        os.environ.pop("GAME_DESIGN_STATE_DIR", None)

    def tearDown(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._workspace.cleanup()

    async def test_the_ai_can_read_each_layer_through_mcp(self) -> None:
        report = await self._call("index_freshness", {})

        self.assertTrue(report["is_fresh"], report["dimensions"])
        self.assertEqual(report["dimensions"]["source"]["state"], "fresh")
        self.assertEqual(report["dimensions"]["durable_state"]["state"], "fresh")
        self.assertEqual(report["dimensions"]["semantic_index"]["state"], "missing")

    async def test_index_status_keeps_v1_fields_and_adds_freshness(self) -> None:
        status = await self._call("index_status", {})

        for key in (
            "schema_version",
            "documents_indexed",
            "images_indexed",
            "ocr_succeeded",
            "stale_documents",
            "catalog_configured",
            "is_stale",
        ):
            self.assertIn(key, status)
        self.assertFalse(status["is_stale"])
        self.assertTrue(status["freshness"]["is_fresh"])

    @staticmethod
    async def _call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise AssertionError(result.content[0].text)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)


if __name__ == "__main__":
    unittest.main()
