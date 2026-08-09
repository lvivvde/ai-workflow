from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

from mcp.server import MCPServer

from .policy import EVIDENCE_POLICY


mcp = MCPServer(
    "game-design-knowledge",
    instructions=EVIDENCE_POLICY,
)


@mcp.tool()
def index_status() -> dict[str, object]:
    """Return index counts, OCR outcomes, timestamp, and source freshness."""
    return _index_status_for_database(_database_path())


def _index_status_for_database(database_path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM documents) AS documents_indexed,
                COUNT(*) AS images_indexed,
                COALESCE(SUM(ocr_status = 'succeeded'), 0) AS ocr_succeeded,
                COALESCE(SUM(ocr_status = 'failed'), 0) AS ocr_failed,
                COALESCE(SUM(ocr_status = 'unavailable'), 0) AS ocr_unavailable
            FROM images
            """
        ).fetchone()
        documents = connection.execute(
            "SELECT path, source_size, source_mtime_ns, indexed_at FROM documents"
        ).fetchall()
        catalog = connection.execute(
            "SELECT path, source_size, source_mtime_ns, indexed_at FROM catalog_metadata WHERE id = 1"
        ).fetchone()

    stale_documents = 0
    for document in documents:
        source_path = Path(document["path"])
        try:
            source_stat = source_path.stat()
        except OSError:
            stale_documents += 1
            continue
        if (
            source_stat.st_size != document["source_size"]
            or source_stat.st_mtime_ns != document["source_mtime_ns"]
        ):
            stale_documents += 1

    indexed_at = max((document["indexed_at"] for document in documents), default=None)
    catalog_is_stale = False
    if catalog is not None:
        catalog_path = Path(catalog["path"])
        try:
            catalog_stat = catalog_path.stat()
        except OSError:
            catalog_is_stale = True
        else:
            catalog_is_stale = (
                catalog_stat.st_size != catalog["source_size"]
                or catalog_stat.st_mtime_ns != catalog["source_mtime_ns"]
            )
        if indexed_at is None or catalog["indexed_at"] > indexed_at:
            indexed_at = catalog["indexed_at"]
    return {
        "schema_version": schema_version,
        "database_path": str(database_path),
        "indexed_at": indexed_at,
        "documents_indexed": counts["documents_indexed"],
        "images_indexed": counts["images_indexed"],
        "ocr_succeeded": counts["ocr_succeeded"],
        "ocr_failed": counts["ocr_failed"],
        "ocr_unavailable": counts["ocr_unavailable"],
        "stale_documents": stale_documents,
        "catalog_configured": catalog is not None,
        "catalog_is_stale": catalog_is_stale,
        "is_stale": stale_documents > 0 or catalog_is_stale,
    }


@mcp.tool()
def search_images(query: str, limit: int = 10) -> dict[str, list[dict[str, object]]]:
    """Search document evidence only; an empty result must not be filled by inference."""
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")

    phrase = f'"{query.replace(chr(34), chr(34) * 2)}"'
    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                i.id AS image_id,
                d.path AS source_document,
                d.document_type,
                i.asset_path,
                i.heading,
                i.paragraph_index,
                i.context_text,
                i.sheet_name,
                i.cell_anchor,
                i.ocr_status,
                i.ocr_text,
                bm25(image_fts) AS score
            FROM image_fts
            JOIN images AS i ON i.id = image_fts.image_id
            JOIN documents AS d ON d.id = i.document_id
            WHERE image_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (phrase, limit),
        ).fetchall()
    matches = [dict(row) for row in rows]
    for match in matches:
        match["asset_path"] = str((database_path.parent / str(match["asset_path"])).resolve())
    return {"matches": matches}


@mcp.tool()
def search_evidence(
    query: str,
    document_type: str | None = None,
    evidence_type: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    """Search indexed document facts; never add inferred or unindexed claims."""
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")

    phrase = f'"{query.replace(chr(34), chr(34) * 2)}"'
    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                e.id AS evidence_id,
                d.path AS source_document,
                d.document_type,
                e.evidence_type,
                e.text,
                e.section_path,
                e.locator,
                b.ordinal AS block_ordinal,
                bm25(evidence_fts) AS score
            FROM evidence_fts
            JOIN evidence AS e ON e.id = evidence_fts.evidence_id
            JOIN documents AS d ON d.id = e.document_id
            LEFT JOIN document_blocks AS b
              ON e.source_table = 'document_blocks'
             AND b.id = e.source_record_id
            WHERE evidence_fts MATCH ?
              AND (? IS NULL OR d.document_type = ?)
              AND (? IS NULL OR e.evidence_type = ?)
            ORDER BY score, e.id
            LIMIT ?
            """,
            (
                phrase,
                document_type,
                document_type,
                evidence_type,
                evidence_type,
                limit,
            ),
        ).fetchall()
        if not rows and len(query) < 3:
            rows = connection.execute(
                """
                SELECT
                    e.id AS evidence_id,
                    d.path AS source_document,
                    d.document_type,
                    e.evidence_type,
                    e.text,
                    e.section_path,
                    e.locator,
                    b.ordinal AS block_ordinal,
                    0.0 AS score
                FROM evidence AS e
                JOIN documents AS d ON d.id = e.document_id
                LEFT JOIN document_blocks AS b
                  ON e.source_table = 'document_blocks'
                 AND b.id = e.source_record_id
                WHERE (instr(e.text, ?) > 0 OR instr(e.section_path, ?) > 0)
                  AND (? IS NULL OR d.document_type = ?)
                  AND (? IS NULL OR e.evidence_type = ?)
                ORDER BY e.id
                LIMIT ?
                """,
                (
                    query,
                    query,
                    document_type,
                    document_type,
                    evidence_type,
                    evidence_type,
                    limit,
                ),
            ).fetchall()

    evidence = []
    for row in rows:
        item = dict(row)
        item["section_path"] = json.loads(item["section_path"])
        item["locator"] = json.loads(item["locator"])
        evidence.append(item)
    status_summary = _index_status_for_database(database_path)
    return {
        "status": (
            "stale"
            if status_summary["is_stale"]
            else "found" if evidence else "not_found"
        ),
        "query": query,
        "match_type": "exact" if evidence else None,
        "evidence": evidence,
        "conflicts": _detect_evidence_conflicts(evidence),
        "limitations": (
            []
            if evidence
            else ["当前索引的文档原文中未找到该查询，且未进行相似玩法联想。"]
        ),
        "index_status": status_summary,
    }


