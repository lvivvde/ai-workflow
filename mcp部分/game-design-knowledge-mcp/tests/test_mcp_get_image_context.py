from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from mcp import Client


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class GetImageContextMcpTests(unittest.TestCase):
    def test_ai_can_get_docx_image_context_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            document_path = source_dir / "战斗系统.docx"
            self._write_docx(document_path)
            self._index(source_dir, output_dir)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                result = asyncio.run(self._call_tool())
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            context = result.structured_content
            self.assertEqual(context["source_document"], str(document_path.resolve()))
            self.assertEqual(context["document_type"], "docx")
            self.assertEqual(context["heading"], "战斗系统")
            self.assertEqual(context["paragraph_index"], 2)
            self.assertEqual(context["context_text"], "结算界面示意图")
            self.assertEqual(context["ocr_status"], "unavailable")
            self.assertTrue(Path(context["asset_path"]).is_file())

    def test_ai_can_search_images_by_nearby_text_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            document_path = source_dir / "战斗系统.docx"
            self._write_docx(document_path)
            self._index(source_dir, output_dir)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                result = asyncio.run(self._search_images("结算界面"))
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            matches = result.structured_content["matches"]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["image_id"], 1)
            self.assertEqual(matches[0]["context_text"], "结算界面示意图")
            self.assertEqual(matches[0]["source_document"], str(document_path.resolve()))

    def test_ai_can_check_index_status_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            self._write_docx(source_dir / "战斗系统.docx")
            self._index(source_dir, output_dir)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                result = asyncio.run(self._index_status())
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            status = result.structured_content
            self.assertEqual(status["schema_version"], 2)
            self.assertEqual(status["documents_indexed"], 1)
            self.assertEqual(status["images_indexed"], 1)
            self.assertEqual(status["ocr_succeeded"], 0)
            self.assertEqual(status["ocr_failed"], 0)
            self.assertEqual(status["ocr_unavailable"], 1)
            self.assertFalse(status["is_stale"])

    @staticmethod
    async def _call_tool():
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return await client.call_tool("get_image_context", {"image_id": 1})

    @staticmethod
    async def _search_images(query: str):
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return await client.call_tool("search_images", {"query": query, "limit": 10})

    @staticmethod
    async def _index_status():
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return await client.call_tool("index_status", {})

    @staticmethod
    def _index(source_dir: Path, output_dir: Path) -> None:
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root / "src")
        environment["PATH"] = ""
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "game_design_knowledge.cli",
                "index",
                str(source_dir),
                "--output",
                str(output_dir),
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        json.loads(completed.stdout)

    @staticmethod
    def _write_docx(path: Path) -> None:
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>战斗系统</w:t></w:r></w:p>
    <w:p><w:r><w:t>结算界面示意图</w:t></w:r><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p>
  </w:body>
</w:document>
"""
        relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/mockup.png"/>
</Relationships>
"""
        content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relationships_xml)
            archive.writestr("word/media/mockup.png", PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
