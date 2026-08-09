from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from mcp import Client

from game_design_knowledge.server import mcp


DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>活动系统</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>通关后领取奖励</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>次数</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>5</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""

EMPTY_RELATIONSHIPS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""


class SearchDocxStructureEvidenceMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_receives_list_and_table_cell_evidence_with_stable_locators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "source"
            output_path = temporary_path / "index"
            source_path.mkdir()
            document_path = source_path / "activity.docx"
            with zipfile.ZipFile(document_path, "w") as archive:
                archive.writestr("word/document.xml", DOCUMENT_XML)
                archive.writestr(
                    "word/_rels/document.xml.rels", EMPTY_RELATIONSHIPS_XML
                )

            subprocess.run(
                [
                    os.fspath(Path(__file__).parents[1] / ".venv" / "Scripts" / "game-design-knowledge.exe"),
                    "index",
                    os.fspath(source_path),
                    "--output",
                    os.fspath(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(output_path)
            try:
                async with Client(mcp) as client:
                    list_result = await client.call_tool(
                        "search_evidence",
                        {"query": "通关后领取奖励", "limit": 20},
                    )
                    table_result = await client.call_tool(
                        "search_evidence", {"query": "次数", "limit": 20}
                    )
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            list_response = json.loads(list_result.content[0].text)
            self.assertEqual(list_response["status"], "found")
            self.assertEqual(len(list_response["evidence"]), 1)
            list_evidence = list_response["evidence"][0]
            self.assertEqual(list_evidence["evidence_type"], "list_item")
            self.assertEqual(list_evidence["section_path"], ["活动系统"])
            self.assertEqual(list_evidence["block_ordinal"], 2)
            self.assertEqual(
                list_evidence["locator"],
                {"paragraph_index": 2, "list_level": 0},
            )

            table_response = json.loads(table_result.content[0].text)
            self.assertEqual(table_response["status"], "found")
            self.assertEqual(len(table_response["evidence"]), 1)
            table_evidence = table_response["evidence"][0]
            self.assertEqual(table_evidence["evidence_type"], "table_cell")
            self.assertEqual(table_evidence["section_path"], ["活动系统"])
            self.assertEqual(table_evidence["block_ordinal"], 5)
            self.assertEqual(
                table_evidence["locator"],
                {"table_index": 1, "row_index": 1, "cell_index": 1},
            )


if __name__ == "__main__":
    unittest.main()
