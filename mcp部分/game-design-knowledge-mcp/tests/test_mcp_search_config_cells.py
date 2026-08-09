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


class SearchConfigCellsMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_can_search_xlsx_cells_without_losing_raw_configuration_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "source"
            output_path = temporary_path / "index"
            source_path.mkdir()
            workbook_path = source_path / "玩法配置.xlsx"
            self._write_xlsx(workbook_path)

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
                    name_result = await client.call_tool(
                        "search_config_cells", {"query": "幸运转盘"}
                    )
                    value_result = await client.call_tool(
                        "search_config_cells", {"query": "5"}
                    )
                    formula_result = await client.call_tool(
                        "search_config_cells", {"query": "B2*100"}
                    )
                    range_result = await client.call_tool(
                        "get_sheet_range",
                        {
                            "workbook": "玩法配置.xlsx",
                            "sheet": "玩法配置",
                            "range": "A2:B3",
                        },
                    )
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            name_response = json.loads(name_result.content[0].text)
            self.assertEqual(name_response["status"], "found")
            self.assertEqual(len(name_response["cells"]), 1)
            name_cell = name_response["cells"][0]
            self.assertEqual(name_cell["source_document"], os.fspath(workbook_path.resolve()))
            self.assertEqual(name_cell["workbook"], "玩法配置.xlsx")
            self.assertEqual(name_cell["sheet_name"], "玩法配置")
            self.assertEqual(name_cell["cell_reference"], "A2")
            self.assertEqual(name_cell["raw_value"], "1")
            self.assertEqual(name_cell["display_text"], "幸运转盘")
            self.assertEqual(name_cell["data_type"], "s")

            value_response = json.loads(value_result.content[0].text)
            self.assertEqual(value_response["status"], "found")
            self.assertEqual(len(value_response["cells"]), 1)
            value_cell = value_response["cells"][0]
            self.assertEqual(value_cell["cell_reference"], "B2")
            self.assertEqual(value_cell["raw_value"], "5")
            self.assertIsNone(value_cell["formula"])
            self.assertEqual(value_cell["style_id"], 3)
            self.assertEqual(value_cell["merged_range"], "B2:C2")

            formula_response = json.loads(formula_result.content[0].text)
            self.assertEqual(formula_response["status"], "found")
            self.assertEqual(len(formula_response["cells"]), 1)
            formula_cell = formula_response["cells"][0]
            self.assertEqual(formula_cell["cell_reference"], "B3")
            self.assertEqual(formula_cell["raw_value"], "800")
            self.assertEqual(formula_cell["formula"], "B2*100")

            range_response = json.loads(range_result.content[0].text)
            self.assertEqual(range_response["status"], "found")
            self.assertEqual(range_response["workbook"], "玩法配置.xlsx")
            self.assertEqual(range_response["sheet"], "玩法配置")
            self.assertEqual(range_response["range"], "A2:B3")
            self.assertEqual(
                [cell["cell_reference"] for cell in range_response["cells"]],
                ["A2", "B2", "A3", "B3"],
            )
            self.assertEqual(range_response["cells"][-1]["formula"], "B2*100")

    async def test_same_workbook_name_in_two_paths_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "source"
            output_path = temporary_path / "index"
            (source_path / "项目A").mkdir(parents=True)
            (source_path / "项目B").mkdir(parents=True)
            self._write_xlsx(source_path / "项目A" / "玩法配置.xlsx")
            self._write_xlsx(source_path / "项目B" / "玩法配置.xlsx")
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
                    result = await client.call_tool(
                        "get_sheet_range",
                        {
                            "workbook": "玩法配置.xlsx",
                            "sheet": "玩法配置",
                            "range": "A1:B3",
                        },
                    )
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            response = json.loads(result.content[0].text)
            self.assertEqual(response["status"], "ambiguous")
            self.assertEqual(len(response["candidates"]), 2)
            self.assertEqual(response["cells"], [])

    @staticmethod
    def _write_xlsx(path: Path) -> None:
        workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="玩法配置" sheetId="1" state="visible" r:id="rId1"/></sheets>
</workbook>
"""
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
        shared_strings = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">
  <si><t>玩法名称</t></si><si><t>幸运转盘</t></si>
</sst>
"""
        sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:C3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="inlineStr"><is><t>每日次数</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>1</v></c>
      <c r="B2" s="3"><v>5</v></c>
    </row>
    <row r="3">
      <c r="A3" t="inlineStr"><is><t>总奖励</t></is></c>
      <c r="B3"><f>B2*100</f><v>800</v></c>
    </row>
  </sheetData>
  <mergeCells count="1"><mergeCell ref="B2:C2"/></mergeCells>
</worksheet>
"""
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            archive.writestr("xl/sharedStrings.xml", shared_strings)
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


if __name__ == "__main__":
    unittest.main()
