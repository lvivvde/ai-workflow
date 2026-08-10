from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from mcp import Client

from game_design_knowledge.server import mcp


class GetEvidenceMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_docx_context_never_crosses_into_another_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "source"
            output = workspace / "index"
            source.mkdir()
            self._write_docx(
                source / "转盘.docx",
                [
                    ("Heading1", "活动系统"),
                    ("Heading2", "幸运转盘"),
                    (None, "玩家每日可参与5次。"),
                ],
            )
            self._write_docx(source / "其他.docx", [(None, "不应成为相邻上下文。")])
            self._index(source, output)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(output)
            try:
                async with Client(mcp) as client:
                    search = await client.call_tool(
                        "search_evidence", {"query": "每日可参与5次"}
                    )
                    evidence_id = search.structured_content["evidence"][0]["evidence_id"]
                    result = await client.call_tool(
                        "get_evidence",
                        {
                            "evidence_id": evidence_id,
                            "context_before": 1,
                            "context_after": 1,
                        },
                    )
                    missing = await client.call_tool(
                        "get_evidence", {"evidence_id": 999999}
                    )
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            response = result.structured_content
            self.assertEqual(response["status"], "found")
            self.assertEqual(response["evidence"]["text"], "玩家每日可参与5次。")
            self.assertEqual(response["evidence"]["block_ordinal"], 3)
            self.assertEqual(len(response["context_before"]), 1)
            self.assertEqual(response["context_before"][0]["text"], "幸运转盘")
            self.assertEqual(response["context_after"], [])
            self.assertFalse(response["index_status"]["is_stale"])
            self.assertEqual(missing.structured_content["status"], "not_found")
            self.assertFalse(
                missing.structured_content["index_status"]["is_stale"]
            )

    @staticmethod
    def _index(source: Path, output: Path) -> None:
        project_root = Path(__file__).parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.fspath(project_root / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "game_design_knowledge.cli",
                "index",
                os.fspath(source),
                "--output",
                os.fspath(output),
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)

    @staticmethod
    def _write_docx(path: Path, paragraphs: list[tuple[str | None, str]]) -> None:
        body = []
        for style, text in paragraphs:
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
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relationships_xml)


if __name__ == "__main__":
    unittest.main()
