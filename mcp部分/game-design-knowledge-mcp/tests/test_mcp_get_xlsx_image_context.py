from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import Client

from document_fixtures import write_xlsx_with_image


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
        write_xlsx_with_image(path, "伤害曲线示意图", PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
