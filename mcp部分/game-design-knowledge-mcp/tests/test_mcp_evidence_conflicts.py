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


class EvidenceConflictMcpTests(unittest.TestCase):
    def test_different_rules_in_the_same_section_are_reported_without_choosing_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "source"
            output = workspace / "index"
            source.mkdir()
            self._write_docx(source / "规则A.docx", "幸运转盘每日开放5次。")
            self._write_docx(source / "规则B.docx", "幸运转盘每日开放8次。")
            self._index(source, output)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(output)
            try:
                response = asyncio.run(self._search()).structured_content
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            self.assertEqual(response["status"], "found")
            self.assertEqual(len(response["evidence"]), 2)
            self.assertEqual(len(response["conflicts"]), 1)
            conflict = response["conflicts"][0]
            self.assertEqual(conflict["type"], "potential_conflict")
            self.assertEqual(conflict["section_path"], ["活动规则"])
            self.assertEqual(
                {item["text"] for item in conflict["evidence"]},
                {"幸运转盘每日开放5次。", "幸运转盘每日开放8次。"},
            )
            self.assertNotIn("preferred_evidence_id", conflict)

    @staticmethod
    async def _search():
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return await client.call_tool("search_evidence", {"query": "幸运转盘"})

    @staticmethod
    def _index(source: Path, output: Path) -> None:
        project_root = Path(__file__).parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.fspath(project_root / "src")
        completed = subprocess.run(
            [sys.executable, "-m", "game_design_knowledge.cli", "index", os.fspath(source), "--output", os.fspath(output)],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)

    @staticmethod
    def _write_docx(path: Path, rule: str) -> None:
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>活动规则</w:t></w:r></w:p>
    <w:p><w:r><w:t>{rule}</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
        relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relationships_xml)


if __name__ == "__main__":
    unittest.main()
