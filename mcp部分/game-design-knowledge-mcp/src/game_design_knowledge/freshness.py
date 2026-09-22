"""Freshness for each layer, reported separately instead of as one verdict.

Source bytes, durable state, parse revisions, the lexical index, the semantic
index, and the explanation cache go stale for different reasons and have
different remedies. Collapsing them into one ``is_stale`` flag hid which layer
was behind; this module answers per layer, with the version it expected, the
version it found, when the layer last succeeded, and what to do next.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .indexer import (
    DEFAULT_PROCESSING_MANIFEST,
    SCHEMA_VERSION,
    SOURCE_PATTERNS,
    excluded_directories,
    source_documents,
)
from .index_revisions import parse_revisions_by_document as indexed_revisions
from .revisions import (
    ProcessingManifest,
    document_id,
    file_sha256,
    normalize_relative_path,
    parse_revision_id,
    source_revision_id,
)
from .snapshots import IndexSnapshotStore, verify_snapshot
from .state import DurableState, StateError, StateLocked, StateVersionError


FRESHNESS_STATES = (
    "fresh",
    "building",
    "stale",
    "missing",
    "failed",
    "incompatible",
)
# Worst first: the report's severity is the first entry present.
SEVERITY = ("incompatible", "failed", "missing", "stale", "building", "fresh")

CORE_DIMENSIONS = ("source", "durable_state", "parse", "lexical_index")
OPTIONAL_DIMENSIONS = ("semantic_index", "explanation_cache")
DIMENSIONS = CORE_DIMENSIONS + OPTIONAL_DIMENSIONS

SUGGESTED_ACTIONS = (
    "none",
    "rebuild_index",
    "migrate_index",
    "review_documents",
    "wait_for_build",
    "not_configured",
)


def freshness_report(
    project_root: Path,
    index_directory: Path,
    *,
    state_directory: Path | None = None,
    processing_manifest: ProcessingManifest | None = None,
) -> dict[str, Any]:
    """Report how far each layer is behind the project documents."""

    project_root = Path(project_root).resolve()
    index_directory = Path(index_directory).resolve()
    manifest = processing_manifest or DEFAULT_PROCESSING_MANIFEST
    state = DurableState(project_root, state_directory)
    store = IndexSnapshotStore(
        index_directory, expected_schema_version=SCHEMA_VERSION
    )

    source_files, source_errors = _source_fingerprints(project_root)
    expected = _expected_revisions(source_files, manifest)
    state_facts = _state_facts(state)
    pointer = store.read_pointer()[0]
    current = store.current_snapshot()

    dimensions = {
        "source": _source_dimension(source_files, source_errors, state),
        "durable_state": _durable_state_dimension(state, state_facts),
        "parse": _parse_dimension(expected, state, state_facts),
        "lexical_index": _lexical_dimension(store, pointer, current, expected),
        "semantic_index": _optional_dimension(
            "The semantic index is not built by this server yet; lexical search "
            "is the only retrieval projection in this version.",
            "V2-07",
        ),
        "explanation_cache": _optional_dimension(
            "No explanation cache is written by this server yet; explanations "
            "are produced per request.",
            "V2-09",
        ),
    }

    blocking = [
        name
        for name in CORE_DIMENSIONS
        if dimensions[name]["state"] != "fresh"
    ]
    return {
        "status": "fresh" if not blocking else "stale",
        "is_fresh": not blocking,
        "worst_state": _worst_state(dimensions),
        "blocking_dimensions": blocking,
        "dimensions": dimensions,
        "checked_at": _now(),
        "project_root": str(project_root),
        "index_directory": str(index_directory),
        "state_directory": str(state.directory),
        "documents_found": len(source_files),
        "expected_schema_version": SCHEMA_VERSION,
    }


def _source_fingerprints(
    project_root: Path,
) -> tuple[dict[str, str], list[str]]:
    excluded = excluded_directories(project_root, None)
    fingerprints: dict[str, str] = {}
    errors: list[str] = []
    for pattern in SOURCE_PATTERNS:
        for path in source_documents(project_root, pattern, excluded):
            relative = normalize_relative_path(
                str(path.relative_to(project_root))
            )
            try:
                fingerprints[relative] = file_sha256(path)
            except OSError as error:
                errors.append(f"{relative}: {error}")
    return fingerprints, errors


def _expected_revisions(
    source_files: Mapping[str, str], manifest: ProcessingManifest
) -> dict[str, dict[str, str]]:
    fingerprint = manifest.fingerprint
    expected: dict[str, dict[str, str]] = {}
    for relative, digest in source_files.items():
        logical = document_id(relative)
        revision = source_revision_id(logical, digest)
        expected[logical] = {
            "document_id": logical,
            "path": relative,
            "source_revision_id": revision,
            "parse_revision_id": parse_revision_id(
                revision,
                database_schema_version=SCHEMA_VERSION,
                processing_fingerprint_value=fingerprint,
            ),
        }
    return expected


def _state_facts(state: DurableState) -> dict[str, Any]:
    try:
        if not state.exists():
            return {"status": "missing", "state": None, "manifest": None}
        return {
            "status": "readable",
            "state": state,
            "manifest": state.load(),
        }
    except StateVersionError as error:
        return {"status": "incompatible", "error": str(error), "manifest": None}
    except StateLocked as error:
        return {"status": "building", "error": str(error), "manifest": None}
    except StateError as error:
        return {"status": "failed", "error": str(error), "manifest": None}


def _source_dimension(
    source_files: Mapping[str, str],
    errors: list[str],
    state: DurableState,
) -> dict[str, Any]:
    if errors:
        return _dimension(
            "failed",
            f"{len(errors)} project document(s) could not be read: {errors[:3]}",
            suggested_action="review_documents",
            expected_version=f"{len(source_files)} document(s)",
        )
    if not source_files:
        return _dimension(
            "missing",
            "No DOCX/XLSX documents were found under the project root.",
            suggested_action="review_documents",
        )
    if not state.exists():
        return _dimension(
            "missing",
            f"{len(source_files)} project document(s) are not registered in "
            "durable state yet.",
            suggested_action="rebuild_index",
            expected_version=f"{len(source_files)} document(s)",
            active_version="0 document(s)",
        )

    recorded = {
        record.document_id: record.content_sha256
        for record in _latest_source_revisions(state)
    }
    unregistered = []
    changed = []
    for relative, digest in sorted(source_files.items()):
        logical = document_id(relative)
        if logical not in recorded:
            unregistered.append(relative)
        elif recorded[logical] != digest:
            changed.append(relative)
    if not unregistered and not changed:
        return _dimension(
            "fresh",
            f"All {len(source_files)} project document(s) match their archived "
            "source revisions.",
            expected_version=f"{len(source_files)} document(s)",
            active_version=f"{len(recorded)} document(s)",
        )
    return _dimension(
        "stale",
        f"{len(changed)} document(s) changed content and {len(unregistered)} "
        f"are not archived yet: {sorted(changed + unregistered)[:5]}",
        suggested_action="rebuild_index",
        expected_version=f"{len(source_files)} document(s)",
        active_version=f"{len(recorded)} document(s)",
    )


def _durable_state_dimension(
    state: DurableState, facts: Mapping[str, Any]
) -> dict[str, Any]:
    status = facts["status"]
    if status == "readable":
        manifest = facts["manifest"]
        return _dimension(
            "fresh",
            f"Durable state is readable at {state.directory}.",
            expected_version=f"state schema {manifest.state_schema_version}",
            active_version=f"state schema {manifest.state_schema_version}",
            last_success_at=manifest.updated_at or None,
        )
    if status == "incompatible":
        return _dimension(
            "incompatible",
            str(facts.get("error")),
            suggested_action="none",
        )
    if status == "building":
        return _dimension(
            "building",
            str(facts.get("error")),
            suggested_action="wait_for_build",
        )
    if status == "failed":
        return _dimension(
            "failed",
            str(facts.get("error")),
            suggested_action="review_documents",
        )
    return _dimension(
        "missing",
        f"Durable state is not initialized at {state.directory}.",
        suggested_action="rebuild_index",
    )


def _parse_dimension(
    expected: Mapping[str, Mapping[str, str]],
    state: DurableState,
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    if facts["status"] != "readable":
        return _dimension(
            "missing",
            "Parse revisions cannot be read because durable state is unavailable.",
            suggested_action="rebuild_index",
        )
    active = state.active_parse_revision()
    assert isinstance(active, dict)
    expected_count = len(expected)
    if not expected_count:
        return _dimension(
            "missing",
            "No project documents are available to parse.",
            suggested_action="review_documents",
        )
    behind = sorted(
        entry["path"]
        for logical, entry in expected.items()
        if logical not in active
        or active[logical].parse_revision_id != entry["parse_revision_id"]
    )
    last_success = max(
        (record.created_at for record in active.values()), default=None
    )
    if not behind:
        return _dimension(
            "fresh",
            f"Every project document has an active parse revision "
            f"({len(active)} in total).",
            expected_version=f"{expected_count} parse revision(s)",
            active_version=f"{len(active)} parse revision(s)",
            last_success_at=last_success,
        )
    return _dimension(
        "stale",
        f"{len(behind)} document(s) have no current parse revision: {behind[:5]}",
        suggested_action="rebuild_index",
        expected_version=f"{expected_count} parse revision(s)",
        active_version=f"{len(active) - len(behind)} parse revision(s)",
        last_success_at=last_success,
    )


def _lexical_dimension(
    store: IndexSnapshotStore,
    pointer: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
    expected: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    if not store.database_path.is_file():
        return _dimension(
            "missing",
            f"There is no published index at {store.database_path}.",
            suggested_action="rebuild_index",
            expected_version=f"schema {SCHEMA_VERSION}",
        )

    verification = verify_snapshot(
        store.index_directory, expected_schema_version=SCHEMA_VERSION
    )
    if verification["status"] == "incompatible":
        return _dimension(
            "incompatible",
            f"The published index has schema version "
            f"{verification['schema_version']}; this build needs {SCHEMA_VERSION}.",
            suggested_action="migrate_index",
            expected_version=f"schema {SCHEMA_VERSION}",
            active_version=f"schema {verification['schema_version']}",
        )
    if not verification["ok"]:
        return _dimension(
            "failed",
            f"The published index does not verify: {verification['status']}.",
            suggested_action="rebuild_index",
            expected_version=f"schema {SCHEMA_VERSION}",
            active_version=f"schema {verification['schema_version']}",
        )
    published = indexed_revisions(store.database_path)
    untracked = sorted(
        entry["path"] for logical, entry in expected.items() if logical not in published
    )
    if untracked:
        return _dimension(
            "stale",
            f"{len(untracked)} indexed document(s) carry no revision identity: "
            f"{untracked[:5]}",
            suggested_action="rebuild_index",
            expected_version=f"{len(expected)} parse revision(s)",
            active_version=f"{len(published)} parse revision(s)",
        )
    behind = sorted(
        entry["path"]
        for logical, entry in expected.items()
        if published[logical] != entry["parse_revision_id"]
    )
    if not behind:
        pointer_note = (
            ""
            if pointer is not None and current is not None
            else (
                "; there is no local publication pointer, so the build identity "
                "of this index is unknown"
            )
        )
        published_at = current.get("published_at") if current else None
        if pointer is not None and current is not None:
            disagreement = _pointer_disagreement(current, published)
            if disagreement:
                return _dimension(
                    "stale",
                    "The publication pointer and the index disagree about "
                    f"{disagreement}; the published files are not a single build.",
                    suggested_action="rebuild_index",
                    expected_version=f"{len(expected)} parse revision(s)",
                    active_version=f"{len(published)} parse revision(s)",
                    last_success_at=published_at,
                )
        return _dimension(
            "fresh",
            f"The published index covers all {len(published)} parse revision(s) "
            f"of the current project documents{pointer_note}.",
            expected_version=f"{len(expected)} parse revision(s)",
            active_version=f"{len(published)} parse revision(s)",
            last_success_at=published_at,
        )
    return _dimension(
        "stale",
        f"{len(behind)} document(s) changed since the index was published: "
        f"{behind[:5]}",
        suggested_action="rebuild_index",
        expected_version=f"{len(expected)} parse revision(s)",
        active_version=f"{len(published)} parse revision(s)",
        last_success_at=current.get("published_at") if current else None,
    )


def _pointer_disagreement(
    current: Mapping[str, Any], published: Mapping[str, str]
) -> str:
    """Name the first document whose pointer and index rows disagree.

    Both are written from one build, so any difference means the published
    files no longer describe a single build.
    """

    recorded = current.get("parse_revisions") or {}
    missing = sorted(set(published) - set(recorded))
    if missing:
        return missing[0]
    extra = sorted(set(recorded) - set(published))
    if extra:
        return extra[0]
    for logical, revision in sorted(published.items()):
        if recorded[logical] != revision:
            return logical
    return ""


def _optional_dimension(detail: str, owner: str) -> dict[str, Any]:
    return _dimension(
        "missing",
        detail,
        suggested_action="not_configured",
        expected_version="not built by this version",
        extra={"optional": True, "owner_ticket": owner},
    )


def _dimension(
    state: str,
    detail: str,
    *,
    suggested_action: str = "none",
    expected_version: str | None = None,
    active_version: str | None = None,
    last_success_at: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in FRESHNESS_STATES:
        raise ValueError(f"Unknown freshness state: {state}")
    if suggested_action not in SUGGESTED_ACTIONS:
        raise ValueError(f"Unknown suggested action: {suggested_action}")
    payload: dict[str, Any] = {
        "state": state,
        "detail": detail,
        "expected_version": expected_version,
        "active_version": active_version,
        "last_success_at": last_success_at,
        "suggested_action": suggested_action,
    }
    if extra:
        payload.update(extra)
    return payload


def _latest_source_revisions(state: DurableState) -> list[Any]:
    latest: dict[str, Any] = {}
    for record in state.source_revisions():
        latest[record.document_id] = record
    return list(latest.values())


def _worst_state(dimensions: Mapping[str, Mapping[str, Any]]) -> str:
    # A layer this version does not build yet is reported, but it cannot make
    # the project look worse than the layers that actually exist.
    present = {
        dimension["state"]
        for dimension in dimensions.values()
        if not dimension.get("optional")
    }
    for state in SEVERITY:
        if state in present:
            return state
    return "fresh"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CORE_DIMENSIONS",
    "DIMENSIONS",
    "FRESHNESS_STATES",
    "OPTIONAL_DIMENSIONS",
    "SEVERITY",
    "SUGGESTED_ACTIONS",
    "freshness_report",
]
