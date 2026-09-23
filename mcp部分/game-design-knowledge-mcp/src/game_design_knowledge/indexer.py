from __future__ import annotations

from contextlib import closing
import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
from typing import Any, Iterable, Mapping
import xml.etree.ElementTree as ET
import zipfile

from . import revisions
from .flow_notation import build_relations, summarize_relations
from .ink import arrow_ink_readings
from .layout import build_reading_order
from .migration import (
    TARGET_SCHEMA_VERSION,
    SchemaVersionError,
    ensure_layout_schema,
    ensure_ocr_schema,
    ensure_processing_schema,
    ensure_revision_schema,
    refusal_message,
)
from .ocr import (
    DEFAULT_CHAIN,
    IMAGE_SUFFIXES,
    TESSERACT_ENGINE,
    OcrEngine,
    OcrResult,
    RegionProvider,
    image_preflight,
    run_image_ocr,
)
from .ocr_normalization import (
    NORMALIZATION_RULESET_VERSION,
    normalize_transcription,
)
from .ocr_regions import (
    DEFAULT_QUALITY_GATE,
    QualityGate,
    RegionObservation,
    region_key_mark_confidence,
    summarize_outcomes,
)
from .processing import configured_manifest as _configured_manifest
from .revisions import ProcessingManifest
from .state import state_directory_for


RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SPREADSHEET_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SCHEMA_VERSION = TARGET_SCHEMA_VERSION
# Standalone images are indexed as their own documents so OCR has a real path
# for them offline; the import tooling for them belongs to V2-06.
IMAGE_SOURCE_PATTERNS = ("*.png", "*.jpg", "*.jpeg")
SOURCE_PATTERNS = ("*.docx", "*.xlsx", *IMAGE_SOURCE_PATTERNS)
EXCLUDED_SOURCE_DIRECTORIES = frozenset(
    {
        ".git",
        ".index",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
    }
)
# The configured pipeline identity lives in the staged pipeline module; every
# entry point that writes a parse revision has to agree with it, or a healthy
# index reads as stale.
DEFAULT_PROCESSING_MANIFEST = _configured_manifest()
MAX_OOXML_MEMBERS = 10_000
MAX_OOXML_MEMBER_SIZE = 128 * 1024 * 1024
MAX_OOXML_TOTAL_SIZE = 512 * 1024 * 1024
MAX_OOXML_COMPRESSION_RATIO = 200


