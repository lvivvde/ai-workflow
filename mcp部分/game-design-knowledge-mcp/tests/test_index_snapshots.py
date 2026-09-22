from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from game_design_knowledge.cli import build_index_atomically
from game_design_knowledge.indexer import SCHEMA_VERSION, index_documents
from game_design_knowledge.snapshots import (
    CURRENT_NAME,
    IndexSnapshotStore,
    SnapshotError,
    SnapshotStateError,
)

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


class IndexSnapshotTests(unittest.TestCase):
    def test_failed_build_keeps_the_previous_snapshot_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")

            build_index_atomically(project_root, index_directory)
            store = IndexSnapshotStore(
                index_directory, expected_schema_version=SCHEMA_VERSION
            )
            published = store.current_snapshot()
            self.assertIsNotNone(published)
            good_bytes = store.database_path.read_bytes()

            # A build that dies before publishing must not move the pointer.
            with self.assertRaises(RuntimeError):
                with store.new_build("interrupted") as interrupted:
                    write_docx(
                        interrupted.directory / "docs" / "另一个.docx", "每日开放9次"
                    )
                    raise RuntimeError("process killed mid-build")

            self.assertEqual(store.current_snapshot()["build_id"], published["build_id"])
            self.assertEqual(store.database_path.read_bytes(), good_bytes)
            self.assertEqual(store.build_record(interrupted.build_id)["state"], "failed")

    def test_unvalidated_snapshot_cannot_become_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            build_index_atomically(project_root, index_directory)
            store = IndexSnapshotStore(
                index_directory, expected_schema_version=SCHEMA_VERSION
            )

            with store.new_build("unvalidated") as build:
                build.database_path.unlink()
                with self.assertRaises(SnapshotStateError):
                    build.publish()
                self.assertEqual(build.state, "running")

            with store.new_build("invalid") as build:
                connection = sqlite3.connect(build.database_path)
                with connection:
                    connection.execute("DROP TABLE evidence_fts")
                connection.close()
                with self.assertRaises(SnapshotStateError):
                    build.validate()
                self.assertEqual(build.state, "failed")

            self.assertIsNotNone(store.current_snapshot())

    def test_locked_database_is_reported_without_half_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            build_index_atomically(project_root, index_directory)
            store = IndexSnapshotStore(
                index_directory, expected_schema_version=SCHEMA_VERSION
            )
            published = store.current_snapshot()

            with store.new_build("second") as build:
                write_docx(project_root / "docs" / "玩法.docx", "每日开放8次")
                index_documents(project_root, build.directory)
                build.validate()
                with open(store.database_path, "rb") as held_open:
                    try:
                        build.publish()
                    except SnapshotError as error:
                        self.assertIn("holds", str(error))
                    else:
                        self.skipTest(
                            "This platform allows replacing an open database"
                        )

            self.assertEqual(store.current_snapshot()["build_id"], published["build_id"])
            connection = sqlite3.connect(
                f"{store.database_path.as_uri()}?mode=ro", uri=True
            )
            try:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
            finally:
                connection.close()

    def test_a_corrupt_pointer_is_recovered_from_the_last_valid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            build_index_atomically(project_root, index_directory)
            write_docx(project_root / "docs" / "玩法.docx", "每日开放8次")
            build_index_atomically(project_root, index_directory)

            store = IndexSnapshotStore(
                index_directory, expected_schema_version=SCHEMA_VERSION
            )
            newest = store.current_snapshot()["build_id"]
            (index_directory / CURRENT_NAME).write_text("{ truncated", encoding="utf-8")

            plan = store.recovery_plan()
            self.assertEqual(plan["status"], "recovery_available")
            self.assertEqual(plan["candidate"]["build_id"], newest)
            self.assertIn("unreadable", plan["error"])

            recovered = store.recover()
            self.assertEqual(recovered["status"], "recovered")
            self.assertEqual(store.current_snapshot()["build_id"], newest)

    def test_pruning_keeps_the_active_and_last_known_good_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root = workspace / "project"
            index_directory = project_root / ".index" / "knowledge"
            write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
            for revision in range(3):
                write_docx(project_root / "docs" / "玩法.docx", f"每日开放{revision}次")
                build_index_atomically(project_root, index_directory)

            store = IndexSnapshotStore(
                index_directory,
                expected_schema_version=SCHEMA_VERSION,
                snapshot_limit=0,
            )
            active = store.current_snapshot()["build_id"]
            previous = store.last_known_good()["build_id"]
            store.prune()

            surviving = {
                store.snapshot_build_id(path) for path in store._snapshot_directories()
            }
            self.assertIn(active, surviving)
            self.assertIn(previous, surviving)

if __name__ == "__main__":
    unittest.main()
