"""Freeze the V1 MCP surface so V2 work cannot silently break old clients.

These checks talk to the live server rather than to a hand-written copy of it:
the frozen contract only means something while the running tool surface, the
response keys, and the locator shapes still match it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from mcp import Client

from game_design_knowledge.cli import build_index_atomically
from game_design_knowledge.server import mcp
from game_design_knowledge.v1_contract import (
    CONFIG_CELL_FIELDS,
    DOCUMENT_EVIDENCE_FIELDS,
    DOCX_LOCATOR_FIELDS,
    INDEX_STATUS_FIELDS,
    PREVIEW_STATES,
    SHEET_RANGE_CELL_FIELDS,
    V1_TOOL_CONTRACTS,
    XLSX_LOCATOR_FIELDS,
    contract_violations,
    missing_response_keys,
    tool_surface,
)


class V1ContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._workspace = tempfile.TemporaryDirectory(prefix="gdk-v1-contract-")
        root = Path(cls._workspace.name)
        cls.source_directory = root / "documents"
        cls.index_directory = root / "index"
        cls.source_directory.mkdir()
        _write_docx(
            cls.source_directory / "活动系统.docx",
            [
                ("Heading1", "活动系统"),
                ("Heading2", "幸运转盘"),
                (None, "玩家每日可参与5次。"),
            ],
        )
        _write_xlsx(cls.source_directory / "数值表.xlsx")
        build_index_atomically(cls.source_directory, cls.index_directory)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._workspace.cleanup()

    def setUp(self) -> None:
        self._saved_environment = {
            name: os.environ.get(name)
            for name in ("GAME_DESIGN_INDEX_DIR", "GAME_DESIGN_PROJECT_ROOT")
        }
        os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(self.index_directory)

    def tearDown(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    async def test_live_tool_surface_still_satisfies_the_frozen_contract(self) -> None:
        surface = await self._surface()

        self.assertEqual(contract_violations(surface), [])

    async def test_contract_violations_name_every_kind_of_break(self) -> None:
        surface = {
            name: dict(payload) for name, payload in (await self._surface()).items()
        }

        renamed = dict(surface)
        entry = dict(renamed["search_evidence"])
        entry["required_parameters"] = ("question",)
        renamed["search_evidence"] = entry
        renamed_violations = contract_violations(renamed)
        self.assertTrue(
            any("search_evidence" in item for item in renamed_violations),
            renamed_violations,
        )

        removed = dict(surface)
        removed.pop("get_sheet_range")
        self.assertTrue(
            any("get_sheet_range" in item for item in contract_violations(removed))
        )

        redefaulted = dict(surface)
        entry = dict(redefaulted["search_evidence"])
        entry["default_parameters"] = {**entry["default_parameters"], "limit": 5}
        redefaulted["search_evidence"] = entry
        self.assertTrue(
            any("limit" in item for item in contract_violations(redefaulted))
        )

    async def test_read_tools_keep_the_frozen_keys_and_a_fresh_index_status(
        self,
    ) -> None:
        search = await self._call("search_evidence", {"query": "每日可参与5次"})
        evidence_id = search["evidence"][0]["evidence_id"]
        arguments = {
            "index_status": {},
            "search_images": {"query": "每日次数"},
            "search_evidence": {"query": "每日可参与5次"},
            "get_evidence": {"evidence_id": evidence_id},
            "search_config_cells": {"query": "每日次数"},
            "get_sheet_range": {
                "workbook": "数值表.xlsx",
                "sheet": "玩法配置",
                "range": "A1:B3",
            },
            "find_feature": {"name": "幸运转盘"},
            "get_feature_evidence": {"name": "幸运转盘"},
            "get_image_context": {"image_id": 1},
        }
        read_contracts = {
            name: contract
            for name, contract in V1_TOOL_CONTRACTS.items()
            if contract.kind == "read"
        }
        self.assertEqual(set(arguments), set(read_contracts))

        for tool, parameters in arguments.items():
            with self.subTest(tool=tool):
                contract = read_contracts[tool]
                response = await self._call(tool, parameters)

                self.assertEqual(missing_response_keys(contract, response), [])

                status = response if tool == "index_status" else response["index_status"]
                if tool != "index_status":
                    self.assertIn(response["status"], contract.allowed_status)
                self.assertLessEqual(INDEX_STATUS_FIELDS, set(status))
                self.assertIn(status["is_stale"], (True, False))
                self.assertIsInstance(status["schema_version"], int)
                self.assertIsInstance(status["documents_indexed"], int)
                self.assertGreaterEqual(status["documents_indexed"], 2)

    async def test_claims_keep_the_frozen_evidence_and_locator_fields(self) -> None:
        search = await self._call("search_evidence", {"query": "每日可参与5次"})
        self.assertEqual(search["status"], "found")
        evidence = search["evidence"][0]
        self.assertLessEqual(DOCUMENT_EVIDENCE_FIELDS, set(evidence))
        self.assertLessEqual(set(evidence["locator"]), DOCX_LOCATOR_FIELDS)

        cells = await self._call("search_config_cells", {"query": "每日次数"})
        self.assertEqual(cells["status"], "found")
        self.assertLessEqual(CONFIG_CELL_FIELDS, set(cells["cells"][0]))

        xlsx_evidence = await self._call(
            "search_evidence", {"query": "每日次数", "document_type": "xlsx"}
        )
        self.assertEqual(xlsx_evidence["status"], "found")
        self.assertLessEqual(
            set(xlsx_evidence["evidence"][0]["locator"]), XLSX_LOCATOR_FIELDS
        )

        sheet = await self._call(
            "get_sheet_range",
            {"workbook": "数值表.xlsx", "sheet": "玩法配置", "range": "A1:B3"},
        )
        self.assertEqual(sheet["status"], "found")
        cell = sheet["cells"][0]
        self.assertLessEqual(SHEET_RANGE_CELL_FIELDS, set(cell))
        self.assertIn("cell_reference", cell)
        self.assertEqual(sheet["workbook"], "数值表.xlsx")
        self.assertEqual(sheet["sheet"], "玩法配置")

    async def test_get_evidence_keeps_the_frozen_keys_when_nothing_matches(
        self,
    ) -> None:
        contract = V1_TOOL_CONTRACTS["get_evidence"]

        found = await self._call(
            "search_evidence", {"query": "每日可参与5次"}
        )
        hit = await self._call(
            "get_evidence",
            {"evidence_id": found["evidence"][0]["evidence_id"]},
        )
        missing = await self._call("get_evidence", {"evidence_id": 10**9})

        self.assertEqual(hit["status"], "found")
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(missing_response_keys(contract, hit), [])
        self.assertEqual(missing_response_keys(contract, missing), [])

    async def test_write_tools_preview_before_applying(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gdk-v1-write-") as temporary:
            workspace = Path(temporary)
            project_root = workspace / "project"
            incoming = workspace / "incoming" / "新玩法.docx"
            project_root.mkdir()
            incoming.parent.mkdir()
            _write_docx(incoming, [(None, "新玩法每次消耗10点体力。")])
            os.environ["GAME_DESIGN_PROJECT_ROOT"] = os.fspath(project_root)
            os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(
                project_root / ".index" / "knowledge"
            )
            build_index_atomically(project_root, project_root / ".index" / "knowledge")

            plan_contract = V1_TOOL_CONTRACTS["plan_document_import"]
            plan = await self._call(
                "plan_document_import", {"source_paths": [os.fspath(incoming)]}
            )
            self.assertIn(plan["status"], PREVIEW_STATES)
            self.assertEqual(missing_response_keys(plan_contract, plan), [])

            import_contract = V1_TOOL_CONTRACTS["import_documents"]
            preview = await self._call(
                "import_documents",
                {
                    "source_paths": [os.fspath(incoming)],
                    "plan_token": plan["plan_token"],
                },
            )
            self.assertIn(preview["status"], PREVIEW_STATES)
            self.assertEqual(missing_response_keys(import_contract, preview), [])

            applied = await self._call(
                "import_documents",
                {
                    "source_paths": [os.fspath(incoming)],
                    "plan_token": plan["plan_token"],
                    "confirmed": True,
                },
            )
            self.assertEqual(applied["status"], "completed")
            self.assertEqual(
                missing_response_keys(import_contract, applied, applied=True), []
            )

            rebuild_contract = V1_TOOL_CONTRACTS["rebuild_shared_index"]
            rebuild_preview = await self._call(
                "rebuild_shared_index", {"confirmed": False}
            )
            self.assertIn(rebuild_preview["status"], PREVIEW_STATES)
            self.assertEqual(
                missing_response_keys(rebuild_contract, rebuild_preview), []
            )

            rebuild_applied = await self._call(
                "rebuild_shared_index", {"confirmed": True}
            )
            self.assertEqual(rebuild_applied["status"], "completed")
            self.assertEqual(
                missing_response_keys(rebuild_contract, rebuild_applied, applied=True),
                [],
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

    @staticmethod
    async def _surface() -> dict[str, dict[str, object]]:
        async with Client(mcp) as client:
            listing = await client.list_tools()
        return tool_surface(listing.tools)


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


def _write_xlsx(path: Path) -> None:
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="玩法配置" sheetId="1" state="visible" r:id="rId1"/></sheets>
</workbook>
"""
    workbook_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:B3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>玩法名称</t></is></c>
      <c r="B1" t="inlineStr"><is><t>每日次数</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>幸运转盘</t></is></c>
      <c r="B2" s="3"><v>5</v></c>
    </row>
    <row r="3">
      <c r="A3" t="inlineStr"><is><t>总奖励</t></is></c>
      <c r="B3"><f>B2*100</f><v>500</v></c>
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
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


if __name__ == "__main__":
    unittest.main()
