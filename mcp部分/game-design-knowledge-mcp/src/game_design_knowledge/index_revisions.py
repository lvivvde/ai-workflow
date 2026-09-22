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


def record_processing_run(
    database_path: Path, *, build_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Project one pipeline run and its stage attempts into the index.

    Attempts are append-only: a retry inserts new rows and never rewrites the
    rows a previous attempt left behind.
    """

    run_manifest = payload.get("run_manifest") or {}
    connection = sqlite3.connect(Path(database_path))
    try:
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT OR REPLACE INTO processing_manifests(
                    build_id, run_id, created_at, configured_fingerprint,
                    configured_manifest, run_manifest, profile, capability, limits
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    build_id,
                    str(payload.get("run_id") or ""),
                    _now(),
                    str(payload.get("configured_fingerprint") or ""),
                    json.dumps(
                        payload.get("configured_manifest") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(run_manifest, ensure_ascii=False, sort_keys=True),
                    str(payload.get("profile") or ""),
                    json.dumps(
                        payload.get("capability") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        payload.get("limits") or {}, ensure_ascii=False, sort_keys=True
                    ),
                ),
            )
            rows = 0
            for attempt in payload.get("attempts") or ():
                connection.execute(
                    """
                    INSERT OR REPLACE INTO stage_attempts(
                        attempt_id, build_id, run_id, stage, attempt_number,
                        document_path, execution_status, quality_status, reason_code,
                        detail, fingerprint, input_sha256, output_sha256, cache_hit,
                        fallback_used, engine, engine_version, model, model_version,
                        owner_ticket, coverage, reason_chain, started_at, finished_at,
                        duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(attempt.get("attempt_id") or ""),
                        build_id,
                        str(attempt.get("run_id") or ""),
                        str(attempt.get("stage") or ""),
                        int(attempt.get("attempt_number") or 1),
                        str(attempt.get("document_path") or ""),
                        str(attempt.get("execution_status") or ""),
                        str(attempt.get("quality_status") or ""),
                        str(attempt.get("reason_code") or ""),
                        str(attempt.get("detail") or ""),
                        str(attempt.get("fingerprint") or ""),
                        str(attempt.get("input_sha256") or ""),
                        str(attempt.get("output_sha256") or ""),
                        int(bool(attempt.get("cache_hit"))),
                        int(bool(attempt.get("fallback_used"))),
                        str(attempt.get("engine") or ""),
                        str(attempt.get("engine_version") or ""),
                        str(attempt.get("model") or ""),
                        str(attempt.get("model_version") or ""),
                        str(attempt.get("owner_ticket") or ""),
                        json.dumps(
                            attempt.get("coverage") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            attempt.get("reason_chain") or [],
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        str(attempt.get("started_at") or ""),
                        str(attempt.get("finished_at") or ""),
                        int(attempt.get("duration_ms") or 0),
                    ),
                )
                rows += 1
    finally:
        connection.close()
    return {"status": "recorded", "build_id": build_id, "attempts": rows}


def processing_manifest(database_path: Path) -> dict[str, Any] | None:
    """The newest run's processing manifest, as the index recorded it."""

    connection = _read_only(database_path)
    try:
        row = connection.execute(
            """
            SELECT build_id, run_id, created_at, configured_fingerprint,
                   configured_manifest, run_manifest, profile, capability, limits
            FROM processing_manifests
            ORDER BY created_at DESC, build_id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "build_id": str(row["build_id"]),
        "run_id": str(row["run_id"]),
        "created_at": str(row["created_at"]),
        "configured_fingerprint": str(row["configured_fingerprint"]),
        "configured_manifest": _json_value(row["configured_manifest"], {}),
        "run_manifest": _json_value(row["run_manifest"], {}),
        "profile": str(row["profile"]),
        "capability": _json_value(row["capability"], {}),
        "limits": _json_value(row["limits"], {}),
    }


def stage_attempts(
    database_path: Path,
    *,
    stage: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Recorded attempts, newest first, optionally for a single stage."""

    connection = _read_only(database_path)
    try:
        if stage is None:
            rows = connection.execute(
                """
                SELECT * FROM stage_attempts
                ORDER BY started_at DESC, attempt_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM stage_attempts WHERE stage = ?
                ORDER BY started_at DESC, attempt_id DESC
                LIMIT ?
                """,
                (stage, limit),
            ).fetchall()
    finally:
        connection.close()
    return [_attempt_row(row) for row in rows]


def stage_attempt_summary(database_path: Path) -> dict[str, Any]:
    """Execution and quality counts per stage, for ``index_status``."""

    connection = _read_only(database_path)
    try:
        rows = connection.execute(
            """
            SELECT stage, execution_status, quality_status, COUNT(*) AS total
            FROM stage_attempts GROUP BY stage, execution_status, quality_status
            """
        ).fetchall()
    finally:
        connection.close()
    stages: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = stages.setdefault(
            str(row["stage"]),
            {
                "attempts": 0,
                "execution_status": {},
                "quality_status": {},
            },
        )
        total = int(row["total"])
        entry["attempts"] += total
        entry["execution_status"][str(row["execution_status"])] = (
            entry["execution_status"].get(str(row["execution_status"]), 0) + total
        )
        entry["quality_status"][str(row["quality_status"])] = (
            entry["quality_status"].get(str(row["quality_status"]), 0) + total
        )
    return {
        "attempts": sum(entry["attempts"] for entry in stages.values()),
        "stages": dict(sorted(stages.items())),
    }


def _attempt_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "attempt_id": str(row["attempt_id"]),
        "build_id": str(row["build_id"]),
        "run_id": str(row["run_id"]),
        "stage": str(row["stage"]),
        "attempt_number": int(row["attempt_number"]),
        "document_path": str(row["document_path"]),
        "execution_status": str(row["execution_status"]),
        "quality_status": str(row["quality_status"]),
        "reason_code": str(row["reason_code"]),
        "detail": str(row["detail"]),
        "fingerprint": str(row["fingerprint"]),
        "input_sha256": str(row["input_sha256"]),
        "output_sha256": str(row["output_sha256"]),
        "cache_hit": bool(row["cache_hit"]),
        "fallback_used": bool(row["fallback_used"]),
        "engine": str(row["engine"]),
        "engine_version": str(row["engine_version"]),
        "model": str(row["model"]),
        "model_version": str(row["model_version"]),
        "owner_ticket": str(row["owner_ticket"]),
        "coverage": _json_value(row["coverage"], {}),
        "reason_chain": _json_value(row["reason_chain"], []),
        "started_at": str(row["started_at"]),
        "finished_at": str(row["finished_at"]),
        "duration_ms": int(row["duration_ms"]),
    }


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
    "processing_manifest",
    "record_index_build",
    "record_processing_run",
    "retrieval_units",
    "stage_attempt_summary",
    "stage_attempts",
]
