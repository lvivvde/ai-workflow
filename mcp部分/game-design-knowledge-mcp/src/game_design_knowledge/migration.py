"""Explicit schema migration for the derived knowledge index.

A derived index can always be deleted and rebuilt, so an old schema is never
worth guessing about. This module makes the two honest answers available:
migrate with a backup and a verification step, or refuse with a named version.

Silent misreads are the failure mode being designed out. A database whose
``user_version`` is newer than this build is reported, never opened for
interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .snapshots import verify_snapshot


TARGET_SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSIONS = frozenset({1})

DOCUMENT_REVISION_COLUMNS: dict[str, str] = {
    "logical_document_id": "TEXT",
    "source_revision_id": "TEXT",
    "parse_revision_id": "TEXT",
}

REVISION_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT NOT NULL,
        backup_path TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS index_builds (
        build_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        note TEXT NOT NULL DEFAULT '',
        database_sha256 TEXT NOT NULL DEFAULT '',
        processing_fingerprint TEXT NOT NULL DEFAULT '',
        processing_manifest TEXT NOT NULL DEFAULT '{}',
        parse_revisions TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_revisions (
        source_revision_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parse_revisions (
        parse_revision_id TEXT PRIMARY KEY,
        source_revision_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        database_schema_version INTEGER NOT NULL,
        processing_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 0
    )
    """,
)


class SchemaVersionError(RuntimeError):
    """Raised when a database's schema version cannot be read safely."""


class MigrationError(RuntimeError):
    """Raised when a migration did not finish and had to be rolled back."""


@dataclass(frozen=True)
class MigrationStep:
    """One explicit version-to-version upgrade of the derived index."""

    version_from: int
    version_to: int
    description: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "from": self.version_from,
            "to": self.version_to,
            "description": self.description,
        }


STEPS: dict[int, MigrationStep] = {
    2: MigrationStep(
        version_from=2,
        version_to=TARGET_SCHEMA_VERSION,
        description=(
            "Add the revision chain tables (schema_migrations, index_builds, "
            "source_revisions, parse_revisions) and the logical document "
            "columns on documents"
        ),
    )
}


def read_schema_version(database_path: Path) -> int:
    path = Path(database_path).resolve()
    if not path.is_file():
        raise SchemaVersionError(f"Index database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError as error:
        raise SchemaVersionError(
            f"Index database {path} is unreadable: {error}"
        ) from error
    finally:
        connection.close()


def refusal_message(version: int) -> str:
    """The one place that explains what to do about an unusable schema."""

    if version in STEPS:
        return (
            f"Index schema version {version} requires an explicit migration to "
            f"version {TARGET_SCHEMA_VERSION}; run `game-design-knowledge migrate` "
            "(it backs the database up first), or delete the derived index and "
            "rebuild it"
        )
    if version in LEGACY_SCHEMA_VERSIONS or version < TARGET_SCHEMA_VERSION:
        return (
            f"Index schema version {version} is unsupported and cannot be read or "
            "migrated automatically; delete the derived index and rebuild it from "
            "the project documents"
        )
    return (
        f"Index schema version {version} is newer than this build supports "
        f"({TARGET_SCHEMA_VERSION}); upgrade the game-design-knowledge MCP server "
        "instead of writing to this index"
    )


def ensure_revision_schema(connection: sqlite3.Connection) -> list[str]:
    """Create the revision-chain tables and columns if they are not there yet."""

    applied: list[str] = []
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    for statement in REVISION_TABLE_STATEMENTS:
        name = _created_name(statement)
        connection.execute(statement)
        if name not in existing:
            applied.append(f"table {name}")

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(documents)")
    }
    for column, column_type in DOCUMENT_REVISION_COLUMNS.items():
        if column in columns:
            continue
        connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {column_type}")
        applied.append(f"column documents.{column}")
    return applied


def plan_migration(database_path: Path) -> dict[str, Any]:
    path = Path(database_path)
    if not path.is_file():
        return {
            "status": "missing_database",
            "database_path": str(path),
            "current_version": None,
            "target_version": TARGET_SCHEMA_VERSION,
            "steps": [],
            "backup_required": False,
            "message": f"Index database does not exist: {path}",
        }

    current = read_schema_version(path)
    if current == TARGET_SCHEMA_VERSION:
        return {
            "status": "up_to_date",
            "database_path": str(path),
            "current_version": current,
            "target_version": TARGET_SCHEMA_VERSION,
            "steps": [],
            "backup_required": False,
            "message": f"Index schema version {current} is current",
        }

    steps: list[MigrationStep] = []
    version = current
    while version in STEPS:
        step = STEPS[version]
        steps.append(step)
        version = step.version_to
    supported = version == TARGET_SCHEMA_VERSION and bool(steps)
    return {
        "status": "migration_available" if supported else "unsupported",
        "database_path": str(path),
        "current_version": current,
        "target_version": TARGET_SCHEMA_VERSION,
        "steps": [step.as_payload() for step in steps],
        "backup_required": supported,
        "message": (
            f"Index schema version {current} can be migrated to "
            f"{TARGET_SCHEMA_VERSION} in {len(steps)} explicit step(s)"
            if supported
            else refusal_message(current)
        ),
    }


