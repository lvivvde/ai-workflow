from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


class AtomicIndexFromCliTests(unittest.TestCase):
    def test_failed_build_does_not_publish_a_partial_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "published-index"
            source_dir.mkdir()
            self._write_incomplete_xlsx(source_dir / "损坏配置.xlsx")

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

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(output_dir.exists())
            self.assertEqual(list(workspace.glob(".published-index.staging-*")), [])

    def test_suspiciously_compressed_ooxml_is_rejected_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "published-index"
            source_dir.mkdir()
            with zipfile.ZipFile(
                source_dir / "可疑文档.docx", "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("word/document.xml", b"A" * (2 * 1024 * 1024))

            project_root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root / "src")
            completed = subprocess.run(
                [sys.executable, "-m", "game_design_knowledge.cli", "index", str(source_dir), "--output", str(output_dir)],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("suspicious compression ratio", completed.stderr)
            self.assertFalse(output_dir.exists())

    @staticmethod
    def _write_incomplete_xlsx(path: Path) -> None:
        workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="缺失工作表" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/missing.xml"/>
</Relationships>
"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)


if __name__ == "__main__":
    unittest.main()
