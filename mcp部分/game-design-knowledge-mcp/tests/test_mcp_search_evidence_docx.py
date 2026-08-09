from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from mcp import Client


class SearchDocxEvidenceMcpTests(unittest.TestCase):
    def test_ai_can_find_a_docx_rule_with_its_section_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            document_path = source_dir / "活动系统.docx"
            self._write_docx(document_path)
            self._index(source_dir, output_dir)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                result = asyncio.run(self._search("每日可参与5次"))
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            response = result.structured_content
            self.assertEqual(response["status"], "found")
            self.assertEqual(response["query"], "每日可参与5次")
            self.assertEqual(response["match_type"], "exact")
            self.assertEqual(len(response["evidence"]), 1)
            evidence = response["evidence"][0]
            self.assertEqual(evidence["source_document"], str(document_path.resolve()))
            self.assertEqual(evidence["document_type"], "docx")
            self.assertEqual(evidence["evidence_type"], "paragraph")
            self.assertEqual(evidence["text"], "玩家每日可参与5次。")
            self.assertEqual(evidence["section_path"], ["活动系统", "幸运转盘"])
            self.assertEqual(evidence["locator"], {"paragraph_index": 3})

    def test_missing_feature_is_not_replaced_with_a_similar_document_term(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            self._write_docx(source_dir / "活动系统.docx")
            self._index(source_dir, output_dir)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                response = asyncio.run(self._search("大风车")).structured_content
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            self.assertEqual(response["status"], "not_found")
            self.assertIsNone(response["match_type"])
            self.assertEqual(response["evidence"], [])
            self.assertEqual(response["conflicts"], [])
            self.assertEqual(
                response["limitations"],
                ["当前索引的文档原文中未找到该查询，且未进行相似玩法联想。"],
            )
            self.assertEqual(response["index_status"]["is_stale"], False)
            self.assertNotIn("suggestions", response)

    def test_search_marks_document_evidence_as_stale_after_the_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            document_path = source_dir / "活动系统.docx"
            self._write_docx(document_path)
            self._index(source_dir, output_dir)
            self._write_docx(document_path, participation_count=8)
            changed_stat = document_path.stat()
            os.utime(
                document_path,
                ns=(changed_stat.st_atime_ns, changed_stat.st_mtime_ns + 5_000_000_000),
            )

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                response = asyncio.run(self._search("每日可参与5次")).structured_content
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            self.assertEqual(response["status"], "stale")
            self.assertEqual(response["match_type"], "exact")
            self.assertEqual(len(response["evidence"]), 1)
            self.assertEqual(response["index_status"]["is_stale"], True)

    @staticmethod
    async def _search(query: str):
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return await client.call_tool("search_evidence", {"query": query, "limit": 20})

    @staticmethod
    def _index(source_dir: Path, output_dir: Path) -> None:
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root / "src")
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
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)

    @staticmethod
    def _write_docx(path: Path, participation_count: int = 5) -> None:
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>活动系统</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>幸运转盘</w:t></w:r></w:p>
    <w:p><w:r><w:t>玩家每日可参与{participation_count}次。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
        relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
        content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relationships_xml)


if __name__ == "__main__":
    unittest.main()