def apply_migration(
    database_path: Path,
    *,
    backup_directory: Path | None = None,
    apply: Callable[[int, sqlite3.Connection], list[str]] | None = None,
) -> dict[str, Any]:
    """Migrate an old index after taking a backup that a rollback can restore."""

    path = Path(database_path)
    plan = plan_migration(path)
    if plan["status"] == "missing_database":
        raise SchemaVersionError(str(plan["message"]))
    if plan["status"] == "up_to_date":
        return {
            "status": "up_to_date",
            "database_path": str(path),
            "from_version": plan["current_version"],
            "to_version": TARGET_SCHEMA_VERSION,
            "backup_path": None,
            "checks": [],
            "plan": plan,
        }
    if plan["status"] != "migration_available":
        raise SchemaVersionError(str(plan["message"]))

    backup_path = _backup_database(path, backup_directory)
    from_version = int(plan["current_version"])
    applied: list[dict[str, Any]] = []
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            version = from_version
            while version in STEPS:
                step = STEPS[version]
                changes = (
                    apply(step.version_to, connection)
                    if apply is not None
                    else _run_step(step, connection)
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO schema_migrations(
                        version, applied_at, description, backup_path
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        step.version_to,
                        _now(),
                        step.description,
                        str(backup_path),
                    ),
                )
                connection.execute(f"PRAGMA user_version = {step.version_to}")
                applied.append(
                    {"from": step.version_from, "to": step.version_to, "changes": changes}
                )
                version = step.version_to
    except Exception as error:
        connection.close()
        restored = rollback_migration(path, backup_path)
        raise MigrationError(
            f"Migration of {path} failed ({type(error).__name__}: {error}); the "
            f"pre-migration backup was restored (schema version "
            f"{restored['schema_version']})"
        ) from error
    connection.close()

    verification = verify_snapshot(path.parent, expected_schema_version=TARGET_SCHEMA_VERSION)
    if not verification["ok"]:
        restored = rollback_migration(path, backup_path)
        raise MigrationError(
            "Migration produced an index that failed verification "
            f"({_failed_checks(verification)}); the pre-migration backup was "
            f"restored (schema version {restored['schema_version']})"
        )
    return {
        "status": "migrated",
        "database_path": str(path),
        "from_version": from_version,
        "to_version": TARGET_SCHEMA_VERSION,
        "backup_path": str(backup_path),
        "applied": applied,
        "checks": verification["checks"],
        "plan": plan,
    }


def rollback_migration(database_path: Path, backup_path: Path) -> dict[str, Any]:
    path = Path(database_path)
    backup = Path(backup_path)
    if not backup.is_file():
        raise MigrationError(f"Pre-migration backup is missing: {backup}")
    shutil.copy2(backup, path)
    return {
        "status": "rolled_back",
        "database_path": str(path),
        "backup_path": str(backup),
        "schema_version": read_schema_version(path),
    }


def _run_step(step: MigrationStep, connection: sqlite3.Connection) -> list[str]:
    if step.version_to == TARGET_SCHEMA_VERSION:
        return ensure_revision_schema(connection)
    raise MigrationError(f"No migration is defined onto version {step.version_to}")


def _backup_database(database_path: Path, backup_directory: Path | None) -> Path:
    directory = (
        Path(backup_directory)
        if backup_directory is not None
        else database_path.parent / "schema-backups"
    )
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    destination = directory / f"{database_path.stem}-v{read_schema_version(database_path)}-{stamp}.sqlite"
    shutil.copy2(database_path, destination)
    return destination


def _failed_checks(verification: dict[str, Any]) -> str:
    return "; ".join(
        f"{check['check']}: {check['detail']}"
        for check in verification.get("checks") or ()
        if not check.get("passed")
    )


def _created_name(statement: str) -> str:
    words = statement.split()
    return words[words.index("EXISTS") + 1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "DOCUMENT_REVISION_COLUMNS",
    "LEGACY_SCHEMA_VERSIONS",
    "MigrationError",
    "MigrationStep",
    "REVISION_TABLE_STATEMENTS",
    "STEPS",
    "SchemaVersionError",
    "TARGET_SCHEMA_VERSION",
    "apply_migration",
    "ensure_revision_schema",
    "plan_migration",
    "read_schema_version",
    "refusal_message",
    "rollback_migration",
]
