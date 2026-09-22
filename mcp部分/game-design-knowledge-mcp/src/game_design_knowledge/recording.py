"""Register the durable-state records that a published index claims to have.

The derived index is a projection: it can always be rebuilt. This module turns
one published build back into durable facts -- archived source bytes, parse
revisions, and verifiable revision bundles -- so that deleting and rebuilding
the index never loses document identity or review history.
"""

from __future__ import annotations

from pathlib import Path

from .index_revisions import document_revisions, latest_build, retrieval_units
from .indexer import SCHEMA_VERSION
from .revisions import ProcessingManifest, document_id
from .state import DurableState, StateError, state_directory_for


def record_build_revisions(
    project_root: Path,
    index_directory: Path,
    *,
    state_directory: Path | None = None,
) -> dict[str, object]:
    """Archive sources, record parse revisions, and publish bundles.

    Callers run this after the index is published, so every problem here is
    reported rather than raised: a missing or newer durable state must not turn
    a successful rebuild into a failed tool call.
    """

    project_root = Path(project_root).resolve()
    index_directory = Path(index_directory).resolve()
    database_path = index_directory / "knowledge.sqlite"
    state = DurableState(
        project_root,
        state_directory if state_directory is not None else state_directory_for(project_root),
    )
    try:
        state.ensure()
        build = latest_build(database_path)
        manifest = ProcessingManifest.from_payload(
            (build or {}).get("processing_manifest") or {}
        )
        archived: list[dict[str, str]] = []
        recorded: list[dict[str, str]] = []
        bundles: list[dict[str, str]] = []
        skipped: list[str] = []
        rows = document_revisions(database_path)
        for row in rows:
            relative, source_revision, parse_revision = _revision_paths(
                row, index_directory, project_root
            )
            if relative is None or source_revision is None or parse_revision is None:
                skipped.append(str(row["path"]))
                continue
            source_path = (project_root / relative).resolve()
            if state.source_revision(source_revision) is None:
                if not source_path.is_file():
                    skipped.append(f"{relative}: source file is gone")
                    continue
                state.archive_source(relative, source_path.read_bytes(), note="rebuild")
                archived.append(
                    {"path": relative, "source_revision_id": source_revision}
                )
            if state.parse_revision(parse_revision) is None:
                state.record_parse_revision(
                    source_revision,
                    database_schema_version=SCHEMA_VERSION,
                    processing_manifest=manifest,
                )
                recorded.append(
                    {"path": relative, "parse_revision_id": parse_revision}
                )
            bundle = state.write_bundle(
                parse_revision,
                {
                    "document_type": row["document_type"],
                    "source_revision_id": source_revision,
                    "source_sha256": row["source_sha256"],
                    "retrieval_units": retrieval_units(
                        database_path, str(row["logical_document_id"])
                    ),
                },
                note="rebuild",
            )
            bundles.append(
                {"path": relative, "bundle_id": str(bundle["bundle_id"])}
            )
        return {
            "status": "recorded",
            "state_directory": str(state.directory),
            "documents": len(rows),
            "archived_sources": archived,
            "recorded_parse_revisions": recorded,
            "bundles": bundles,
            "skipped": skipped,
            "processing_fingerprint": manifest.fingerprint,
        }
    except (StateError, OSError) as error:
        return {
            "status": "not_recorded",
            "state_directory": str(state.directory),
            "error": f"{type(error).__name__}: {error}",
            "archived_sources": [],
            "recorded_parse_revisions": [],
            "bundles": [],
            "skipped": [],
        }


def _revision_paths(
    row: dict[str, object], index_directory: Path, project_root: Path
) -> tuple[str | None, str | None, str | None]:
    """Turn one index row back into project-relative revision identities."""

    logical = row.get("logical_document_id")
    source_revision = row.get("source_revision_id")
    parse_revision = row.get("parse_revision_id")
    if not logical or not source_revision or not parse_revision:
        return None, None, None
    source_path = Path(str(row["path"]))
    if not source_path.is_absolute():
        source_path = index_directory / source_path
    try:
        relative = source_path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return None, None, None
    if document_id(relative) != str(logical):
        return None, None, None
    return relative, str(source_revision), str(parse_revision)


__all__ = ["record_build_revisions"]
