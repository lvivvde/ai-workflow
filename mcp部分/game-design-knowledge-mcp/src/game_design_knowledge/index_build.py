"""Publish one derived index build through the immutable snapshot protocol.

This is the only place that turns a source tree into a published index, so the
pipeline stage, the import tools, the CLI, and the benchmark all publish the
same way: build into a snapshot, validate it, then swap the active files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .index_revisions import (
    parse_revisions_by_document,
    record_index_build,
    record_processing_run,
)
from .indexer import DEFAULT_PROCESSING_MANIFEST, SCHEMA_VERSION, index_documents
from .revisions import ProcessingManifest
from .snapshots import IndexSnapshotStore


def build_index_atomically(
    source: Path,
    output: Path,
    *,
    processing_manifest: ProcessingManifest | None = None,
    ocr_engine: str | None = None,
    extra_excluded_directories: Iterable[str] | None = None,
    processing_run: Any = None,
) -> dict[str, int]:
    """Build into a snapshot, then publish it with a rename and a pointer write.

    ``processing_run`` is the pipeline's recorder. When it is passed, the run's
    stage attempts and processing manifest are written into the snapshot before
    it is validated, so the published index and its provenance ship together.
    """

    output = output.resolve()
    if output == Path(output.anchor):
        raise ValueError("Index output must not be a filesystem root")
    if output.exists() and not output.is_dir():
        raise ValueError(f"Index output exists and is not a directory: {output}")

    store = IndexSnapshotStore(output, expected_schema_version=SCHEMA_VERSION)
    with store.new_build(note=f"index {source.name}") as build:
        manifest = processing_manifest or DEFAULT_PROCESSING_MANIFEST
        report = index_documents(
            source,
            build.directory,
            processing_manifest=manifest,
            ocr_engine=ocr_engine,
            extra_excluded_directories=extra_excluded_directories,
        )
        if processing_run is not None:
            processing_run.record_projection(report)
            record_processing_run(
                build.database_path,
                build_id=build.build_id,
                payload=processing_run.index_payload(),
            )
        build.report = dict(report)
        build.record_parse_revisions(
            parse_revisions_by_document(build.database_path)
        )
        record_index_build(
            build.database_path,
            build_id=build.build_id,
            processing_manifest=manifest.as_payload(),
            parse_revisions=build.parse_revisions,
            state="validated",
            note=build.note,
            started_at=build.started_at,
        )
        build.validate()
        build.publish()
        return report


__all__ = ["build_index_atomically"]