def _detect_evidence_conflicts(
    evidence: list[dict[str, object]],
) -> list[dict[str, object]]:
    groups: dict[tuple[tuple[str, ...], str], list[dict[str, object]]] = {}
    for item in evidence:
        if item.get("document_type") != "docx" or item.get("evidence_type") == "heading":
            continue
        key = (
            tuple(str(part) for part in item.get("section_path", [])),
            str(item.get("evidence_type")),
        )
        groups.setdefault(key, []).append(item)

    conflicts = []
    for (section_path, evidence_type), items in groups.items():
        distinct_texts = {str(item.get("text")) for item in items}
        distinct_documents = {str(item.get("source_document")) for item in items}
        if len(distinct_texts) < 2 or len(distinct_documents) < 2:
            continue
        conflicts.append(
            {
                "type": "potential_conflict",
                "reason": "同一章节路径和证据类型在不同文档中记录了不同原文。",
                "section_path": list(section_path),
                "evidence_type": evidence_type,
                "evidence": [
                    {
                        "evidence_id": item["evidence_id"],
                        "source_document": item["source_document"],
                        "text": item["text"],
                        "locator": item["locator"],
                    }
                    for item in items
                ],
            }
        )
    return conflicts


@mcp.tool()
def get_evidence(
    evidence_id: int,
    context_before: int = 1,
    context_after: int = 1,
) -> dict[str, object]:
    """Return one traceable fact and adjacent blocks from the same document only."""
    if context_before < 0 or context_before > 20:
        raise ValueError("context_before must be between 0 and 20")
    if context_after < 0 or context_after > 20:
        raise ValueError("context_after must be between 0 and 20")

    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                e.id AS evidence_id,
                e.document_id,
                e.source_table,
                e.source_record_id,
                e.evidence_type,
                e.text,
                e.section_path,
                e.locator,
                e.authority,
                d.path AS source_document,
                d.document_type,
                b.ordinal AS block_ordinal
            FROM evidence AS e
            JOIN documents AS d ON d.id = e.document_id
            LEFT JOIN document_blocks AS b
              ON e.source_table = 'document_blocks'
             AND b.id = e.source_record_id
            WHERE e.id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "not_found",
                "evidence_id": evidence_id,
                "evidence": None,
                "context_before": [],
                "context_after": [],
            }

        before_rows: list[sqlite3.Row] = []
        after_rows: list[sqlite3.Row] = []
        if row["source_table"] == "document_blocks":
            before_rows = connection.execute(
                """
                SELECT block_type, text, section_path, locator, ordinal AS block_ordinal
                FROM document_blocks
                WHERE document_id = ? AND ordinal < ?
                ORDER BY ordinal DESC
                LIMIT ?
                """,
                (row["document_id"], row["block_ordinal"], context_before),
            ).fetchall()
            before_rows = list(reversed(before_rows))
            after_rows = connection.execute(
                """
                SELECT block_type, text, section_path, locator, ordinal AS block_ordinal
                FROM document_blocks
                WHERE document_id = ? AND ordinal > ?
                ORDER BY ordinal
                LIMIT ?
                """,
                (row["document_id"], row["block_ordinal"], context_after),
            ).fetchall()

    evidence = dict(row)
    evidence.pop("document_id")
    evidence.pop("source_table")
    evidence.pop("source_record_id")
    evidence["section_path"] = json.loads(evidence["section_path"])
    evidence["locator"] = json.loads(evidence["locator"])
    return {
        "status": "found",
        "evidence_id": evidence_id,
        "evidence": evidence,
        "context_before": [_document_block_result(item) for item in before_rows],
        "context_after": [_document_block_result(item) for item in after_rows],
        "index_status": _index_status_for_database(database_path),
    }


