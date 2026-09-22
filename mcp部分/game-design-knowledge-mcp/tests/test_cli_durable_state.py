"""`cli index` publishes the index and then registers durable state for it."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from game_design_knowledge.state import DurableState, state_directory_for

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def run_index(
    source: Path, output: Path, *extra_arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "game_design_knowledge.cli",
            "index",
            str(source),
            "--output",
            str(output),
            *extra_arguments,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


class CliDurableStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory(prefix="gdk-cli-state-")
        self.project_root = Path(self._workspace.name) / "project"
        self.index_directory = self.project_root / ".index" / "knowledge"
        self.document = self.project_root / "docs" / "玩法.docx"
        write_docx(self.document, "每日开放5次")

    def tearDown(self) -> None:
        self._workspace.cleanup()

    def test_index_infers_the_project_root_and_registers_durable_state(self) -> None:
        completed = run_index(self.project_root, self.index_directory)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["documents_indexed"], 1)
        self.assertEqual(report["documents_reused"], 0)

        state = DurableState(self.project_root, state_directory_for(self.project_root))
        self.assertTrue(state.exists())
        documents = state.documents()
        self.assertEqual(
            [record.relative_path for record in documents.values()], ["docs/玩法.docx"]
        )
        for record in documents.values():
            self.assertIsNotNone(record.active_parse_revision)
        bundles = state.bundle_ids()
        self.assertEqual(len(bundles), 1)
        verification = state.verify_bundle(bundles[0])
        self.assertTrue(verification["ok"], verification["checks"])

    def test_explicit_project_root_records_state_outside_the_index_layout(self) -> None:
        outside = Path(self._workspace.name) / "elsewhere"
        completed = run_index(
            self.project_root, outside, "--project-root", str(self.project_root)
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(state_directory_for(self.project_root).is_dir())

    def test_an_index_without_an_owning_project_stays_derived_only(self) -> None:
        outside = Path(self._workspace.name) / "elsewhere"
        completed = run_index(self.project_root, outside)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(state_directory_for(self.project_root).exists())

    def test_a_source_outside_the_project_root_is_reported_not_recorded(self) -> None:
        elsewhere_project = Path(self._workspace.name) / "other-project"
        write_docx(elsewhere_project / "docs" / "其他.docx", "另一份文件")
        completed = run_index(
            elsewhere_project,
            self.index_directory,
            "--project-root",
            str(self.project_root),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("not registered", completed.stderr)
        self.assertFalse(state_directory_for(self.project_root).exists())


if __name__ == "__main__":
    unittest.main()
