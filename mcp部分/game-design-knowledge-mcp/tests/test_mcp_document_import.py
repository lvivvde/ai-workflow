from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from mcp import Client

from game_design_knowledge.cli import _build_index_atomically
from game_design_knowledge.ingest import (
    apply_document_import,
    plan_document_import,
)
from game_design_knowledge.server import mcp


class DocumentImportMcpTests(unittest.TestCase):
    def test_ai_must_preview_and_confirm_before_copying_and_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            incoming = workspace / "incoming" / "活动系统.docx"
            index_directory = project_root / ".index" / "knowledge"
            incoming.parent.mkdir()
            project_root.mkdir()
            self._write_docx(incoming, "玩家每日可参与5次。")
            _build_index_atomically(project_root, index_directory)

            previous_root = os.environ.get("GAME_DESIGN_PROJECT_ROOT")
            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_PROJECT_ROOT"] = str(project_root)
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(index_directory)
            try:
                plan = asyncio.run(
                    self._call_tool(
                        "plan_document_import",
                        {
                            "source_paths": [str(incoming)],
                            "destination": "docs",
                            "operation": "copy",
                        },
                    )
                )
                destination = project_root / "docs" / "docx" / incoming.name
                self.assertEqual(plan["status"], "confirmation_required")
                self.assertFalse(destination.exists())

                unconfirmed = asyncio.run(
                    self._call_tool(
                        "import_documents",
                        {
                            "source_paths": [str(incoming)],
                            "destination": "docs",
                            "operation": "copy",
                            "plan_token": plan["plan_token"],
                            "confirmed": False,
                        },
                    )
                )
                self.assertEqual(unconfirmed["status"], "confirmation_required")
                self.assertFalse(destination.exists())

                completed = asyncio.run(
                    self._call_tool(
                        "import_documents",
                        {
                            "source_paths": [str(incoming)],
                            "destination": "docs",
                            "operation": "copy",
                            "plan_token": plan["plan_token"],
                            "confirmed": True,
                        },
                    )
                )
            finally:
                self._restore_environment(
                    "GAME_DESIGN_PROJECT_ROOT", previous_root
                )
                self._restore_environment("GAME_DESIGN_INDEX_DIR", previous_index)

            self.assertEqual(completed["status"], "completed")
            self.assertTrue(destination.is_file())
            self.assertTrue(incoming.is_file())
            self.assertEqual(completed["index_report"]["documents_indexed"], 1)
            self.assertFalse(completed["index_status"]["is_stale"])
            with closing(sqlite3.connect(index_directory / "knowledge.sqlite")) as connection:
                stored_path = connection.execute(
                    "SELECT path FROM documents"
                ).fetchone()[0]
            self.assertEqual(stored_path, "../../docs/docx/活动系统.docx")

    def test_failed_move_and_index_build_restores_source_and_old_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            incoming = workspace / "incoming" / "损坏配置.xlsx"
            index_directory = project_root / ".index" / "knowledge"
            incoming.parent.mkdir()
            project_root.mkdir()
            incoming.write_bytes(b"not-an-ooxml-archive")
            _build_index_atomically(project_root, index_directory)
            original_database = (index_directory / "knowledge.sqlite").read_bytes()

            plan = plan_document_import(
                [str(incoming)], project_root, "docs", "move"
            )
            with self.assertRaises(zipfile.BadZipFile):
                apply_document_import(
                    source_paths=[str(incoming)],
                    project_root=project_root,
                    index_directory=index_directory,
                    destination="docs",
                    operation="move",
                    plan_token=str(plan["plan_token"]),
                )

            self.assertTrue(incoming.is_file())
            self.assertFalse((project_root / "docs" / "xlsx" / incoming.name).exists())
            self.assertEqual(
                (index_directory / "knowledge.sqlite").read_bytes(),
                original_database,
            )

    def test_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            incoming = workspace / "incoming" / "活动系统.docx"
            existing = project_root / "docs" / "docx" / incoming.name
            incoming.parent.mkdir()
            existing.parent.mkdir(parents=True)
            self._write_docx(incoming, "新内容")
            self._write_docx(existing, "已有内容")

            with self.assertRaises(FileExistsError):
                plan_document_import(
                    [str(incoming)], project_root, "docs", "copy"
                )
            self.assertIn("已有内容", self._read_docx_text(existing))

    @staticmethod
    async def _call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
        if result.is_error:
            raise AssertionError(result.content[0].text)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    @staticmethod
    def _restore_environment(name: str, previous_value: str | None) -> None:
        if previous_value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous_value

    @staticmethod
    def _write_docx(path: Path, text: str) -> None:
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>
"""
        relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relationships_xml)

    @staticmethod
    def _read_docx_text(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            return archive.read("word/document.xml").decode("utf-8")


if __name__ == "__main__":
    unittest.main()
