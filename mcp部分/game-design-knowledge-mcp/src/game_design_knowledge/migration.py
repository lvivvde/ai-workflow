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


TARGET_SCHEMA_VERSION = 5
LEGACY_SCHEMA_VERSIONS = frozenset({1})
REVISION_SCHEMA_VERSION = 3
PROCESSING_SCHEMA_VERSION = 4

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

PROCESSING_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS processing_manifests (
        build_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        configured_fingerprint TEXT NOT NULL,
        configured_manifest TEXT NOT NULL DEFAULT '{}',
        run_manifest TEXT NOT NULL DEFAULT '{}',
        profile TEXT NOT NULL DEFAULT '',
        capability TEXT NOT NULL DEFAULT '{}',
        limits TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_attempts (
        attempt_id TEXT PRIMARY KEY,
        build_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        stage TEXT NOT NULL,
        attempt_number INTEGER NOT NULL DEFAULT 1,
        document_path TEXT NOT NULL DEFAULT '',
        execution_status TEXT NOT NULL,
        quality_status TEXT NOT NULL,
        reason_code TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        fingerprint TEXT NOT NULL DEFAULT '',
        input_sha256 TEXT NOT NULL DEFAULT '',
        output_sha256 TEXT NOT NULL DEFAULT '',
        cache_hit INTEGER NOT NULL DEFAULT 0,
        fallback_used INTEGER NOT NULL DEFAULT 0,
        engine TEXT NOT NULL DEFAULT '',
        engine_version TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        model_version TEXT NOT NULL DEFAULT '',
        owner_ticket TEXT NOT NULL DEFAULT '',
        coverage TEXT NOT NULL DEFAULT '{}',
        reason_chain TEXT NOT NULL DEFAULT '[]',
        started_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL DEFAULT 0
    )
    """,
)


class SchemaVersionError(RuntimeError):
    """Raised when a database's schema version cannot be read safely."""


# The OCR run, its regions, and the normalization suggestions proposed for each
# region. Raw transcription lives in ``ocr_regions.text_raw`` and is never
# rewritten; a suggestion is a separate row that points back at its region.
OCR_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ocr_runs (
        id INTEGER PRIMARY KEY,
        image_id INTEGER NOT NULL,
        requested_engine TEXT NOT NULL DEFAULT '',
        engine TEXT NOT NULL DEFAULT '',
        engine_version TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT '',
        fallback_used INTEGER NOT NULL DEFAULT 0,
        execution_status TEXT NOT NULL,
        quality_status TEXT NOT NULL,
        reason_code TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        evidence_state TEXT NOT NULL DEFAULT 'transcription',
        language TEXT NOT NULL DEFAULT '',
        reading_order_source TEXT NOT NULL DEFAULT '',
        region_count INTEGER NOT NULL DEFAULT 0,
        reason_chain TEXT NOT NULL DEFAULT '[]',
        duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ocr_regions (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        image_id INTEGER NOT NULL,
        region_index INTEGER NOT NULL,
        reading_order INTEGER NOT NULL DEFAULT 0,
        bbox TEXT,
        text_raw TEXT NOT NULL,
        text_confidence REAL,
        region_confidence REAL,
        key_mark_confidence REAL,
        language TEXT NOT NULL DEFAULT '',
        UNIQUE(run_id, region_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ocr_normalizations (
        id INTEGER PRIMARY KEY,
        region_id INTEGER NOT NULL,
        ruleset_version TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        changes TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        UNIQUE(region_id, ruleset_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ocr_runs_image_index ON ocr_runs(image_id)",
    "CREATE INDEX IF NOT EXISTS ocr_regions_image_index ON ocr_regions(image_id)",
)


def ensure_ocr_schema(connection: sqlite3.Connection) -> list[str]:
    """Create the OCR tables that carry region-level transcription."""

    return _ensure_tables(connection, OCR_TABLE_STATEMENTS)


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
        version_to=REVISION_SCHEMA_VERSION,
        description=(
            "Add the revision chain tables (schema_migrations, index_builds, "
            "source_revisions, parse_revisions) and the logical document "
            "columns on documents"
        ),
    ),
    3: MigrationStep(
        version_from=3,
        version_to=PROCESSING_SCHEMA_VERSION,
        description=(
            "Add the processing tables (processing_manifests, stage_attempts) "
            "that record which stages, engines, and fallbacks produced a build"
        ),
    ),
    4: MigrationStep(
        version_from=PROCESSING_SCHEMA_VERSION,
        version_to=TARGET_SCHEMA_VERSION,
        description=(
            "Add the OCR tables (ocr_runs, ocr_regions, ocr_normalizations) that "
            "keep region geometry, raw transcription, separated confidences, and "
            "the suggestion proposed for each region"
        ),
    ),
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

    applied = _ensure_tables(connection, REVISION_TABLE_STATEMENTS)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(documents)")
    }
    for column, column_type in DOCUMENT_REVISION_COLUMNS.items():
        if column in columns:
            continue
        connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {column_type}")
        applied.append(f"column documents.{column}")
    return applied


def ensure_processing_schema(connection: sqlite3.Connection) -> list[str]:
    """Create the processing tables that carry a run's stage history."""

    return _ensure_tables(connection, PROCESSING_TABLE_STATEMENTS)


def _ensure_tables(
    connection: sqlite3.Connection, statements: tuple[str, ...]
) -> list[str]:
    applied: list[str] = []
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    for statement in statements:
        name = _created_name(statement)
        connection.execute(statement)
        if name not in existing:
            applied.append(f"table {name}")
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
    if step.version_to == REVISION_SCHEMA_VERSION:
        return ensure_revision_schema(connection)
    if step.version_to == PROCESSING_SCHEMA_VERSION:
        return ensure_processing_schema(connection)
    if step.version_to == TARGET_SCHEMA_VERSION:
        return ensure_ocr_schema(connection)
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
    "PROCESSING_TABLE_STATEMENTS",
    "REVISION_SCHEMA_VERSION",
    "REVISION_TABLE_STATEMENTS",
    "STEPS",
    "SchemaVersionError",
    "TARGET_SCHEMA_VERSION",
    "apply_migration",
    "ensure_ocr_schema",
    "ensure_processing_schema",
    "ensure_revision_schema",
    "OCR_TABLE_STATEMENTS",
    "plan_migration",
    "PROCESSING_SCHEMA_VERSION",
    "read_schema_version",
    "refusal_message",
    "rollback_migration",
]
