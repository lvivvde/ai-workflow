from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from game_design_knowledge.index_build import build_index_atomically
from game_design_knowledge.indexer import SCHEMA_VERSION, index_documents
from game_design_knowledge.migration import (
    STEPS,
    MigrationError,
    SchemaVersionError,
    apply_migration,
    plan_migration,
    read_schema_version,
    refusal_message,
    rollback_migration,
)

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


def build_project(workspace: Path) -> tuple[Path, Path]:
    project_root = workspace / "project"
    index_directory = project_root / ".index" / "knowledge"
    write_docx(project_root / "docs" / "玩法.docx", "每日开放5次")
    build_index_atomically(project_root, index_directory)
    return project_root, index_directory


def downgrade_to_v2(database_path: Path) -> None:
    """Turn a current index into a realistic Schema v2 one."""

    with closing(sqlite3.connect(database_path)) as connection, connection:
        for table in ("parse_revisions", "source_revisions", "index_builds", "schema_migrations"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for column in ("logical_document_id", "source_revision_id", "parse_revision_id"):
            connection.execute(f"ALTER TABLE documents DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 2")


def downgrade_to_v4(database_path: Path) -> None:
    """Turn a current index into the processing-only Schema v4 one."""

    with closing(sqlite3.connect(database_path)) as connection, connection:
        for table in ("ocr_normalizations", "ocr_regions", "ocr_runs"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("PRAGMA user_version = 4")


class SchemaMigrationTests(unittest.TestCase):
    def test_a_v4_index_gains_the_ocr_tables_without_losing_its_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root, index_directory = build_project(workspace)
            database_path = index_directory / "knowledge.sqlite"
            downgrade_to_v4(database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                before = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

            plan = plan_migration(database_path)
            self.assertEqual(plan["status"], "migration_available")
            self.assertEqual(plan["current_version"], 4)
            self.assertEqual(
                [(step["from"], step["to"]) for step in plan["steps"]], [(4, SCHEMA_VERSION)]
            )

            report = apply_migration(database_path)

            self.assertEqual(report["to_version"], SCHEMA_VERSION)
            with closing(sqlite3.connect(database_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                after = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            self.assertLessEqual(
                {"ocr_runs", "ocr_regions", "ocr_normalizations"}, tables
            )
            self.assertEqual(after, before, "migration only adds tables")
            self.assertEqual(read_schema_version(database_path), SCHEMA_VERSION)
            self.assertEqual(
                index_documents(project_root, index_directory)["documents_indexed"], 1
            )

    def test_a_v2_index_needs_an_explicit_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            project_root, index_directory = build_project(workspace)
            database_path = index_directory / "knowledge.sqlite"
            downgrade_to_v2(database_path)

            plan = plan_migration(database_path)
            self.assertEqual(plan["status"], "migration_available")
            self.assertEqual(plan["current_version"], 2)
            self.assertEqual(plan["target_version"], SCHEMA_VERSION)
            self.assertTrue(plan["backup_required"])

            with self.assertRaises(SchemaVersionError) as raised:
                index_documents(project_root, index_directory)
            self.assertIn("schema version 2", str(raised.exception))
            self.assertIn("migrate", str(raised.exception))
            self.assertEqual(read_schema_version(database_path), 2)

    def test_migration_adds_the_revision_chain_and_keeps_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            _, index_directory = build_project(workspace)
            database_path = index_directory / "knowledge.sqlite"
            downgrade_to_v2(database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                before = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

            report = apply_migration(database_path)

            self.assertEqual(report["status"], "migrated")
            self.assertEqual(report["from_version"], 2)
            self.assertEqual(report["to_version"], SCHEMA_VERSION)
            self.assertTrue(Path(report["backup_path"]).is_file())
            with closing(sqlite3.connect(database_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                after = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
                recorded = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            self.assertLessEqual(
                {
                    "schema_migrations",
                    "index_builds",
                    "source_revisions",
                    "parse_revisions",
                },
                tables,
            )
            self.assertEqual(after, before)
            self.assertEqual(
                [row[0] for row in recorded],
                [step.version_to for version, step in sorted(STEPS.items()) if version >= 2],
                "a v2 index walks every declared step and records each one",
            )
            self.assertEqual(read_schema_version(database_path), SCHEMA_VERSION)

            self.assertEqual(plan_migration(database_path)["status"], "up_to_date")
            self.assertEqual(
                index_documents(Path(temporary_directory) / "project", index_directory)[
                    "documents_indexed"
                ],
                1,
                "a migrated index is readable normally again",
            )

    def test_a_failed_migration_restores_the_pre_migration_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            _, index_directory = build_project(workspace)
            database_path = index_directory / "knowledge.sqlite"
            downgrade_to_v2(database_path)
            original = database_path.read_bytes()

            def explode(version: int, connection: sqlite3.Connection) -> list[str]:
                connection.execute("CREATE TABLE half_applied (id INTEGER)")
                raise RuntimeError("migration step failed")

            with self.assertRaises(MigrationError) as raised:
                apply_migration(database_path, apply=explode)

            self.assertIn("pre-migration backup was restored", str(raised.exception))
            self.assertEqual(database_path.read_bytes(), original)
            self.assertEqual(read_schema_version(database_path), 2)
            with closing(sqlite3.connect(database_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertNotIn("half_applied", tables)

    def test_rollback_restores_the_backup_and_its_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            _, index_directory = build_project(workspace)
            database_path = index_directory / "knowledge.sqlite"
            downgrade_to_v2(database_path)

            report = apply_migration(database_path)
            restored = rollback_migration(database_path, Path(report["backup_path"]))

            self.assertEqual(restored["schema_version"], 2)
            self.assertEqual(read_schema_version(database_path), 2)

    def test_a_legacy_or_newer_schema_is_refused_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            _, index_directory = build_project(workspace)
            database_path = index_directory / "knowledge.sqlite"

            for version in (1, 99):
                with closing(sqlite3.connect(database_path)) as connection, connection:
                    connection.execute(f"PRAGMA user_version = {version}")
                plan = plan_migration(database_path)
                self.assertEqual(plan["status"], "unsupported")
                self.assertIn(f"schema version {version}", plan["message"])
                with self.assertRaises(SchemaVersionError):
                    apply_migration(database_path)

            self.assertIn("newer than this build", refusal_message(99))
            self.assertIn("rebuild", refusal_message(1))


if __name__ == "__main__":
    unittest.main()
