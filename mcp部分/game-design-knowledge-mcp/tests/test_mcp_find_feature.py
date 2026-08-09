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


class FindFeatureMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_canonical_names_and_manually_confirmed_aliases_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "project"
            catalog_path = source_path / "knowledge" / "catalog.json"
            output_path = temporary_path / "index"
            catalog_path.parent.mkdir(parents=True)
            catalog = {
                "version": 1,
                "features": [
                    {
                        "key": "lucky-wheel",
                        "canonical_name": "幸运转盘",
                        "source": "策划玩法目录",
                        "aliases": [
                            {
                                "name": "大转盘",
                                "source": "策划确认",
                                "confirmed_at": "2026-08-09",
                                "confirmed_by": "测试策划",
                            }
                        ],
                    }
                ],
            }
            original_catalog = json.dumps(catalog, ensure_ascii=False, indent=2)
            catalog_path.write_text(original_catalog, encoding="utf-8")
            self._write_docx(source_path / "knowledge" / "幸运转盘.docx")

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
                    canonical_result = await client.call_tool(
                        "find_feature", {"name": "幸运转盘"}
                    )
                    alias_result = await client.call_tool(
                        "find_feature", {"name": "大转盘"}
                    )
                    missing_result = await client.call_tool(
                        "find_feature", {"name": "风车玩法"}
                    )
                    evidence_result = await client.call_tool(
                        "get_feature_evidence", {"name": "大转盘"}
                    )
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            canonical_response = json.loads(canonical_result.content[0].text)
            self.assertEqual(canonical_response["status"], "found")
            self.assertEqual(canonical_response["match_type"], "canonical")
            self.assertEqual(canonical_response["feature"]["key"], "lucky-wheel")
            self.assertEqual(
                canonical_response["feature"]["canonical_name"], "幸运转盘"
            )

            alias_response = json.loads(alias_result.content[0].text)
            self.assertEqual(alias_response["status"], "found")
            self.assertEqual(alias_response["match_type"], "confirmed_alias")
            self.assertEqual(alias_response["matched_alias"]["name"], "大转盘")
            self.assertEqual(
                alias_response["matched_alias"]["confirmed_by"], "测试策划"
            )
            self.assertEqual(
                alias_response["feature"]["canonical_name"], "幸运转盘"
            )

            missing_response = json.loads(missing_result.content[0].text)
            self.assertEqual(missing_response["status"], "not_found")
            self.assertIsNone(missing_response["match_type"])
            self.assertIsNone(missing_response["feature"])
            self.assertNotIn("suggestions", missing_response)
            self.assertEqual(
                missing_response["limitations"],
                ["当前人工确认目录中未找到该正式名称或别名，未进行自动联想。"],
            )
            self.assertEqual(catalog_path.read_text(encoding="utf-8"), original_catalog)

            evidence_response = json.loads(evidence_result.content[0].text)
            self.assertEqual(evidence_response["status"], "found")
            self.assertEqual(evidence_response["query"], "大转盘")
            self.assertEqual(evidence_response["resolved_name"], "幸运转盘")
            self.assertEqual(
                evidence_response["feature"]["canonical_name"], "幸运转盘"
            )
            self.assertEqual(len(evidence_response["documents"]), 1)
            self.assertEqual(
                evidence_response["documents"][0]["text"],
                "大转盘每日开放。",
            )
            self.assertEqual(evidence_response["configs"], [])
            self.assertEqual(evidence_response["images"], [])

    async def test_catalog_changes_are_stale_and_invalid_updates_do_not_replace_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_path = temporary_path / "project"
            catalog_path = source_path / "knowledge" / "catalog.json"
            output_path = temporary_path / "index"
            catalog_path.parent.mkdir(parents=True)
            valid_catalog = {
                "version": 1,
                "features": [
                    {
                        "key": "lucky-wheel",
                        "canonical_name": "幸运转盘",
                        "source": "策划确认",
                        "aliases": [
                            {
                                "name": "大转盘",
                                "source": "策划确认",
                                "confirmed_at": "2026-08-09",
                                "confirmed_by": "测试策划",
                            }
                        ],
                    }
                ],
            }
            catalog_path.write_text(
                json.dumps(valid_catalog, ensure_ascii=False), encoding="utf-8"
            )
            self._run_index(source_path, output_path, check=True)

            invalid_catalog = valid_catalog.copy()
            invalid_catalog["features"] = [dict(valid_catalog["features"][0])]
            invalid_catalog["features"][0]["aliases"] = ["未经确认的字符串外号"]
            catalog_path.write_text(
                json.dumps(invalid_catalog, ensure_ascii=False), encoding="utf-8"
            )

            previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
            os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(output_path)
            try:
                async with Client(mcp) as client:
                    stale_status = await client.call_tool("index_status", {})
                failed = self._run_index(source_path, output_path, check=False)
                async with Client(mcp) as client:
                    preserved = await client.call_tool(
                        "find_feature", {"name": "大转盘"}
                    )
            finally:
                if previous_index is None:
                    os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
                else:
                    os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

            self.assertEqual(stale_status.structured_content["catalog_is_stale"], True)
            self.assertEqual(stale_status.structured_content["is_stale"], True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("confirmation objects", failed.stderr)
            self.assertEqual(preserved.structured_content["status"], "found")
            self.assertEqual(
                preserved.structured_content["feature"]["canonical_name"], "幸运转盘"
            )

    @staticmethod
    def _run_index(source: Path, output: Path, check: bool) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                os.fspath(Path(__file__).parents[1] / ".venv" / "Scripts" / "game-design-knowledge.exe"),
                "index",
                os.fspath(source),
                "--output",
                os.fspath(output),
            ],
            check=check,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _write_docx(path: Path) -> None:
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>大转盘每日开放。</w:t></w:r></w:p></w:body>
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
