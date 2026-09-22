"""The derived index's own view of the revision chain.

Durable state is authoritative. These helpers let a published index answer
"which source bytes, parse revision, and locator produced this row?" without
opening another store, which is what a bundle verification and a freshness
check need.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def document_revisions(database_path: Path) -> list[dict[str, Any]]:
    """Every indexed document with the revisions the build wrote for it."""

    connection = _read_only(database_path)
    try:
        rows = connection.execute(
            """
            SELECT path, document_type, source_sha256, logical_document_id,
                   source_revision_id, parse_revision_id
            FROM documents
            ORDER BY path
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "path": str(row["path"]),
            "document_type": str(row["document_type"]),
            "source_sha256": str(row["source_sha256"]),
            "logical_document_id": row["logical_document_id"],
            "source_revision_id": row["source_revision_id"],
            "parse_revision_id": row["parse_revision_id"],
        }
        for row in rows
    ]


def parse_revisions_by_document(database_path: Path) -> dict[str, str]:
    """Map logical document id to the parse revision stored in the index."""

    return {
        str(row["logical_document_id"] or row["path"]): str(row["parse_revision_id"])
        for row in document_revisions(database_path)
        if row["parse_revision_id"]
    }


def retrieval_units(database_path: Path, logical_document_id: str) -> list[dict[str, Any]]:
    """The located retrieval units a published bundle can be checked against."""

    connection = _read_only(database_path)
    try:
        rows = connection.execute(
            """
            SELECT e.id, e.evidence_type, e.authority, e.section_path, e.locator
            FROM evidence AS e
            JOIN documents AS d ON d.id = e.document_id
            WHERE d.logical_document_id = ?
            ORDER BY e.id
            """,
            (logical_document_id,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "unit_id": f"evidence:{row['id']}",
            "unit_type": str(row["evidence_type"]),
            "authority": str(row["authority"]),
            "section_path": _json_value(row["section_path"], []),
            "locator": _json_value(row["locator"], {}),
        }
        for row in rows
    ]


def record_index_build(
    database_path: Path,
    *,
    build_id: str,
    processing_manifest: Mapping[str, Any],
    parse_revisions: Mapping[str, str],
    state: str = "validated",
    note: str = "",
    started_at: str | None = None,
    finished_at: str | None = None,
    database_sha256: str = "",
) -> dict[str, Any]:
    """Write the build's own provenance into the index it is about to publish."""

    fingerprint = str(processing_manifest.get("fingerprint") or "")
    connection = sqlite3.connect(Path(database_path))
    try:
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT OR REPLACE INTO index_builds(
                    build_id, state, started_at, finished_at, note,
                    database_sha256, processing_fingerprint,
                    processing_manifest, parse_revisions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    build_id,
                    state,
                    started_at,
                    finished_at or _now(),
                    note,
                    database_sha256,
                    fingerprint,
                    json.dumps(dict(processing_manifest), ensure_ascii=False, sort_keys=True),
                    json.dumps(dict(parse_revisions), ensure_ascii=False, sort_keys=True),
                ),
            )
    finally:
        connection.close()
    return {
        "build_id": build_id,
        "state": state,
        "processing_fingerprint": fingerprint,
        "parse_revisions": dict(parse_revisions),
    }


def builds(database_path: Path, limit: int = 10) -> list[dict[str, Any]]:
    connection = _read_only(database_path)
    try:
        rows = connection.execute(
            """
            SELECT build_id, state, started_at, finished_at, note,
                   database_sha256, processing_fingerprint,
                   processing_manifest, parse_revisions
            FROM index_builds
            ORDER BY COALESCE(finished_at, started_at) DESC, build_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "build_id": str(row["build_id"]),
            "state": str(row["state"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "note": str(row["note"]),
            "database_sha256": str(row["database_sha256"]),
            "processing_fingerprint": str(row["processing_fingerprint"]),
            "processing_manifest": _json_value(row["processing_manifest"], {}),
            "parse_revisions": _json_value(row["parse_revisions"], {}),
        }
        for row in rows
    ]


def latest_build(database_path: Path) -> dict[str, Any] | None:
    found = builds(database_path, limit=1)
    return found[0] if found else None


def _read_only(database_path: Path) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "builds",
    "document_revisions",
    "latest_build",
    "parse_revisions_by_document",
    "record_index_build",
    "retrieval_units",
]
