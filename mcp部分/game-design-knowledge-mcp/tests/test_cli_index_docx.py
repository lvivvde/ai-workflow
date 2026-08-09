from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class IndexDocxFromCliTests(unittest.TestCase):
    def test_user_can_index_a_docx_with_an_embedded_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            self._write_docx(source_dir / "战斗系统.docx")

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
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["documents_indexed"], 1)
            self.assertEqual(report["images_indexed"], 1)
            self.assertTrue((output_dir / "knowledge.sqlite").is_file())
            exported_images = list((output_dir / "assets").glob("*.png"))
            self.assertEqual(len(exported_images), 1)
            self.assertEqual(exported_images[0].read_bytes(), PNG_BYTES)

    def test_index_reports_when_ocr_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            source_dir = workspace / "documents"
            output_dir = workspace / "index"
            source_dir.mkdir()
            self._write_docx(source_dir / "战斗系统.docx")

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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["ocr_succeeded"], 0)
            self.assertEqual(report["ocr_failed"], 0)
            self.assertEqual(report["ocr_unavailable"], 1)

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
  <Relationship Id="rId5"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/mockup.png"/>
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
