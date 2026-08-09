from __future__ import annotations

import asyncio
import base64
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


class GetXlsxImageContextMcpTests(unittest.TestCase):
    def test_ai_can_get_sheet_cell_and_text_for_an_xlsx_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            document_path = source_dir / "战斗数值.xlsx"
            self._write_xlsx(document_path)
            self._index(source_dir, output_dir)

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = str(output_dir)
            try:
                result = asyncio.run(self._get_context())
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            context = result.structured_content
            self.assertEqual(context["source_document"], str(document_path.resolve()))
            self.assertEqual(context["document_type"], "xlsx")
            self.assertEqual(context["sheet_name"], "数值配置")
            self.assertEqual(context["cell_anchor"], "C5")
            self.assertEqual(context["context_text"], "伤害曲线示意图")

    @staticmethod
    async def _get_context():
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            return await client.call_tool("get_image_context", {"image_id": 1})

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

    @staticmethod
    def _write_xlsx(path: Path) -> None:
        workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="数值配置" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
        sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetData><row r="5"><c r="C5" t="inlineStr"><is><t>伤害曲线示意图</t></is></c></row></sheetData>
  <drawing r:id="rId2"/>
</worksheet>
"""
        sheet_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>
</Relationships>
"""
        drawing_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <xdr:oneCellAnchor>
    <xdr:from><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>4</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:pic><xdr:blipFill><a:blip r:embed="rId3"/></xdr:blipFill></xdr:pic>
    <xdr:clientData/>
  </xdr:oneCellAnchor>
</xdr:wsDr>
"""
        drawing_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/mockup.png"/>
</Relationships>
"""
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
            archive.writestr("xl/drawings/drawing1.xml", drawing_xml)
            archive.writestr("xl/drawings/_rels/drawing1.xml.rels", drawing_rels)
            archive.writestr("xl/media/mockup.png", PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