def index_documents(
    source: Path,
    output: Path,
    *,
    processing_manifest: ProcessingManifest | None = None,
    ocr_engine: str | None = None,
    extra_excluded_directories: Iterable[str] | None = None,
    ocr_providers: Mapping[str, RegionProvider] | None = None,
    ocr_gate: QualityGate | Mapping[str, Any] | None = None,
    ocr_language: str | None = None,
    allow_compatibility_fallback: bool = True,
) -> dict[str, int]:
    """Build the derived index for one source tree.

    ``ocr_engine`` keeps its V1 meaning: when it is unset, images are handed
    straight to the compatibility engine exactly as they were before V2. Naming
    an engine (or injecting a test provider) switches to the degradation chain,
    which also records regions, confidences, and normalization suggestions.
    """

    source = source.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assets_dir = output / "assets"
    assets_dir.mkdir(exist_ok=True)
    manifest = processing_manifest or DEFAULT_PROCESSING_MANIFEST
    fingerprint = manifest.fingerprint
    excluded = excluded_directories(source, extra_excluded_directories)
    engine = _ocr_engine(ocr_engine)
    gate = quality_gate(ocr_gate)
    chain_mode = ocr_engine is not None or bool(ocr_providers)
    outcomes: list[Any] = []

    documents_indexed = 0
    images_indexed = 0
    ocr_succeeded = 0
    ocr_failed = 0
    ocr_unavailable = 0
    documents_added = 0
    documents_updated = 0
    documents_reused = 0
    documents_removed = 0
    assets_removed = 0
    ocr_regions_recorded = 0
    layout_recorded: dict[str, int] = {}
    current_paths = {
        _stored_source_path(path, output)
        for pattern in SOURCE_PATTERNS
        for path in source_documents(source, pattern, excluded)
    }
    with closing(sqlite3.connect(output / "knowledge.sqlite")) as connection, connection:
        existing_schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if existing_schema_version not in (0, SCHEMA_VERSION):
            raise SchemaVersionError(refusal_message(existing_schema_version))
        _create_schema(connection)
        features_indexed, aliases_indexed = _index_catalog(source, output, connection)
        for document_path in source_documents(source, "*.docx", excluded):
            document_id, action = _prepare_document(
                connection,
                document_path,
                "docx",
                output,
                logical_root=source,
                processing_fingerprint=fingerprint,
            )
            documents_indexed += 1
            documents_added += action == "added"
            documents_updated += action == "updated"
            documents_reused += action == "reused"
            if action == "reused":
                continue
            _validate_ooxml_archive(document_path)
            for block in _read_docx_blocks(document_path):
                block_id = connection.execute(
                    """
                    INSERT INTO document_blocks(
                        document_id, ordinal, block_type, heading_level,
                        section_path, style, text, source_part, locator
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        block["ordinal"],
                        block["block_type"],
                        block["heading_level"],
                        json.dumps(block["section_path"], ensure_ascii=False),
                        block["style"],
                        block["text"],
                        "word/document.xml",
                        json.dumps(block["locator"], ensure_ascii=False),
                    ),
                ).lastrowid
                if block["text"] and block["block_type"] in {
                    "heading",
                    "paragraph",
                    "list_item",
                    "table_cell",
                }:
                    evidence_id = connection.execute(
                        """
                        INSERT INTO evidence(
                            document_id, evidence_type, source_table,
                            source_record_id, text, section_path, locator, authority
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            block["block_type"],
                            "document_blocks",
                            block_id,
                            block["text"],
                            json.dumps(block["section_path"], ensure_ascii=False),
                            json.dumps(block["locator"], ensure_ascii=False),
                            "document",
                        ),
                    ).lastrowid
                    connection.execute(
                        "INSERT INTO evidence_fts(evidence_id, text, section_path) VALUES (?, ?, ?)",
                        (
                            evidence_id,
                            block["text"],
                            " / ".join(block["section_path"]),
                        ),
                    )
            for (
                relationship_id,
                media_name,
                image_bytes,
                heading,
                paragraph_index,
                context_text,
            ) in _read_docx_images(document_path):
                digest = hashlib.sha256(image_bytes).hexdigest()
                suffix = Path(media_name).suffix.lower() or ".bin"
                asset_path = assets_dir / f"{digest}{suffix}"
                if not asset_path.exists():
                    asset_path.write_bytes(image_bytes)
                mime_type = mimetypes.guess_type(media_name)[0] or "application/octet-stream"
                ocr = _transcribe_image(
                    asset_path,
                    engine,
                    chain_mode=chain_mode,
                    providers=ocr_providers,
                    gate=gate,
                    language=ocr_language,
                    allow_compatibility_fallback=allow_compatibility_fallback,
                )
                ocr_status, ocr_text, ocr_error = ocr.v1_status, ocr.text, ocr.error
                image_id = connection.execute(
                    """
                    INSERT INTO images(
                        document_id, relationship_id, source_part, sha256,
                        mime_type, asset_path, ocr_status, ocr_text, ocr_error,
                        heading, paragraph_index, context_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        relationship_id,
                        media_name,
                        digest,
                        mime_type,
                        asset_path.relative_to(output).as_posix(),
                        ocr_status,
                        ocr_text,
                        ocr_error,
                        heading,
                        paragraph_index,
                        context_text,
                    ),
                ).lastrowid
                connection.execute(
                    "INSERT INTO image_fts(image_id, context_text, ocr_text, heading) VALUES (?, ?, ?, ?)",
                    (image_id, context_text, ocr_text, heading or ""),
                )
                layout_recorded = _add_counts(
                    layout_recorded,
                    _record_ocr_result(connection, image_id, ocr, asset_path),
                )
                outcomes.append(ocr.outcome)
                ocr_regions_recorded += len(ocr.observation.regions)
                images_indexed += 1
                ocr_succeeded += ocr_status == "succeeded"
                ocr_failed += ocr_status == "failed"
                ocr_unavailable += ocr_status == "unavailable"
        for document_path in source_documents(source, "*.xlsx", excluded):
            document_id, action = _prepare_document(
                connection,
                document_path,
                "xlsx",
                output,
                logical_root=source,
                processing_fingerprint=fingerprint,
            )
            documents_indexed += 1
            documents_added += action == "added"
            documents_updated += action == "updated"
            documents_reused += action == "reused"
            if action == "reused":
                continue
            _validate_ooxml_archive(document_path)
            for sheet_record in _read_xlsx_cells(document_path):
                sheet_id = connection.execute(
                    """
                    INSERT INTO workbook_sheets(
                        document_id, sheet_name, sheet_index, visibility, used_range
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        sheet_record["sheet_name"],
                        sheet_record["sheet_index"],
                        sheet_record["visibility"],
                        sheet_record["used_range"],
                    ),
                ).lastrowid
                for cell in sheet_record["cells"]:
                    cell_id = connection.execute(
                        """
                        INSERT INTO sheet_cells(
                            sheet_id, cell_reference, row_index, column_index,
                            raw_value, display_text, formula, data_type,
                            style_id, merged_range
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sheet_id,
                            cell["cell_reference"],
                            cell["row_index"],
                            cell["column_index"],
                            cell["raw_value"],
                            cell["display_text"],
                            cell["formula"],
                            cell["data_type"],
                            cell["style_id"],
                            cell["merged_range"],
                        ),
                    ).lastrowid
                    connection.execute(
                        """
                        INSERT INTO cell_fts(cell_id, display_text, raw_value, formula)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            cell_id,
                            cell["display_text"],
                            cell["raw_value"],
                            cell["formula"] or "",
                        ),
                    )
                    evidence_text = cell["display_text"] or cell["raw_value"]
                    if evidence_text:
                        locator = {
                            "sheet_name": sheet_record["sheet_name"],
                            "cell_reference": cell["cell_reference"],
                        }
                        evidence_id = connection.execute(
                            """
                            INSERT INTO evidence(
                                document_id, evidence_type, source_table,
                                source_record_id, text, section_path, locator, authority
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                document_id,
                                "config_cell",
                                "sheet_cells",
                                cell_id,
                                evidence_text,
                                json.dumps(
                                    [sheet_record["sheet_name"]], ensure_ascii=False
                                ),
                                json.dumps(locator, ensure_ascii=False),
                                "configuration",
                            ),
                        ).lastrowid
                        connection.execute(
                            """
                            INSERT INTO evidence_fts(evidence_id, text, section_path)
                            VALUES (?, ?, ?)
                            """,
                            (
                                evidence_id,
                                evidence_text,
                                sheet_record["sheet_name"],
                            ),
                        )
            for (
                relationship_id,
                media_name,
                image_bytes,
                sheet_name,
                cell_anchor,
                context_text,
            ) in _read_xlsx_images(document_path):
                digest = hashlib.sha256(image_bytes).hexdigest()
                suffix = Path(media_name).suffix.lower() or ".bin"
                asset_path = assets_dir / f"{digest}{suffix}"
                if not asset_path.exists():
                    asset_path.write_bytes(image_bytes)
                mime_type = mimetypes.guess_type(media_name)[0] or "application/octet-stream"
                ocr = _transcribe_image(
                    asset_path,
                    engine,
                    chain_mode=chain_mode,
                    providers=ocr_providers,
                    gate=gate,
                    language=ocr_language,
                    allow_compatibility_fallback=allow_compatibility_fallback,
                )
                ocr_status, ocr_text, ocr_error = ocr.v1_status, ocr.text, ocr.error
                image_id = connection.execute(
                    """
                    INSERT INTO images(
                        document_id, relationship_id, source_part, sha256,
                        mime_type, asset_path, ocr_status, ocr_text, ocr_error,
                        context_text, sheet_name, cell_anchor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        relationship_id,
                        media_name,
                        digest,
                        mime_type,
                        asset_path.relative_to(output).as_posix(),
                        ocr_status,
                        ocr_text,
                        ocr_error,
                        context_text,
                        sheet_name,
                        cell_anchor,
                    ),
                ).lastrowid
                connection.execute(
                    "INSERT INTO image_fts(image_id, context_text, ocr_text, heading) VALUES (?, ?, ?, ?)",
                    (image_id, context_text, ocr_text, ""),
                )
                layout_recorded = _add_counts(
                    layout_recorded,
                    _record_ocr_result(connection, image_id, ocr, asset_path),
                )
                outcomes.append(ocr.outcome)
                ocr_regions_recorded += len(ocr.observation.regions)
                images_indexed += 1
                ocr_succeeded += ocr_status == "succeeded"
                ocr_failed += ocr_status == "failed"
                ocr_unavailable += ocr_status == "unavailable"

        for document_path in _standalone_images(source, excluded):
            image_result = _index_standalone_image(
                connection,
                document_path,
                output,
                logical_root=source,
                processing_fingerprint=fingerprint,
                engine=engine,
                chain_mode=chain_mode,
                providers=ocr_providers,
                gate=gate,
                language=ocr_language,
                allow_compatibility_fallback=allow_compatibility_fallback,
            )
            if image_result is None:
                continue
            action, ocr, counted = image_result
            layout_recorded = _add_counts(layout_recorded, counted)
            documents_indexed += 1
            documents_added += action == "added"
            documents_updated += action == "updated"
            documents_reused += action == "reused"
            if action == "reused":
                continue
            outcomes.append(ocr.outcome)
            ocr_regions_recorded += len(ocr.observation.regions)
            images_indexed += 1
            ocr_succeeded += ocr.v1_status == "succeeded"
            ocr_failed += ocr.v1_status == "failed"
            ocr_unavailable += ocr.v1_status == "unavailable"

        for stale_document in connection.execute(
            "SELECT id, path FROM documents"
        ).fetchall():
            if stale_document[1] not in current_paths:
                _delete_document(connection, stale_document[0])
                documents_removed += 1

        referenced_assets = {
            (output / row[0]).resolve()
            for row in connection.execute("SELECT DISTINCT asset_path FROM images")
        }
        for asset_path in assets_dir.iterdir():
            if asset_path.is_file() and asset_path.resolve() not in referenced_assets:
                asset_path.unlink()
                assets_removed += 1

    report: dict[str, Any] = {
        "documents_indexed": documents_indexed,
        "images_indexed": images_indexed,
        "ocr_succeeded": ocr_succeeded,
        "ocr_failed": ocr_failed,
        "ocr_unavailable": ocr_unavailable,
        "features_indexed": features_indexed,
        "aliases_indexed": aliases_indexed,
        "documents_added": documents_added,
        "documents_updated": documents_updated,
        "documents_reused": documents_reused,
        "documents_removed": documents_removed,
        "assets_removed": assets_removed,
    }
    # Additive V2 detail. The three V1 counters above keep their exact meaning;
    # these say how many images went through the chain and how the region-level
    # grading turned out.
    report.update(summarize_outcomes(outcomes))
    report["ocr_regions"] = ocr_regions_recorded
    # Layout is derived in the same pass as the transcription it reads, so these
    # counters describe this build's images exactly like the OCR ones above.
    report["layout_elements"] = layout_recorded.get("layout_elements", 0)
    report["layout_relations"] = layout_recorded.get("relations", 0)
    report["layout_confirmed_relations"] = layout_recorded.get(
        "confirmed_relations", 0
    )
    report["layout_candidate_relations"] = layout_recorded.get(
        "candidate_relations", 0
    )
    report["layout_uncertain_relations"] = layout_recorded.get(
        "uncertain_relations", 0
    )
    return report


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            document_type TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            logical_document_id TEXT,
            source_revision_id TEXT,
            parse_revision_id TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS documents_path_unique ON documents(path);

        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            relationship_id TEXT NOT NULL,
            source_part TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            asset_path TEXT NOT NULL,
            ocr_status TEXT NOT NULL,
            ocr_text TEXT NOT NULL,
            ocr_error TEXT,
            heading TEXT,
            paragraph_index INTEGER,
            context_text TEXT NOT NULL DEFAULT '',
            sheet_name TEXT,
            cell_anchor TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS image_fts USING fts5(
            image_id UNINDEXED,
            context_text,
            ocr_text,
            heading,
            tokenize='trigram'
        );

        CREATE TABLE IF NOT EXISTS document_blocks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            ordinal INTEGER NOT NULL,
            block_type TEXT NOT NULL,
            heading_level INTEGER,
            section_path TEXT NOT NULL,
            style TEXT,
            text TEXT NOT NULL,
            source_part TEXT NOT NULL,
            locator TEXT NOT NULL,
            UNIQUE(document_id, ordinal)
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            evidence_type TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_record_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            section_path TEXT NOT NULL,
            locator TEXT NOT NULL,
            authority TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
            evidence_id UNINDEXED,
            text,
            section_path,
            tokenize='trigram'
        );

        CREATE TABLE IF NOT EXISTS workbook_sheets (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            sheet_name TEXT NOT NULL,
            sheet_index INTEGER NOT NULL,
            visibility TEXT NOT NULL,
            used_range TEXT,
            UNIQUE(document_id, sheet_name)
        );

        CREATE TABLE IF NOT EXISTS sheet_cells (
            id INTEGER PRIMARY KEY,
            sheet_id INTEGER NOT NULL REFERENCES workbook_sheets(id),
            cell_reference TEXT NOT NULL,
            row_index INTEGER NOT NULL,
            column_index INTEGER NOT NULL,
            raw_value TEXT NOT NULL,
            display_text TEXT NOT NULL,
            formula TEXT,
            data_type TEXT NOT NULL,
            style_id INTEGER,
            merged_range TEXT,
            UNIQUE(sheet_id, cell_reference)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS cell_fts USING fts5(
            cell_id UNINDEXED,
            display_text,
            raw_value,
            formula,
            tokenize='trigram'
        );

        CREATE TABLE IF NOT EXISTS catalog_features (
            id INTEGER PRIMARY KEY,
            feature_key TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL,
            source TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS catalog_aliases (
            id INTEGER PRIMARY KEY,
            feature_id INTEGER NOT NULL REFERENCES catalog_features(id),
            alias TEXT NOT NULL,
            source TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            confirmed_by TEXT NOT NULL,
            UNIQUE(feature_id, alias)
        );

        CREATE TABLE IF NOT EXISTS catalog_metadata (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            path TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        """
    )
    # The schema version is written explicitly rather than inside the script so
    # that the target version has exactly one author: migration.py.
    connection.execute(f"PRAGMA user_version = {TARGET_SCHEMA_VERSION}")
    ensure_revision_schema(connection)
    ensure_processing_schema(connection)
    ensure_ocr_schema(connection)
    ensure_layout_schema(connection)


def _index_catalog(
    source: Path, output: Path, connection: sqlite3.Connection
) -> tuple[int, int]:
    connection.execute("DELETE FROM catalog_aliases")
    connection.execute("DELETE FROM catalog_features")
    connection.execute("DELETE FROM catalog_metadata")
    catalog_path = _find_catalog_path(source)
    if catalog_path is None:
        return 0, 0
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("features"), list):
        raise ValueError(f"Catalog must contain a features array: {catalog_path}")

    features_indexed = 0
    aliases_indexed = 0
    for feature in catalog["features"]:
        if not isinstance(feature, dict):
            raise ValueError("Each catalog feature must be an object")
        feature_key = _required_catalog_text(feature, "key")
        canonical_name = _required_catalog_text(feature, "canonical_name")
        source_name = str(feature.get("source") or catalog_path.as_posix())
        feature_id = connection.execute(
            """
            INSERT INTO catalog_features(feature_key, canonical_name, source)
            VALUES (?, ?, ?)
            """,
            (feature_key, canonical_name, source_name),
        ).lastrowid
        features_indexed += 1
        aliases = feature.get("aliases", [])
        if not isinstance(aliases, list):
            raise ValueError(f"aliases must be an array for feature {feature_key}")
        for alias in aliases:
            if not isinstance(alias, dict):
                raise ValueError(
                    f"Aliases must be confirmation objects for feature {feature_key}"
                )
            connection.execute(
                """
                INSERT INTO catalog_aliases(
                    feature_id, alias, source, confirmed_at, confirmed_by
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    feature_id,
                    _required_catalog_text(alias, "name"),
                    str(alias.get("source") or source_name),
                    _required_catalog_text(alias, "confirmed_at"),
                    _required_catalog_text(alias, "confirmed_by"),
                ),
            )
            aliases_indexed += 1
    catalog_stat = catalog_path.stat()
    connection.execute(
        """
        INSERT INTO catalog_metadata(
            id, path, source_size, source_mtime_ns, source_sha256, indexed_at
        ) VALUES (1, ?, ?, ?, ?, ?)
        """,
        (
            _stored_source_path(catalog_path, output),
            catalog_stat.st_size,
            catalog_stat.st_mtime_ns,
            catalog_fingerprint(catalog),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return features_indexed, aliases_indexed


def catalog_fingerprint(catalog: object) -> str:
    canonical_json = json.dumps(
        catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _find_catalog_path(source: Path) -> Path | None:
    candidates = [source / "knowledge" / "catalog.json", source / "catalog.json"]
    if source.name.lower() == "docs":
        candidates.append(source.parent / "catalog.json")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _required_catalog_text(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Catalog field {field!r} must be a non-empty string")
    return value.strip()


def _prepare_document(
    connection: sqlite3.Connection,
    path: Path,
    document_type: str,
    output: Path,
    *,
    logical_root: Path | None = None,
    processing_fingerprint: str = "",
) -> tuple[int, str]:
    stored_path = _stored_source_path(path, output)
    source_hash = revisions.file_sha256(path)
    source_stat = path.stat()
    logical_id, source_revision, parse_revision = _revision_identities(
        path, source_hash, logical_root, processing_fingerprint
    )
    existing = connection.execute(
        "SELECT id, source_sha256 FROM documents WHERE path = ?", (stored_path,)
    ).fetchone()
    indexed_at = datetime.now(timezone.utc).isoformat()
    if existing is not None and existing[1] == source_hash:
        # Reuse keeps the derived rows, but the revision identity is a pure
        # function of the bytes and the pipeline, so it is refreshed either way.
        connection.execute(
            """
            UPDATE documents
            SET source_size = ?, source_mtime_ns = ?, indexed_at = ?,
                logical_document_id = ?, source_revision_id = ?,
                parse_revision_id = ?
            WHERE id = ?
            """,
            (
                source_stat.st_size,
                source_stat.st_mtime_ns,
                indexed_at,
                logical_id,
                source_revision,
                parse_revision,
                existing[0],
            ),
        )
        return existing[0], "reused"
    action = "added"
    if existing is not None:
        _delete_document(connection, existing[0])
        action = "updated"
    document_id = connection.execute(
        """
        INSERT INTO documents(
            path, document_type, source_size, source_mtime_ns,
            source_sha256, indexed_at, logical_document_id,
            source_revision_id, parse_revision_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stored_path,
            document_type,
            source_stat.st_size,
            source_stat.st_mtime_ns,
            source_hash,
            indexed_at,
            logical_id,
            source_revision,
            parse_revision,
        ),
    ).lastrowid
    return document_id, action


def _revision_identities(
    path: Path,
    source_hash: str,
    logical_root: Path | None,
    processing_fingerprint: str,
) -> tuple[str | None, str | None, str | None]:
    """Derive the logical document, source revision, and parse revision ids."""

    if logical_root is None:
        return None, None, None
    try:
        logical_path = path.resolve().relative_to(logical_root.resolve()).as_posix()
    except ValueError:
        logical_path = path.name
    logical_id = revisions.document_id(logical_path)
    source_revision = revisions.source_revision_id(logical_id, source_hash)
    if not processing_fingerprint:
        return logical_id, source_revision, None
    parse_revision = revisions.parse_revision_id(
        source_revision,
        database_schema_version=SCHEMA_VERSION,
        processing_fingerprint_value=processing_fingerprint,
    )
    return logical_id, source_revision, parse_revision


def excluded_directories(
    source: Path, extra: Iterable[str] | None
) -> frozenset[str]:
    """Directories that never hold project documents, plus the durable state."""

    names = set(EXCLUDED_SOURCE_DIRECTORIES)
    names.add(state_directory_for(source).name)
    names.update(str(name) for name in extra or ())
    return frozenset(name for name in names if name)


def source_documents(
    source: Path, pattern: str, excluded: Iterable[str]
) -> list[Path]:
    blocked = set(excluded)
    return sorted(
        path
        for path in source.rglob(pattern)
        if not blocked.intersection(path.relative_to(source).parts[:-1])
    )


def _stored_source_path(path: Path, output: Path) -> str:
    """Store a portable path when source and index are on the same filesystem."""
    resolved_path = path.resolve()
    try:
        relative_path = os.path.relpath(resolved_path, output.resolve())
    except ValueError:
        return str(resolved_path)
    return Path(relative_path).as_posix()


def _delete_document(connection: sqlite3.Connection, document_id: int) -> None:
    connection.execute(
        "DELETE FROM evidence_fts WHERE evidence_id IN (SELECT id FROM evidence WHERE document_id = ?)",
        (document_id,),
    )
    connection.execute(
        "DELETE FROM image_fts WHERE image_id IN (SELECT id FROM images WHERE document_id = ?)",
        (document_id,),
    )
    connection.execute(
        """
        DELETE FROM cell_fts
        WHERE cell_id IN (
            SELECT c.id FROM sheet_cells AS c
            JOIN workbook_sheets AS ws ON ws.id = c.sheet_id
            WHERE ws.document_id = ?
        )
        """,
        (document_id,),
    )
    connection.execute("DELETE FROM evidence WHERE document_id = ?", (document_id,))
    connection.execute(
        """
        DELETE FROM ocr_normalizations
        WHERE region_id IN (
            SELECT r.id FROM ocr_regions AS r
            JOIN images AS i ON i.id = r.image_id
            WHERE i.document_id = ?
        )
        """,
        (document_id,),
    )
    connection.execute(
        "DELETE FROM ocr_regions WHERE image_id IN (SELECT id FROM images WHERE document_id = ?)",
        (document_id,),
    )
    connection.execute(
        "DELETE FROM ocr_runs WHERE image_id IN (SELECT id FROM images WHERE document_id = ?)",
        (document_id,),
    )
    for table in ("structural_relations", "layout_elements", "layout_runs"):
        connection.execute(
            f"DELETE FROM {table} "
            "WHERE image_id IN (SELECT id FROM images WHERE document_id = ?)",
            (document_id,),
        )
    connection.execute("DELETE FROM images WHERE document_id = ?", (document_id,))
    connection.execute(
        "DELETE FROM sheet_cells WHERE sheet_id IN (SELECT id FROM workbook_sheets WHERE document_id = ?)",
        (document_id,),
    )
    connection.execute("DELETE FROM workbook_sheets WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM document_blocks WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def _validate_ooxml_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_OOXML_MEMBERS:
            raise ValueError(f"OOXML archive has too many members: {path}")
        total_size = sum(member.file_size for member in members)
        if total_size > MAX_OOXML_TOTAL_SIZE:
            raise ValueError(f"OOXML archive expands beyond the size limit: {path}")
        for member in members:
            if member.file_size > MAX_OOXML_MEMBER_SIZE:
                raise ValueError(
                    f"OOXML member expands beyond the size limit: {member.filename}"
                )
            if member.file_size < 1024 * 1024 or member.compress_size <= 0:
                continue
            compression_ratio = member.file_size / member.compress_size
            if compression_ratio > MAX_OOXML_COMPRESSION_RATIO:
                raise ValueError(
                    "OOXML member has a suspicious compression ratio: "
                    f"{member.filename} ({compression_ratio:.1f}:1)"
                )


def _read_docx_blocks(path: Path):
    with zipfile.ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        body = document.find(f"{{{WORD_NS}}}body")
        if body is None:
            return
        section_path: list[str] = []
        paragraph_index = 0
        ordinal = 0
        table_index = 0
        for child in body:
            if child.tag == f"{{{WORD_NS}}}p":
                paragraph_index += 1
                text = _word_text(child).strip()
                if not text:
                    continue

                style = child.find(f"{{{WORD_NS}}}pPr/{{{WORD_NS}}}pStyle")
                style_name = (
                    style.attrib.get(f"{{{WORD_NS}}}val", "")
                    if style is not None
                    else ""
                )
                heading_level = _heading_level(style_name)
                locator: dict[str, int] = {"paragraph_index": paragraph_index}
                if heading_level is not None:
                    section_path = section_path[: heading_level - 1]
                    while len(section_path) < heading_level - 1:
                        section_path.append("")
                    section_path.append(text)
                    block_type = "heading"
                else:
                    list_level = _list_level(child)
                    if list_level is None:
                        block_type = "paragraph"
                    else:
                        block_type = "list_item"
                        locator["list_level"] = list_level

                ordinal += 1
                yield {
                    "ordinal": ordinal,
                    "block_type": block_type,
                    "heading_level": heading_level,
                    "section_path": [part for part in section_path if part],
                    "style": style_name or None,
                    "text": text,
                    "locator": locator,
                }
                continue

            if child.tag != f"{{{WORD_NS}}}tbl":
                continue
            table_index += 1
            ordinal += 1
            yield _docx_structure_block(
                ordinal,
                "table",
                section_path,
                {"table_index": table_index},
            )
            for row_index, row in enumerate(
                child.findall(f"{{{WORD_NS}}}tr"), start=1
            ):
                ordinal += 1
                yield _docx_structure_block(
                    ordinal,
                    "table_row",
                    section_path,
                    {"table_index": table_index, "row_index": row_index},
                )
                for cell_index, cell in enumerate(
                    row.findall(f"{{{WORD_NS}}}tc"), start=1
                ):
                    ordinal += 1
                    yield {
                        "ordinal": ordinal,
                        "block_type": "table_cell",
                        "heading_level": None,
                        "section_path": [part for part in section_path if part],
                        "style": None,
                        "text": _word_text(cell).strip(),
                        "locator": {
                            "table_index": table_index,
                            "row_index": row_index,
                            "cell_index": cell_index,
                        },
                    }


def _word_text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{{{WORD_NS}}}t"))


def _list_level(paragraph: ET.Element) -> int | None:
    level = paragraph.find(
        f"{{{WORD_NS}}}pPr/{{{WORD_NS}}}numPr/{{{WORD_NS}}}ilvl"
    )
    if level is None:
        return None
    value = level.attrib.get(f"{{{WORD_NS}}}val", "")
    return int(value) if value.isdigit() else 0


def _docx_structure_block(
    ordinal: int,
    block_type: str,
    section_path: list[str],
    locator: dict[str, int],
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "block_type": block_type,
        "heading_level": None,
        "section_path": [part for part in section_path if part],
        "style": None,
        "text": "",
        "locator": locator,
    }


def _heading_level(style_name: str) -> int | None:
    match = re.match(r"^(?:Heading|标题)\s*([1-9]\d*)$", style_name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _read_docx_images(path: Path):
    with zipfile.ZipFile(path) as archive:
        relationships = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        targets = {
            relationship.attrib["Id"]: relationship.attrib["Target"]
            for relationship in relationships.findall(f"{{{RELATIONSHIPS_NS}}}Relationship")
            if relationship.attrib.get("Type", "").endswith("/image")
        }
        document = ET.fromstring(archive.read("word/document.xml"))
        current_heading = None
        paragraph_index = 0
        for paragraph in document.iter(f"{{{WORD_NS}}}p"):
            paragraph_index += 1
            context_text = "".join(
                node.text or "" for node in paragraph.iter(f"{{{WORD_NS}}}t")
            )
            style = paragraph.find(
                f"{{{WORD_NS}}}pPr/{{{WORD_NS}}}pStyle"
            )
            style_name = style.attrib.get(f"{{{WORD_NS}}}val", "") if style is not None else ""
            if style_name.lower().startswith("heading") or style_name.startswith("标题"):
                current_heading = context_text

            for blip in paragraph.iter(f"{{{DRAWING_NS}}}blip"):
                relationship_id = blip.attrib.get(f"{{{OFFICE_REL_NS}}}embed")
                if relationship_id not in targets:
                    continue
                target = PurePosixPath(targets[relationship_id])
                media_part = (PurePosixPath("word") / target).as_posix()
                if ".." in PurePosixPath(media_part).parts:
                    raise ValueError(f"Unsafe DOCX media relationship: {target}")
                yield (
                    relationship_id,
                    media_part,
                    archive.read(media_part),
                    current_heading,
                    paragraph_index,
                    context_text,
                )


def _read_xlsx_images(path: Path):
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_relationships = _relationship_targets(
            archive.read("xl/_rels/workbook.xml.rels"), "/worksheet"
        )
        for sheet in workbook.iter(f"{{{SPREADSHEET_NS}}}sheet"):
            sheet_relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id")
            if sheet_relationship_id not in workbook_relationships:
                continue
            sheet_part = _resolve_part("xl/workbook.xml", workbook_relationships[sheet_relationship_id])
            sheet_xml = ET.fromstring(archive.read(sheet_part))
            sheet_name = sheet.attrib.get("name", "")
            cell_texts = _sheet_cell_texts(sheet_xml, shared_strings)
            drawings = list(sheet_xml.iter(f"{{{SPREADSHEET_NS}}}drawing"))
            if not drawings:
                continue
            relationships_part = _rels_part(sheet_part)
            if relationships_part not in archive.namelist():
                raise ValueError(
                    f"Worksheet declares a drawing but has no relationships: {sheet_part}"
                )
            sheet_relationships = _relationship_targets(archive.read(relationships_part))
            for drawing in drawings:
                drawing_relationship_id = drawing.attrib.get(f"{{{OFFICE_REL_NS}}}id")
                drawing_target = sheet_relationships.get(drawing_relationship_id)
                if drawing_target is None:
                    continue
                drawing_part = _resolve_part(sheet_part, drawing_target)
                drawing_xml = ET.fromstring(archive.read(drawing_part))
                image_relationships = _relationship_targets(
                    archive.read(_rels_part(drawing_part)), "/image"
                )
                for anchor in list(drawing_xml):
                    cell_anchor = _drawing_cell_anchor(anchor)
                    context_text = cell_texts.get(cell_anchor, "") if cell_anchor else ""
                    for blip in anchor.iter(f"{{{DRAWING_NS}}}blip"):
                        image_relationship_id = blip.attrib.get(f"{{{OFFICE_REL_NS}}}embed")
                        image_target = image_relationships.get(image_relationship_id)
                        if image_target is None:
                            continue
                        media_part = _resolve_part(drawing_part, image_target)
                        yield (
                            image_relationship_id,
                            media_part,
                            archive.read(media_part),
                            sheet_name,
                            cell_anchor,
                            context_text,
                        )


def _read_xlsx_cells(path: Path):
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_relationships = _relationship_targets(
            archive.read("xl/_rels/workbook.xml.rels"), "/worksheet"
        )
        for sheet_index, sheet in enumerate(
            workbook.iter(f"{{{SPREADSHEET_NS}}}sheet"), start=1
        ):
            relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id")
            if relationship_id not in workbook_relationships:
                continue
            sheet_part = _resolve_part(
                "xl/workbook.xml", workbook_relationships[relationship_id]
            )
            sheet_xml = ET.fromstring(archive.read(sheet_part))
            dimension = sheet_xml.find(f"{{{SPREADSHEET_NS}}}dimension")
            merged_ranges = [
                node.attrib["ref"]
                for node in sheet_xml.iter(f"{{{SPREADSHEET_NS}}}mergeCell")
                if node.attrib.get("ref")
            ]
            cells = []
            for cell in sheet_xml.iter(f"{{{SPREADSHEET_NS}}}c"):
                cell_reference = cell.attrib.get("r", "").upper()
                coordinates = _cell_coordinates(cell_reference)
                if coordinates is None:
                    continue
                raw_node = cell.find(f"{{{SPREADSHEET_NS}}}v")
                raw_value = (
                    raw_node.text
                    if raw_node is not None and raw_node.text is not None
                    else ""
                )
                data_type = cell.attrib.get("t", "n")
                if data_type == "inlineStr":
                    display_text = _word_or_sheet_text(cell)
                    if not raw_value:
                        raw_value = display_text
                elif data_type == "s" and raw_value.isdigit():
                    shared_index = int(raw_value)
                    display_text = (
                        shared_strings[shared_index]
                        if shared_index < len(shared_strings)
                        else ""
                    )
                else:
                    display_text = raw_value
                formula_node = cell.find(f"{{{SPREADSHEET_NS}}}f")
                formula = (
                    formula_node.text
                    if formula_node is not None and formula_node.text is not None
                    else None
                )
                style_value = cell.attrib.get("s", "")
                cells.append(
                    {
                        "cell_reference": cell_reference,
                        "row_index": coordinates[0],
                        "column_index": coordinates[1],
                        "raw_value": raw_value,
                        "display_text": display_text,
                        "formula": formula,
                        "data_type": data_type,
                        "style_id": int(style_value) if style_value.isdigit() else None,
                        "merged_range": _merged_range_for_cell(
                            cell_reference, merged_ranges
                        ),
                    }
                )
            yield {
                "sheet_name": sheet.attrib.get("name", ""),
                "sheet_index": sheet_index,
                "visibility": sheet.attrib.get("state", "visible"),
                "used_range": dimension.attrib.get("ref") if dimension is not None else None,
                "cells": cells,
            }


def _word_or_sheet_text(element: ET.Element) -> str:
    return "".join(
        node.text or "" for node in element.iter(f"{{{SPREADSHEET_NS}}}t")
    )


def _cell_coordinates(reference: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Z]+)\$?([1-9]\d*)", reference.upper())
    if match is None:
        return None
    column_index = 0
    for letter in match.group(1):
        column_index = column_index * 26 + ord(letter) - 64
    return int(match.group(2)), column_index


def _merged_range_for_cell(reference: str, ranges: list[str]) -> str | None:
    coordinates = _cell_coordinates(reference)
    if coordinates is None:
        return None
    row_index, column_index = coordinates
    for merged_range in ranges:
        parts = merged_range.split(":", maxsplit=1)
        start = _cell_coordinates(parts[0])
        end = _cell_coordinates(parts[-1])
        if start is None or end is None:
            continue
        if (
            start[0] <= row_index <= end[0]
            and start[1] <= column_index <= end[1]
        ):
            return merged_range
    return None


def _relationship_targets(xml_bytes: bytes, type_suffix: str | None = None) -> dict[str, str]:
    relationships = ET.fromstring(xml_bytes)
    return {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(f"{{{RELATIONSHIPS_NS}}}Relationship")
        if type_suffix is None or relationship.attrib.get("Type", "").endswith(type_suffix)
    }


def _resolve_part(source_part: str, target: str) -> str:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        combined = target.lstrip("/")
    else:
        combined = (PurePosixPath(source_part).parent / target_path).as_posix()
    resolved = PurePosixPath(posixpath.normpath(combined))
    if resolved.is_absolute() or ".." in resolved.parts:
        raise ValueError(f"Unsafe OOXML relationship target: {target}")
    return resolved.as_posix()


def _rels_part(part: str) -> str:
    path = PurePosixPath(part)
    return (path.parent / "_rels" / f"{path.name}.rels").as_posix()


def _ocr_engine(name: str | None) -> OcrEngine:
    """Resolve the engine the pipeline selected; V1 behaviour when unset."""

    if not name:
        return TESSERACT_ENGINE
    for engine in DEFAULT_CHAIN:
        if engine.name == name:
            return engine
    raise ValueError(f"unknown OCR engine: {name}")


def _run_ocr(
    asset_path: Path, engine: OcrEngine = TESSERACT_ENGINE
) -> tuple[str, str, str | None]:
    return engine.transcribe(Path(asset_path))


def _transcribe_image(
    asset_path: Path,
    engine: OcrEngine,
    *,
    chain_mode: bool,
    providers: Mapping[str, RegionProvider] | None,
    gate: QualityGate,
    language: str | None,
    allow_compatibility_fallback: bool,
) -> OcrResult:
    """One image, either through the chain or through the V1 single engine."""

    if not chain_mode:
        status, text, error = engine.transcribe(Path(asset_path))
        provider_status = {
            "succeeded": "succeeded",
            "failed": "failed",
            "unavailable": "unavailable",
        }.get(status, "failed")
        observation = RegionObservation(
            engine=engine.name,
            provider_status=provider_status,
            flat_text=text or "",
            detail=error or "",
            language=language or "",
            requested_engine=engine.name,
        )
        return OcrResult(
            observation=observation,
            outcome=observation.outcome(gate),
            attempts=(),
            requested_engine=engine.name,
        )
    return run_image_ocr(
        Path(asset_path),
        engine=engine,
        providers=providers,
        gate=gate,
        language=language,
        allow_compatibility_fallback=allow_compatibility_fallback,
    )


def _record_ocr_result(
    connection: sqlite3.Connection,
    image_id: int,
    result: OcrResult,
    asset_path: Path,
) -> dict[str, int]:
    """Persist the run, its regions, the suggestion per region, and its layout."""

    observation = result.observation
    outcome = result.outcome
    now = _now_iso()
    run_id = connection.execute(
        """
        INSERT INTO ocr_runs(
            image_id, requested_engine, engine, engine_version, tier,
            fallback_used, execution_status, quality_status, reason_code,
            detail, evidence_state, language, reading_order_source,
            region_count, reason_chain, duration_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_id,
            result.requested_engine,
            observation.engine,
            observation.engine_version,
            observation.tier,
            int(observation.fallback_used),
            outcome.execution_status,
            outcome.quality_status,
            outcome.reason_code,
            outcome.detail,
            outcome.evidence_state,
            observation.language,
            observation.reading_order_source,
            len(observation.regions),
            json.dumps([dict(entry) for entry in observation.reason_chain], ensure_ascii=False),
            observation.duration_ms,
            now,
        ),
    ).lastrowid
    for region in observation.ordered():
        region_id = connection.execute(
            """
            INSERT INTO ocr_regions(
                run_id, image_id, region_index, reading_order, bbox, text_raw,
                text_confidence, region_confidence, key_mark_confidence, language
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                image_id,
                region.index,
                region.reading_order,
                (
                    json.dumps(region.bbox.as_payload())
                    if region.bbox is not None
                    else None
                ),
                region.text,
                region.text_confidence,
                region.region_confidence,
                region_key_mark_confidence(region),
                region.language or observation.language,
            ),
        ).lastrowid
        suggestion = normalize_transcription(region.text)
        if suggestion.changed:
            connection.execute(
                """
                INSERT INTO ocr_normalizations(
                    region_id, ruleset_version, normalized_text, changes, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    region_id,
                    NORMALIZATION_RULESET_VERSION,
                    suggestion.normalized,
                    json.dumps(
                        [change.as_payload() for change in suggestion.changes],
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
    return _record_layout_result(connection, image_id, result, asset_path)


def _record_layout_result(
    connection: sqlite3.Connection,
    image_id: int,
    result: OcrResult,
    asset_path: Path,
) -> dict[str, int]:
    """Persist the reading order and relations derived from this run's regions.

    The geometry is read straight back off the regions the run just recorded, so
    the layout can never describe a different transcription than the one stored
    beside it. An image with no regions still gets a run row: "this image had
    nothing to order" is a fact worth storing next to "this image was never
    looked at".

    Which way an arrow block points is measured from that block's own pixels
    (``ink.arrow_ink_readings``) rather than believed from the transcription
    alone, so a mirrored glyph reading cannot reverse a flow (issue #28).
    """

    regions = result.observation.ordered()
    order = build_reading_order(regions)
    relations = build_relations(order, ink=arrow_ink_readings(asset_path, regions))
    counted = summarize_relations(relations)
    counted["layout_elements"] = len(order.elements)
    now = _now_iso()
    run_id = connection.execute(
        """
        INSERT INTO layout_runs(
            image_id, ruleset_version, order_source, column_count, element_count,
            geometry_confidence, uncertainty, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_id,
            order.ruleset_version,
            order.order_source,
            order.column_count,
            len(order.elements),
            order.geometry_confidence,
            order.uncertainty,
            order.detail,
            now,
        ),
    ).lastrowid
    for element in order.elements:
        connection.execute(
            """
            INSERT INTO layout_elements(
                run_id, image_id, region_index, kind, direction, reading_order,
                row_index, column_index, depth_hint, bbox, text_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                image_id,
                element.region_index,
                element.kind,
                element.direction,
                element.reading_order,
                element.row,
                element.column,
                element.depth_hint,
                (
                    json.dumps(element.bbox.as_payload())
                    if element.bbox is not None
                    else None
                ),
                element.text_confidence,
            ),
        )
    for relation in relations:
        connection.execute(
            """
            INSERT INTO structural_relations(
                run_id, image_id, kind, status, source_region, target_region,
                via_regions, direction, geometry_basis, detail, uncertainty,
                geometry_confidence, ocr_confidence, rule_version, claim_boundary,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                image_id,
                relation.kind,
                relation.status,
                relation.source_region,
                relation.target_region,
                json.dumps(list(relation.via_regions), ensure_ascii=False),
                relation.direction,
                relation.geometry_basis,
                relation.detail,
                relation.uncertainty,
                relation.geometry_confidence,
                relation.ocr_confidence,
                relation.rule_version,
                relation.claim_boundary,
                now,
            ),
        )
    return counted


def _add_counts(total: dict[str, int], counted: Mapping[str, int]) -> dict[str, int]:
    """Accumulate one image's layout counters into the build report."""

    for key, value in counted.items():
        total[key] = total.get(key, 0) + value
    return total


def _standalone_images(source: Path, excluded: Iterable[str]) -> list[Path]:
    """Image files that are their own document rather than an embedded asset."""

    found: list[Path] = []
    for pattern in IMAGE_SOURCE_PATTERNS:
        found.extend(source_documents(source, pattern, excluded))
    return sorted(set(found))


def _index_standalone_image(
    connection: sqlite3.Connection,
    path: Path,
    output: Path,
    *,
    logical_root: Path,
    processing_fingerprint: str,
    engine: OcrEngine,
    chain_mode: bool,
    providers: Mapping[str, RegionProvider] | None,
    gate: QualityGate,
    language: str | None,
    allow_compatibility_fallback: bool,
) -> tuple[str, OcrResult | None, dict[str, int]]:
    """Index one standalone PNG/JPEG as its own document with one image row."""

    document_id, action = _prepare_document(
        connection,
        path,
        "image",
        output,
        logical_root=logical_root,
        processing_fingerprint=processing_fingerprint,
    )
    if action == "reused":
        return action, None, {}
    digest = revisions.file_sha256(path)
    assets_dir = output / "assets"
    asset_path = assets_dir / f"{digest}{path.suffix.lower()}"
    if not asset_path.exists():
        asset_path.write_bytes(path.read_bytes())
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    ocr = _transcribe_image(
        asset_path,
        engine,
        chain_mode=chain_mode,
        providers=providers,
        gate=gate,
        language=language,
        allow_compatibility_fallback=allow_compatibility_fallback,
    )
    image_id = connection.execute(
        """
        INSERT INTO images(
            document_id, relationship_id, source_part, sha256, mime_type,
            asset_path, ocr_status, ocr_text, ocr_error, heading,
            paragraph_index, context_text, sheet_name, cell_anchor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            "standalone",
            _source_part(path, logical_root),
            digest,
            mime_type,
            asset_path.relative_to(output).as_posix(),
            ocr.v1_status,
            ocr.text,
            ocr.error,
            "",
            None,
            "",
            None,
            None,
        ),
    ).lastrowid
    connection.execute(
        "INSERT INTO image_fts(image_id, context_text, ocr_text, heading) VALUES (?, ?, ?, ?)",
        (image_id, _source_part(path, logical_root), ocr.text, ""),
    )
    counted = _record_ocr_result(connection, image_id, ocr, asset_path)
    return action, ocr, counted


def _source_part(path: Path, logical_root: Path) -> str:
    try:
        return path.resolve().relative_to(logical_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quality_gate(value: QualityGate | Mapping[str, Any] | None) -> QualityGate:
    """Accept the configured gate either as an object or as recorded payload."""

    if value is None:
        return DEFAULT_QUALITY_GATE
    if isinstance(value, QualityGate):
        return value
    default = DEFAULT_QUALITY_GATE
    try:
        return QualityGate(
            min_text_confidence=float(
                value.get("min_text_confidence", default.min_text_confidence)
            ),
            min_key_mark_confidence=float(
                value.get("min_key_mark_confidence", default.min_key_mark_confidence)
            ),
            min_characters=int(value.get("min_characters", default.min_characters)),
        )
    except (TypeError, ValueError):
        return default


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    part = "xl/sharedStrings.xml"
    if part not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(part))
    return [
        "".join(node.text or "" for node in item.iter(f"{{{SPREADSHEET_NS}}}t"))
        for item in root.iter(f"{{{SPREADSHEET_NS}}}si")
    ]


def _sheet_cell_texts(sheet: ET.Element, shared_strings: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for cell in sheet.iter(f"{{{SPREADSHEET_NS}}}c"):
        reference = cell.attrib.get("r")
        if not reference:
            continue
        if cell.attrib.get("t") == "inlineStr":
            result[reference] = "".join(
                node.text or "" for node in cell.iter(f"{{{SPREADSHEET_NS}}}t")
            )
            continue
        value = cell.find(f"{{{SPREADSHEET_NS}}}v")
        text = value.text if value is not None and value.text is not None else ""
        if cell.attrib.get("t") == "s" and text.isdigit():
            index = int(text)
            text = shared_strings[index] if index < len(shared_strings) else ""
        result[reference] = text
    return result


def _drawing_cell_anchor(anchor: ET.Element) -> str | None:
    origin = anchor.find(f"{{{SPREADSHEET_DRAWING_NS}}}from")
    if origin is None:
        return None
    column = origin.find(f"{{{SPREADSHEET_DRAWING_NS}}}col")
    row = origin.find(f"{{{SPREADSHEET_DRAWING_NS}}}row")
    if column is None or row is None or column.text is None or row.text is None:
        return None
    return f"{_column_name(int(column.text))}{int(row.text) + 1}"


def _column_name(zero_based_column: int) -> str:
    column = zero_based_column + 1
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
