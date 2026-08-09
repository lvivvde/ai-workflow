from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
import zipfile


class IncrementalIndexCliTests(unittest.TestCase):
    def test_unchanged_changed_and_removed_documents_are_synchronized_by_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "source"
            output = workspace / "index"
            source.mkdir()
            document = source / "玩法.docx"
            self._write_docx(document, "每日开放5次")

            first = self._index(source, output)
            self.assertEqual(first["documents_added"], 1)
            self.assertEqual(first["documents_reused"], 0)

            second = self._index(source, output)
            self.assertEqual(second["documents_added"], 0)
            self.assertEqual(second["documents_updated"], 0)
            self.assertEqual(second["documents_reused"], 1)
            self.assertEqual(second["documents_removed"], 0)

            self._write_docx(document, "每日开放8次")
            third = self._index(source, output)
            self.assertEqual(third["documents_added"], 0)
            self.assertEqual(third["documents_updated"], 1)
            self.assertEqual(third["documents_reused"], 0)

            document.unlink()
            fourth = self._index(source, output)
            self.assertEqual(fourth["documents_removed"], 1)
            self.assertEqual(fourth["documents_indexed"], 0)

    def test_incompatible_schema_requires_an_explicit_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "source"
            output = workspace / "index"
            source.mkdir()
            self._write_docx(source / "玩法.docx", "每日开放5次")
            self._index(source, output)
            database_path = output / "knowledge.sqlite"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute("PRAGMA user_version = 1")

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

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("schema version 1", completed.stderr)
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_shared_image_asset_is_removed_only_after_its_last_document_is_deleted(self) -> None:
        from test_cli_index_docx import IndexDocxFromCliTests

        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "source"
            output = workspace / "index"
            source.mkdir()
            first_document = source / "玩法A.docx"
            second_document = source / "玩法B.docx"
            IndexDocxFromCliTests._write_docx(first_document)
            IndexDocxFromCliTests._write_docx(second_document)

            first = self._index(source, output)
            self.assertEqual(first["assets_removed"], 0)
            self.assertEqual(len(list((output / "assets").glob("*.png"))), 1)

            first_document.unlink()
            second = self._index(source, output)
            self.assertEqual(second["assets_removed"], 0)
            self.assertEqual(len(list((output / "assets").glob("*.png"))), 1)

            second_document.unlink()
            third = self._index(source, output)
            self.assertEqual(third["assets_removed"], 1)
            self.assertEqual(list((output / "assets").glob("*.png")), [])

    @staticmethod
    def _index(source: Path, output: Path) -> dict[str, int]:
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
        return json.loads(completed.stdout)

    @staticmethod
    def _write_docx(path: Path, text: str) -> None:
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
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