def _document_block_result(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    result["section_path"] = json.loads(result["section_path"])
    result["locator"] = json.loads(result["locator"])
    return result


@mcp.tool()
def search_config_cells(
    query: str,
    workbook: str | None = None,
    sheet: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """Search exact XLSX cell text, raw values, and formulas without inference."""
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")

    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                d.path AS source_document,
                ws.sheet_name,
                c.cell_reference,
                c.row_index,
                c.column_index,
                c.raw_value,
                c.display_text,
                c.formula,
                c.data_type,
                c.style_id,
                c.merged_range
            FROM sheet_cells AS c
            JOIN workbook_sheets AS ws ON ws.id = c.sheet_id
            JOIN documents AS d ON d.id = ws.document_id
            WHERE (
                    instr(c.display_text, ?) > 0
                 OR instr(c.raw_value, ?) > 0
                 OR instr(COALESCE(c.formula, ''), ?) > 0
            )
              AND (? IS NULL OR d.path = ? OR d.path LIKE ? OR d.path LIKE ?)
              AND (? IS NULL OR ws.sheet_name = ?)
            ORDER BY d.path, ws.sheet_index, c.row_index, c.column_index
            LIMIT ?
            """,
            (
                query,
                query,
                query,
                workbook,
                workbook,
                f"%/{workbook}" if workbook is not None else None,
                f"%\\{workbook}" if workbook is not None else None,
                sheet,
                sheet,
                limit,
            ),
        ).fetchall()

    cells = []
    for row in rows:
        cell = dict(row)
        cell["workbook"] = Path(cell["source_document"]).name
        cells.append(cell)
    status_summary = _index_status_for_database(database_path)
    return {
        "status": (
            "stale"
            if status_summary["is_stale"]
            else "found" if cells else "not_found"
        ),
        "query": query,
        "match_type": "exact" if cells else None,
        "cells": cells,
        "limitations": (
            []
            if cells
            else ["当前索引的工作簿原文中未找到该单元格事实。"]
        ),
        "index_status": status_summary,
    }


@mcp.tool()
def get_sheet_range(workbook: str, sheet: str, range: str) -> dict[str, object]:
    """Return stored XLSX facts inside an exact A1 range without interpreting them."""
    workbook = workbook.strip()
    sheet = sheet.strip()
    requested_range = range.strip().upper()
    if not workbook or not sheet or not requested_range:
        raise ValueError("workbook, sheet, and range must not be empty")
    start_row, start_column, end_row, end_column, normalized_range = _parse_a1_range(
        requested_range
    )

    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        sheets = connection.execute(
            """
            SELECT ws.id, d.path AS source_document
            FROM workbook_sheets AS ws
            JOIN documents AS d ON d.id = ws.document_id
            WHERE ws.sheet_name = ?
              AND (d.path = ? OR d.path LIKE ? OR d.path LIKE ?)
            ORDER BY d.path
            """,
            (sheet, workbook, f"%/{workbook}", f"%\\{workbook}"),
        ).fetchall()
        if not sheets:
            return {
                "status": "not_found",
                "workbook": workbook,
                "sheet": sheet,
                "range": normalized_range,
                "cells": [],
                "limitations": ["索引中未找到指定工作簿与工作表。"],
            }
        if len(sheets) > 1:
            return {
                "status": "ambiguous",
                "workbook": workbook,
                "sheet": sheet,
                "range": normalized_range,
                "cells": [],
                "candidates": [item["source_document"] for item in sheets],
                "limitations": ["存在多个同名工作簿，请使用完整路径。"],
            }
        cells = connection.execute(
            """
            SELECT
                cell_reference,
                row_index,
                column_index,
                raw_value,
                display_text,
                formula,
                data_type,
                style_id,
                merged_range
            FROM sheet_cells
            WHERE sheet_id = ?
              AND row_index BETWEEN ? AND ?
              AND column_index BETWEEN ? AND ?
            ORDER BY row_index, column_index
            """,
            (
                sheets[0]["id"],
                start_row,
                end_row,
                start_column,
                end_column,
            ),
        ).fetchall()

    status_summary = _index_status_for_database(database_path)
    return {
        "status": "stale" if status_summary["is_stale"] else "found",
        "source_document": sheets[0]["source_document"],
        "workbook": Path(sheets[0]["source_document"]).name,
        "sheet": sheet,
        "range": normalized_range,
        "cells": [dict(cell) for cell in cells],
        "limitations": [],
        "index_status": status_summary,
    }


def _parse_a1_range(value: str) -> tuple[int, int, int, int, str]:
    match = re.fullmatch(
        r"\$?([A-Z]+)\$?([1-9]\d*)(?::\$?([A-Z]+)\$?([1-9]\d*))?",
        value,
    )
    if match is None:
        raise ValueError(f"Invalid A1 range: {value}")
    start_column = _a1_column_index(match.group(1))
    start_row = int(match.group(2))
    end_column = _a1_column_index(match.group(3) or match.group(1))
    end_row = int(match.group(4) or match.group(2))
    if end_row < start_row or end_column < start_column:
        raise ValueError(f"A1 range must run from top-left to bottom-right: {value}")
    normalized = f"{match.group(1)}{start_row}:{match.group(3) or match.group(1)}{end_row}"
    return start_row, start_column, end_row, end_column, normalized


def _a1_column_index(letters: str) -> int:
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - 64
    return column


@mcp.tool()
def find_feature(name: str) -> dict[str, object]:
    """Resolve only exact canonical names or aliases confirmed in catalog.json."""
    name = name.strip()
    if not name:
        raise ValueError("name must not be empty")

    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        canonical_rows = connection.execute(
            """
            SELECT id, feature_key, canonical_name, source
            FROM catalog_features
            WHERE canonical_name = ?
            ORDER BY id
            """,
            (name,),
        ).fetchall()
        alias_rows = connection.execute(
            """
            SELECT
                f.id,
                f.feature_key,
                f.canonical_name,
                f.source,
                a.alias,
                a.source AS alias_source,
                a.confirmed_at,
                a.confirmed_by
            FROM catalog_aliases AS a
            JOIN catalog_features AS f ON f.id = a.feature_id
            WHERE a.alias = ?
            ORDER BY f.id, a.id
            """,
            (name,),
        ).fetchall()

    matches: dict[int, dict[str, object]] = {}
    for row in alias_rows:
        matches[row["id"]] = {
            "match_type": "confirmed_alias",
            "feature": {
                "key": row["feature_key"],
                "canonical_name": row["canonical_name"],
                "source": row["source"],
            },
            "matched_alias": {
                "name": row["alias"],
                "source": row["alias_source"],
                "confirmed_at": row["confirmed_at"],
                "confirmed_by": row["confirmed_by"],
            },
        }
    for row in canonical_rows:
        matches[row["id"]] = {
            "match_type": "canonical",
            "feature": {
                "key": row["feature_key"],
                "canonical_name": row["canonical_name"],
                "source": row["source"],
            },
            "matched_alias": None,
        }

    if not matches:
        return {
            "status": "not_found",
            "query": name,
            "match_type": None,
            "feature": None,
            "matched_alias": None,
            "limitations": [
                "当前人工确认目录中未找到该正式名称或别名，未进行自动联想。"
            ],
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "query": name,
            "match_type": "exact_catalog_collision",
            "feature": None,
            "matched_alias": None,
            "candidates": [match["feature"] for match in matches.values()],
            "limitations": ["该名称在人工目录中对应多个玩法，需要人工消歧。"],
        }

    match = next(iter(matches.values()))
    return {
        "status": "found",
        "query": name,
        "match_type": match["match_type"],
        "feature": match["feature"],
        "matched_alias": match["matched_alias"],
        "limitations": [],
    }


@mcp.tool()
def get_feature_evidence(
    name: str,
    include_documents: bool = True,
    include_configs: bool = True,
    include_images: bool = True,
) -> dict[str, object]:
    """Collect facts for a feature only after exact catalog resolution."""
    name = name.strip()
    if not name:
        raise ValueError("name must not be empty")
    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        resolved = connection.execute(
            """
            SELECT f.id, f.feature_key, f.canonical_name, f.source, 'canonical' AS match_type
            FROM catalog_features AS f
            WHERE f.canonical_name = ?
            UNION ALL
            SELECT f.id, f.feature_key, f.canonical_name, f.source,
                   'confirmed_alias' AS match_type
            FROM catalog_aliases AS a
            JOIN catalog_features AS f ON f.id = a.feature_id
            WHERE a.alias = ?
            ORDER BY id
            """,
            (name, name),
        ).fetchall()
        unique_features = {row["id"]: row for row in resolved}
        if not unique_features:
            return {
                "status": "not_found",
                "query": name,
                "resolved_name": None,
                "feature": None,
                "documents": [],
                "configs": [],
                "images": [],
                "limitations": [
                    "当前人工确认目录中未找到该正式名称或别名，未进行自动联想。"
                ],
            }
        if len(unique_features) > 1:
            return {
                "status": "ambiguous",
                "query": name,
                "resolved_name": None,
                "feature": None,
                "documents": [],
                "configs": [],
                "images": [],
                "candidates": [row["canonical_name"] for row in unique_features.values()],
                "limitations": ["该名称在人工目录中对应多个玩法，需要人工消歧。"],
            }

        feature_row = next(iter(unique_features.values()))
        canonical_name = feature_row["canonical_name"]
        confirmed_aliases = [
            row[0]
            for row in connection.execute(
                "SELECT alias FROM catalog_aliases WHERE feature_id = ? ORDER BY id",
                (feature_row["id"],),
            ).fetchall()
        ]
        search_terms = list(dict.fromkeys([canonical_name, *confirmed_aliases]))
        document_rows = []
        config_rows = []
        image_rows = []
        if include_documents:
            document_rows = connection.execute(
                """
                SELECT
                    e.id AS evidence_id,
                    d.path AS source_document,
                    e.evidence_type,
                    e.text,
                    e.section_path,
                    e.locator,
                    b.ordinal AS block_ordinal
                FROM evidence AS e
                JOIN documents AS d ON d.id = e.document_id
                LEFT JOIN document_blocks AS b
                  ON e.source_table = 'document_blocks'
                 AND b.id = e.source_record_id
                WHERE d.document_type = 'docx' AND (
                """
                + " OR ".join("instr(e.text, ?) > 0" for _ in search_terms)
                + """
                )
                ORDER BY d.path, b.ordinal, e.id
                """,
                search_terms,
            ).fetchall()
        if include_configs:
            config_rows = connection.execute(
                """
                SELECT
                    d.path AS source_document,
                    ws.sheet_name,
                    c.cell_reference,
                    c.raw_value,
                    c.display_text,
                    c.formula,
                    c.data_type,
                    c.style_id,
                    c.merged_range
                FROM sheet_cells AS c
                JOIN workbook_sheets AS ws ON ws.id = c.sheet_id
                JOIN documents AS d ON d.id = ws.document_id
                WHERE (
                """
                + " OR ".join(
                    "instr(c.display_text, ?) > 0 OR instr(c.raw_value, ?) > 0 "
                    "OR instr(COALESCE(c.formula, ''), ?) > 0"
                    for _ in search_terms
                )
                + """
                )
                ORDER BY d.path, ws.sheet_index, c.row_index, c.column_index
                """,
                [term for term in search_terms for _ in range(3)],
            ).fetchall()
        if include_images:
            image_rows = connection.execute(
                """
                SELECT
                    i.id AS image_id,
                    d.path AS source_document,
                    i.asset_path,
                    i.heading,
                    i.paragraph_index,
                    i.context_text,
                    i.sheet_name,
                    i.cell_anchor,
                    i.ocr_status,
                    i.ocr_text
                FROM images AS i
                JOIN documents AS d ON d.id = i.document_id
                WHERE (
                """
                + " OR ".join(
                    "instr(i.context_text, ?) > 0 OR instr(i.ocr_text, ?) > 0 "
                    "OR instr(COALESCE(i.heading, ''), ?) > 0"
                    for _ in search_terms
                )
                + """
                )
                ORDER BY d.path, i.id
                """,
                [term for term in search_terms for _ in range(3)],
            ).fetchall()

    documents = []
    for row in document_rows:
        item = dict(row)
        item["section_path"] = json.loads(item["section_path"])
        item["locator"] = json.loads(item["locator"])
        documents.append(item)
    configs = [dict(row) for row in config_rows]
    images = []
    for row in image_rows:
        item = dict(row)
        item["asset_path"] = str((database_path.parent / item["asset_path"]).resolve())
        images.append(item)
    status_summary = _index_status_for_database(database_path)
    return {
        "status": "stale" if status_summary["is_stale"] else "found",
        "query": name,
        "resolved_name": canonical_name,
        "searched_names": search_terms,
        "match_type": feature_row["match_type"],
        "feature": {
            "key": feature_row["feature_key"],
            "canonical_name": canonical_name,
            "source": feature_row["source"],
        },
        "documents": documents,
        "configs": configs,
        "images": images,
        "limitations": [],
        "index_status": status_summary,
    }


@mcp.tool()
def get_image_context(image_id: int) -> dict[str, object]:
    """Return traceable source evidence, nearby text, and OCR for an indexed image."""
    database_path = _database_path()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                d.path AS source_document,
                d.document_type,
                i.asset_path,
                i.heading,
                i.paragraph_index,
                i.context_text,
                i.sheet_name,
                i.cell_anchor,
                i.ocr_status,
                i.ocr_text
            FROM images AS i
            JOIN documents AS d ON d.id = i.document_id
            WHERE i.id = ?
            """,
            (image_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Image {image_id} was not found in the index")
    result = dict(row)
    result["asset_path"] = str((database_path.parent / str(result["asset_path"])).resolve())
    return result


def _database_path() -> Path:
    configured = os.environ.get("GAME_DESIGN_INDEX_DIR")
    if not configured:
        raise RuntimeError("GAME_DESIGN_INDEX_DIR is not configured")
    database_path = Path(configured).expanduser().resolve() / "knowledge.sqlite"
    if not database_path.is_file():
        raise FileNotFoundError(f"Knowledge index does not exist: {database_path}")
    return database_path


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
