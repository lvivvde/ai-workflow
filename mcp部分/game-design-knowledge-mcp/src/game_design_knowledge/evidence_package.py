"""Versioned evidence packages: one retrieval unit with every layer kept apart.

A retrieval unit is anchored by the row that produced it -- ``evidence:12`` for
a quoted block, ``image:3`` for an indexed image. The package reports the source
revision that row belongs to, the locator a person can check, the transcription,
the visual interpretation, the notation, the statement itself, an explanation
slot, the uncertainties, and the provenance that ties them together.

Two rules shape every payload here:

* Nothing is inferred. A layer this build cannot serve is reported as
  unavailable with a reason code, never as an empty list that would read like
  "the picture has nothing in it".
* A layer never becomes evidence. A transcription stays a suggestion, a
  confirmed step records geometry only, and the statement keeps the authority
  the index gave it.

``transcription``, ``visual_interpretation`` and ``notation`` are the same
payloads ``get_image_context`` serves, projected into one item per region,
element, and relation, so a derived item can always be resolved back to the
source revision *and* the original region it came from.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .flow_notation import CLAIM_BOUNDARY
from .ocr_normalization import extract_critical_tokens


SOURCE_REFERENCE_VERSION = "source-reference-v1"
DISPLAY_LOCATOR_VERSION = "display-locator-v1"
ASSET_REFERENCE_VERSION = "asset-reference-v1"
EVIDENCE_PACKAGE_VERSION = "evidence-package-v1"

UNIT_EVIDENCE_PREFIX = "evidence:"
UNIT_IMAGE_PREFIX = "image:"

ASSET_REFERENCE_PREFIX = "asset-"
ASSET_REFERENCE_LENGTH = 32
MAX_INLINE_ASSET_BYTES = 4 * 1024 * 1024
MAX_PAGE_SIZE = 200
MAX_PACKAGE_UNITS = 50

#: The layers a package separates. ``provenance`` and the ``unavailable`` list
#: describe the package itself, so they are returned whole on every page.
PACKAGE_SECTIONS: tuple[str, ...] = (
    "source",
    "statement",
    "transcription",
    "visual_interpretation",
    "notation",
    "explanation",
    "uncertainties",
)

EVIDENCE_PACKAGE_BOUNDARY = (
    "One package reports what the index holds for one retrieval unit, layer by "
    "layer. It is not an answer: the statement keeps its authority, the "
    "transcription stays a suggestion, and every derived layer names the rule "
    "that produced it."
)

SOURCE_REFERENCE_BOUNDARY = (
    "A source reference names the bytes and the parse revision a fact came "
    "from; it does not promise that the file is still on disk."
)

ASSET_REFERENCE_BOUNDARY = (
    "An asset reference is a token this index issued for an asset it holds. It "
    "is not a path and it cannot be exchanged for one."
)

#: Reason codes for a layer this build cannot serve for one unit.
UNAVAILABLE_UNIT_NOT_FOUND = "unit_not_found"
UNAVAILABLE_NOT_AN_IMAGE = "unit_is_not_an_image"
UNAVAILABLE_NOT_A_STATEMENT = "unit_is_not_a_statement"
UNAVAILABLE_NO_REGIONS = "no_region_transcription"
UNAVAILABLE_NO_LAYOUT = "no_layout_analysis"
UNAVAILABLE_NO_RELATIONS = "no_structural_relation"
UNAVAILABLE_NO_EXPLANATION = "no_explanation_profile"

#: Uncertainty codes a package reports about its own layers.
UNCERTAINTY_LOW_QUALITY_OCR = "low_quality_transcription"
UNCERTAINTY_MISSING_GEOMETRY = "missing_geometry"
UNCERTAINTY_CANDIDATE_RELATION = "candidate_relation"
UNCERTAINTY_LAYER_UNAVAILABLE = "layer_unavailable"


def parse_unit_id(value: str) -> tuple[str, int]:
    """Split ``evidence:12`` / ``image:3`` into a kind and a row id.

    A retrieval unit is never a path, a table name, or a SQL fragment: anything
    that is not one of the two supported prefixes is rejected outright.
    """

    text = str(value or "").strip()
    for prefix, kind in (
        (UNIT_EVIDENCE_PREFIX, "evidence"),
        (UNIT_IMAGE_PREFIX, "image"),
    ):
        if text.startswith(prefix):
            record = text[len(prefix) :]
            if record.isdigit() and int(record) > 0:
                return kind, int(record)
            raise ValueError(f"{value!r} is not a retrieval unit: expected {prefix}<id>")
    raise ValueError(
        "unit_id must look like 'evidence:<id>' or 'image:<id>', never a path "
        f"or a row selector: {value!r}"
    )


def source_reference(
    *,
    logical_document_id: str | None,
    source_revision_id: str | None,
    parse_revision_id: str | None,
    path: str,
    document_type: str,
    source_sha256: str = "",
    locator: Mapping[str, Any] | None = None,
    section_path: Sequence[Any] | None = None,
    authority: str = "",
    region: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The revision-aware reference every V2 derived item resolves to."""

    return {
        "schema_version": SOURCE_REFERENCE_VERSION,
        "logical_document_id": logical_document_id or "",
        "source_revision_id": source_revision_id or "",
        "parse_revision_id": parse_revision_id or "",
        "path": str(path),
        "document_type": str(document_type),
        "source_sha256": str(source_sha256),
        "authority": str(authority),
        "section_path": [str(part) for part in (section_path or ())],
        "locator": dict(locator or {}),
        "region": dict(region) if region is not None else None,
        "boundary": SOURCE_REFERENCE_BOUNDARY,
    }


def display_locator(
    document_type: str,
    locator: Mapping[str, Any] | None,
    *,
    section_path: Sequence[Any] | None = None,
    asset_name: str = "",
) -> dict[str, Any]:
    """A locator a person can check by hand, next to the machine fields."""

    fields = dict(locator or {})
    kind, label = _locator_label(str(document_type), fields, asset_name)
    return {
        "schema_version": DISPLAY_LOCATOR_VERSION,
        "kind": kind,
        "label": label,
        "section_path": [str(part) for part in (section_path or ())],
        "fields": fields,
        "asset": asset_name or None,
    }


def asset_reference(index_directory: Path, relative_asset: str) -> str:
    """The token this index issues for one asset it holds.

    The token is derived from the index the asset belongs to plus the asset's
    path *inside* that index, so it is stable across rebuilds but cannot be
    minted for a file somewhere else on disk.
    """

    payload = (
        f"{Path(index_directory).resolve().name}\n"
        f"{Path(str(relative_asset)).as_posix()}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{ASSET_REFERENCE_PREFIX}{digest[:ASSET_REFERENCE_LENGTH]}"


def looks_like_asset_reference(value: str) -> bool:
    candidate = str(value or "").strip().lower()
    if not candidate.startswith(ASSET_REFERENCE_PREFIX):
        return False
    digest = candidate[len(ASSET_REFERENCE_PREFIX) :]
    if len(digest) != ASSET_REFERENCE_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in digest)


def authorized_assets(index: Any, index_directory: Path) -> dict[str, dict[str, Any]]:
    """Every asset this index may hand out, keyed by the reference it issued."""

    try:
        rows = index.fetchall(
            """
            SELECT id, document_id, asset_path, sha256, mime_type
            FROM images
            ORDER BY id
            """
        )
    except sqlite3.OperationalError:
        return {}
    issued: dict[str, dict[str, Any]] = {}
    for row in rows:
        relative = str(row["asset_path"])
        reference = asset_reference(index_directory, relative)
        issued[reference] = {
            "schema_version": ASSET_REFERENCE_VERSION,
            "asset_reference": reference,
            "image_id": int(row["id"]),
            "document_id": int(row["document_id"]),
            "relative_path": relative,
            "path": str((Path(index_directory) / relative).resolve()),
            "sha256": str(row["sha256"]),
            "mime_type": str(row["mime_type"]),
            "boundary": ASSET_REFERENCE_BOUNDARY,
        }
    return issued


def resolve_asset_reference(
    index: Any, index_directory: Path, reference: str
) -> dict[str, Any] | None:
    """Resolve one issued reference, or ``None`` -- never a caller's path."""

    if not looks_like_asset_reference(reference):
        return None
    return authorized_assets(index, index_directory).get(str(reference).strip().lower())


def asset_content(
    asset: Mapping[str, Any], *, limit: int = MAX_INLINE_ASSET_BYTES
) -> dict[str, Any]:
    """Inline one authorized asset, refusing anything above the size limit."""

    path = Path(str(asset["path"]))
    try:
        data = path.read_bytes()
    except OSError as error:
        return {"included": False, "size": 0, "detail": str(error)}
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > limit:
        return {
            "included": False,
            "size": len(data),
            "sha256": digest,
            "detail": f"asset is larger than the {limit}-byte inline limit",
        }
    return {
        "included": True,
        "size": len(data),
        "sha256": digest,
        # A reader can tell a rewritten asset from the one that was indexed.
        "sha256_matches_index": digest == str(asset.get("sha256") or ""),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def image_transcription(index: Any, image_id: int) -> dict[str, Any] | None:
    """Region-level OCR for one image, or ``None`` on an index without it.

    Returned whole -- raw text, the suggestion proposed for each region, and the
    three confidences separately -- because a caller needs to see *why* a
    reading is uncertain, and because an OCR transcription never becomes source
    evidence on its own.
    """

    try:
        run = index.fetchone(
            """
            SELECT id, requested_engine, engine, engine_version, tier,
                   fallback_used, execution_status, quality_status, reason_code,
                   detail, evidence_state, language, reading_order_source,
                   region_count, reason_chain, duration_ms
            FROM ocr_runs
            WHERE image_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (image_id,),
        )
    except sqlite3.OperationalError:
        return None
    if run is None:
        return None
    regions = index.fetchall(
        """
        SELECT r.region_index, r.reading_order, r.bbox, r.text_raw,
               r.text_confidence, r.region_confidence, r.key_mark_confidence,
               r.language, n.normalized_text, n.changes, n.ruleset_version
        FROM ocr_regions AS r
        LEFT JOIN ocr_normalizations AS n ON n.region_id = r.id
        WHERE r.run_id = ?
        ORDER BY r.reading_order
        """,
        (int(run["id"]),),
    )
    return {
        "run": {
            "requested_engine": run["requested_engine"],
            "engine": run["engine"],
            "engine_version": run["engine_version"],
            "tier": run["tier"],
            "fallback_used": bool(run["fallback_used"]),
            "execution_status": run["execution_status"],
            "quality_status": run["quality_status"],
            "reason_code": run["reason_code"],
            "detail": run["detail"],
            "evidence_state": run["evidence_state"],
            "language": run["language"],
            "reading_order_source": run["reading_order_source"],
            "duration_ms": run["duration_ms"],
            "reason_chain": _json_value(run["reason_chain"], []),
        },
        "regions": [
            {
                "region_index": region["region_index"],
                "reading_order": region["reading_order"],
                "bbox": _json_value(region["bbox"], None),
                "text": region["text_raw"],
                "text_confidence": region["text_confidence"],
                "region_confidence": region["region_confidence"],
                "key_mark_confidence": region["key_mark_confidence"],
                "language": region["language"],
                "critical_tokens": [
                    token.as_payload()
                    for token in extract_critical_tokens(region["text_raw"])
                ],
                "normalization_suggestion": (
                    {
                        "suggestion_only": True,
                        "ruleset_version": region["ruleset_version"],
                        "normalized": region["normalized_text"],
                        "changes": _json_value(region["changes"], []),
                    }
                    if region["normalized_text"] is not None
                    else None
                ),
            }
            for region in regions
        ],
        "evidence_boundary": (
            "OCR output stays a transcription; it is not source evidence and is "
            "only upgraded by a human confirmation or a source text layer."
        ),
    }


def image_layout(index: Any, image_id: int) -> dict[str, Any] | None:
    """Reading order and structural relations for one image, or ``None``.

    Returned whole for the same reason the regions are: a caller has to see the
    geometry basis, the rule version, and *two* confidences -- the layout's and
    the transcription's -- to judge a relation. Every relation also carries the
    claim boundary, because "an arrow points down to this block" is a statement
    about the picture and nothing more.
    """

    try:
        run = index.fetchone(
            """
            SELECT id, ruleset_version, order_source, column_count, element_count,
                   geometry_confidence, uncertainty, detail
            FROM layout_runs
            WHERE image_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (image_id,),
        )
    except sqlite3.OperationalError:
        return None
    if run is None:
        return None
    elements = index.fetchall(
        """
        SELECT region_index, kind, direction, reading_order, row_index,
               column_index, depth_hint, bbox, text_confidence
        FROM layout_elements
        WHERE run_id = ?
        ORDER BY reading_order
        """,
        (int(run["id"]),),
    )
    relations = index.fetchall(
        """
        SELECT kind, status, source_region, target_region, via_regions, direction,
               geometry_basis, detail, uncertainty, geometry_confidence,
               ocr_confidence, rule_version
        FROM structural_relations
        WHERE run_id = ?
        ORDER BY id
        """,
        (int(run["id"]),),
    )
    return {
        "run": {
            "ruleset_version": run["ruleset_version"],
            "order_source": run["order_source"],
            "column_count": run["column_count"],
            "element_count": run["element_count"],
            "geometry_confidence": run["geometry_confidence"],
            "uncertainty": run["uncertainty"],
            "detail": run["detail"],
        },
        "elements": [
            {
                "region_index": element["region_index"],
                "kind": element["kind"],
                "direction": element["direction"],
                "reading_order": element["reading_order"],
                "row": element["row_index"],
                "column": element["column_index"],
                "depth_hint": element["depth_hint"],
                "bbox": _json_value(element["bbox"], None),
                "text_confidence": element["text_confidence"],
            }
            for element in elements
        ],
        "relations": [
            {
                "kind": relation["kind"],
                "status": relation["status"],
                "source_region": relation["source_region"],
                "target_region": relation["target_region"],
                "via_regions": _json_value(relation["via_regions"], []),
                "direction": relation["direction"],
                "geometry_basis": relation["geometry_basis"],
                "detail": relation["detail"],
                "uncertainty": relation["uncertainty"],
                "geometry_confidence": relation["geometry_confidence"],
                "ocr_confidence": relation["ocr_confidence"],
                "rule_version": relation["rule_version"],
                "claim_boundary": CLAIM_BOUNDARY,
            }
            for relation in relations
        ],
        "evidence_boundary": (
            "Indentation is recorded as a depth hint and never becomes a parent "
            "or next relation; a confirmed step records visible geometry only."
        ),
    }


def evidence_package(
    index: Any,
    *,
    unit_id: str,
    sections: Sequence[str] | None = None,
    limit: int = 20,
    cursor: str = "",
) -> dict[str, Any]:
    """Build one package for one retrieval unit, paged over its own items."""

    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    offset = _cursor_offset(cursor)
    requested = requested_sections(sections)
    kind, record_id = parse_unit_id(unit_id)
    index_directory = Path(index.database_path).parent

    if kind == "evidence":
        built = _evidence_unit(index, record_id, index_directory)
    else:
        built = _image_unit(index, record_id, index_directory)

    if built is None:
        return {
            "status": "not_found",
            "schema_version": EVIDENCE_PACKAGE_VERSION,
            "unit_id": unit_id,
            "retrieval_unit": {
                "unit_id": unit_id,
                "unit_type": kind,
                "resolved": False,
            },
            "source_reference": None,
            "display_locator": None,
            "asset_reference": None,
            "sections": {name: [] for name in requested},
            "unavailable": [
                _unavailable(
                    name,
                    UNAVAILABLE_UNIT_NOT_FOUND,
                    f"{unit_id} is not in this index, so this layer has nothing "
                    "to report.",
                )
                for name in requested
            ],
            "provenance": None,
            "page": _page_payload(limit, offset, total=0, returned=0, totals={}),
            "boundary": EVIDENCE_PACKAGE_BOUNDARY,
        }

    page, totals, total = _page_sections(built["sections"], requested, limit, offset)
    return {
        "status": "found",
        "schema_version": EVIDENCE_PACKAGE_VERSION,
        "unit_id": unit_id,
        "retrieval_unit": built["retrieval_unit"],
        "source_reference": built["source_reference"],
        "display_locator": built["display_locator"],
        "asset_reference": built["asset_reference"],
        "sections": page,
        "unavailable": [
            entry for entry in built["unavailable"] if entry["section"] in requested
        ],
        "provenance": built["provenance"],
        "page": _page_payload(limit, offset, total=total, returned=sum(
            len(items) for items in page.values()
        ), totals=totals),
        "boundary": EVIDENCE_PACKAGE_BOUNDARY,
    }


def hydrated_hit(
    *,
    unit_id: str,
    unit_type: str,
    reference: Mapping[str, Any],
    display: Mapping[str, Any],
    authority: str = "",
    asset_reference_value: str = "",
) -> dict[str, Any]:
    """The compact V2 form of one search hit.

    It carries exactly what a caller needs to ask for the full package by
    reference: the unit it anchors on, the revision-aware source reference, and
    the locator. Layer contents stay behind the package tool, so a search result
    can never be mistaken for the whole evidence.
    """

    hit = {
        "schema_version": EVIDENCE_PACKAGE_VERSION,
        "unit_id": unit_id,
        "unit_type": unit_type,
        "authority": authority,
        "source_reference": dict(reference),
        "display_locator": dict(display),
        "section_names": list(PACKAGE_SECTIONS),
        "expand": {
            "tool": "get_evidence_package",
            "arguments": {"unit_id": unit_id},
        },
        "boundary": EVIDENCE_PACKAGE_BOUNDARY,
    }
    if asset_reference_value:
        hit["asset_reference"] = asset_reference_value
        hit["asset_boundary"] = ASSET_REFERENCE_BOUNDARY
    return hit


def evidence_packages(
    index: Any,
    *,
    unit_ids: Sequence[str],
    sections: Sequence[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Several packages in one call, keeping every one that resolved.

    A unit that cannot be built never discards the ones that could: the partial
    response names what was skipped and why.
    """

    units = [str(unit) for unit in unit_ids]
    if not units:
        raise ValueError("unit_ids must contain at least one retrieval unit")
    if len(units) > MAX_PACKAGE_UNITS:
        raise ValueError(
            f"unit_ids cannot contain more than {MAX_PACKAGE_UNITS} units in one call"
        )

    packages: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for unit_id in units:
        try:
            package = evidence_package(
                index, unit_id=unit_id, sections=sections, limit=limit
            )
        except ValueError as error:
            unresolved.append(
                {"unit_id": unit_id, "status": "invalid", "detail": str(error)}
            )
            continue
        if package["status"] == "not_found":
            unresolved.append(
                {
                    "unit_id": unit_id,
                    "status": "not_found",
                    "detail": f"{unit_id} is not in this index",
                }
            )
            continue
        packages.append(package)

    if not unresolved:
        status = "found"
    elif packages:
        status = "partial"
    else:
        status = "not_found"
    return {
        "status": status,
        "schema_version": EVIDENCE_PACKAGE_VERSION,
        "requested": len(units),
        "resolved": len(packages),
        "packages": packages,
        "unresolved": unresolved,
        "boundary": EVIDENCE_PACKAGE_BOUNDARY,
    }


def requested_sections(sections: Sequence[str] | None) -> tuple[str, ...]:
    """Validate the section filter, refusing names this build cannot serve."""

    if sections is None:
        return PACKAGE_SECTIONS
    requested = tuple(dict.fromkeys(str(section) for section in sections))
    unknown = sorted(set(requested) - set(PACKAGE_SECTIONS))
    if unknown:
        raise ValueError(
            f"unknown evidence-package sections: {unknown}; "
            f"supported sections are {list(PACKAGE_SECTIONS)}"
        )
    if not requested:
        raise ValueError("sections must name at least one section")
    return requested


def _evidence_unit(
    index: Any, evidence_id: int, index_directory: Path
) -> dict[str, Any] | None:
    row = index.fetchone(
        """
        SELECT e.id AS evidence_id, e.evidence_type, e.source_table,
               e.source_record_id, e.text, e.section_path, e.locator, e.authority,
               d.path, d.document_type, d.source_sha256, d.logical_document_id,
               d.source_revision_id, d.parse_revision_id
        FROM evidence AS e
        JOIN documents AS d ON d.id = e.document_id
        WHERE e.id = ?
        """,
        (evidence_id,),
    )
    if row is None:
        return None

    section_path = _json_value(row["section_path"], [])
    locator = _json_value(row["locator"], {})
    reference = source_reference(
        logical_document_id=row["logical_document_id"],
        source_revision_id=row["source_revision_id"],
        parse_revision_id=row["parse_revision_id"],
        path=row["path"],
        document_type=row["document_type"],
        source_sha256=row["source_sha256"],
        locator=locator,
        section_path=section_path,
        authority=row["authority"],
        region=_region_of(locator),
    )
    display = display_locator(row["document_type"], locator, section_path=section_path)
    unit = {
        "unit_id": f"{UNIT_EVIDENCE_PREFIX}{evidence_id}",
        "unit_type": str(row["evidence_type"]),
        "resolved": True,
        "authority": str(row["authority"]),
        "source_table": str(row["source_table"]),
    }

    unavailable = [
        _unavailable(
            section,
            UNAVAILABLE_NOT_AN_IMAGE,
            "This unit is a quoted document block, so it has a statement and a "
            "source reference but no image layer.",
        )
        for section in ("transcription", "visual_interpretation", "notation")
    ]
    sections: dict[str, list[dict[str, Any]]] = {
        "source": [
            {
                "kind": "source",
                "source_reference": reference,
                "display_locator": display,
                "boundary": SOURCE_REFERENCE_BOUNDARY,
            }
        ],
        "statement": [
            {
                "kind": "statement",
                "text": str(row["text"]),
                "evidence_type": str(row["evidence_type"]),
                "authority": str(row["authority"]),
                "verbatim": True,
                "source_reference": reference,
            }
        ],
        "transcription": [],
        "visual_interpretation": [],
        "notation": [],
        "explanation": [],
        "uncertainties": [
            _uncertainty(
                "explanation",
                UNAVAILABLE_NO_EXPLANATION,
                "No explanation profile is served by this build.",
            )
        ],
    }
    unavailable.append(
        _unavailable(
            "explanation",
            UNAVAILABLE_NO_EXPLANATION,
            "No explanation profile is served by this build.",
        )
    )
    return {
        "retrieval_unit": unit,
        "source_reference": reference,
        "display_locator": display,
        "asset_reference": None,
        "sections": sections,
        "unavailable": unavailable,
        "provenance": _provenance(index, row, index_directory),
    }


def _image_unit(
    index: Any, image_id: int, index_directory: Path
) -> dict[str, Any] | None:
    row = index.fetchone(
        """
        SELECT i.id AS image_id, i.relationship_id, i.source_part, i.sha256,
               i.mime_type, i.asset_path, i.heading, i.paragraph_index,
               i.sheet_name, i.cell_anchor, i.ocr_status,
               d.path, d.document_type, d.source_sha256, d.logical_document_id,
               d.source_revision_id, d.parse_revision_id
        FROM images AS i
        JOIN documents AS d ON d.id = i.document_id
        WHERE i.id = ?
        """,
        (image_id,),
    )
    if row is None:
        return None

    locator = _image_locator(row)
    reference = source_reference(
        logical_document_id=row["logical_document_id"],
        source_revision_id=row["source_revision_id"],
        parse_revision_id=row["parse_revision_id"],
        path=row["path"],
        document_type=row["document_type"],
        source_sha256=row["source_sha256"],
        locator=locator,
        section_path=[],
        authority="source-image",
        region={"source_part": str(row["source_part"])},
    )
    display = display_locator(
        row["document_type"],
        locator,
        asset_name=Path(str(row["asset_path"])).name,
    )
    asset_reference_value = asset_reference(index_directory, str(row["asset_path"]))
    unit = {
        "unit_id": f"{UNIT_IMAGE_PREFIX}{image_id}",
        "unit_type": "image",
        "resolved": True,
        "authority": "source-image",
        "source_part": str(row["source_part"]),
        "relationship_id": str(row["relationship_id"]),
        "mime_type": str(row["mime_type"]),
        "sha256": str(row["sha256"]),
        "ocr_status": str(row["ocr_status"]),
        "asset_reference": asset_reference_value,
    }

    sections: dict[str, list[dict[str, Any]]] = {
        "source": [
            {
                "kind": "source",
                "source_reference": reference,
                "display_locator": display,
                "asset_reference": asset_reference_value,
                "asset_boundary": ASSET_REFERENCE_BOUNDARY,
                "boundary": SOURCE_REFERENCE_BOUNDARY,
            }
        ],
        "statement": [],
        "transcription": [],
        "visual_interpretation": [],
        "notation": [],
        "explanation": [],
        "uncertainties": [],
    }
    unavailable = [
        _unavailable(
            "statement",
            UNAVAILABLE_NOT_A_STATEMENT,
            "An image is a source, not a statement: quote it through the "
            "evidence rows that cite it.",
        ),
        _unavailable(
            "explanation",
            UNAVAILABLE_NO_EXPLANATION,
            "No explanation profile is served by this build.",
        ),
    ]
    sections["uncertainties"].append(
        _uncertainty(
            "explanation",
            UNAVAILABLE_NO_EXPLANATION,
            "No explanation profile is served by this build.",
        )
    )

    transcription = image_transcription(index, image_id)
    if transcription is None:
        unavailable.append(
            _unavailable(
                "transcription",
                UNAVAILABLE_NO_REGIONS,
                "This index holds no OCR run for the image, so there is nothing "
                "to transcribe.",
            )
        )
        sections["uncertainties"].append(
            _uncertainty(
                "transcription",
                UNAVAILABLE_NO_REGIONS,
                "This index holds no OCR run for the image.",
            )
        )
    else:
        run = transcription["run"]
        sections["transcription"].append(
            {
                "kind": "ocr_run",
                "source_reference": reference,
                "run": run,
                "evidence_state": run["evidence_state"],
                "boundary": transcription["evidence_boundary"],
            }
        )
        for region in transcription["regions"]:
            sections["transcription"].append(
                {
                    "kind": "ocr_region",
                    "region": _region_payload(region),
                    "source_reference": _with_region(reference, region),
                    "boundary": transcription["evidence_boundary"],
                }
            )
        if str(run["quality_status"]) not in {"accepted", "not_evaluated"}:
            sections["uncertainties"].append(
                _uncertainty(
                    "transcription",
                    UNCERTAINTY_LOW_QUALITY_OCR,
                    f"OCR quality is {run['quality_status']} "
                    f"({run['reason_code'] or 'no reason code'}): {run['detail']}",
                    geometry=False,
                )
            )
        if run["evidence_state"] != "machine-supported":
            sections["uncertainties"].append(
                _uncertainty(
                    "transcription",
                    UNCERTAINTY_LOW_QUALITY_OCR,
                    "The transcription is "
                    f"{run['evidence_state'] or 'unconfirmed'}, so it stays a "
                    "suggestion until a person or a source text layer confirms it.",
                    geometry=False,
                )
            )

    layout = image_layout(index, image_id)
    if layout is None:
        unavailable.append(
            _unavailable(
                "visual_interpretation",
                UNAVAILABLE_NO_LAYOUT,
                "This index holds no layout analysis for the image.",
            )
        )
        unavailable.append(
            _unavailable(
                "notation",
                UNAVAILABLE_NO_RELATIONS,
                "Without layout analysis there is no geometry to relate.",
            )
        )
        for section in ("visual_interpretation", "notation"):
            sections["uncertainties"].append(
                _uncertainty(
                    section,
                    UNAVAILABLE_NO_LAYOUT
                    if section == "visual_interpretation"
                    else UNAVAILABLE_NO_RELATIONS,
                    "This index holds no layout analysis for the image.",
                )
            )
    else:
        run = layout["run"]
        sections["visual_interpretation"].append(
            {
                "kind": "layout_run",
                "source_reference": reference,
                "run": run,
                "boundary": layout["evidence_boundary"],
            }
        )
        for element in layout["elements"]:
            sections["visual_interpretation"].append(
                {
                    "kind": "layout_element",
                    "element": element,
                    "source_reference": _with_region(reference, element),
                    "boundary": layout["evidence_boundary"],
                }
            )
        for relation in layout["relations"]:
            sections["notation"].append(
                {
                    "kind": "structural_relation",
                    "relation": relation,
                    "source_reference": _with_region(
                        reference,
                        {"region_index": relation["source_region"]},
                    ),
                    "claim_boundary": relation["claim_boundary"],
                }
            )
        sections["uncertainties"].extend(_layout_uncertainties(run, layout))
        if not layout["relations"]:
            unavailable.append(
                _unavailable(
                    "notation",
                    UNAVAILABLE_NO_RELATIONS,
                    "The layout analysis found no structural relation to report.",
                )
            )

    return {
        "retrieval_unit": unit,
        "source_reference": reference,
        "display_locator": display,
        "asset_reference": asset_reference_value,
        "sections": sections,
        "unavailable": unavailable,
        "provenance": _provenance(index, row, index_directory, image_id=image_id),
    }


def _layout_uncertainties(
    run: Mapping[str, Any], layout: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Geometry uncertainty, reported apart from the transcription's."""

    entries: list[dict[str, Any]] = []
    if str(run["uncertainty"]):
        entries.append(
            _uncertainty(
                "visual_interpretation",
                str(run["uncertainty"]),
                str(run["detail"]),
            )
        )
    blocks_without_box = sum(
        1 for element in layout["elements"] if element["bbox"] is None
    )
    if blocks_without_box:
        entries.append(
            _uncertainty(
                "visual_interpretation",
                UNCERTAINTY_MISSING_GEOMETRY,
                f"{blocks_without_box} region(s) reported no bounding box about "
                "the reading order; their place is the engine's own region order.",
            )
        )
    candidates = [
        relation
        for relation in layout["relations"]
        if relation["status"] != "confirmed"
    ]
    if candidates:
        codes = sorted({str(relation["uncertainty"]) for relation in candidates})
        entries.append(
            _uncertainty(
                "notation",
                UNCERTAINTY_CANDIDATE_RELATION,
                f"{len(candidates)} relation(s) stay candidates "
                f"({', '.join(code for code in codes if code) or 'unstated'}); "
                "each one carries its own reason and basis.",
            )
        )
    return entries


def _provenance(
    index: Any,
    row: Mapping[str, Any],
    index_directory: Path,
    *,
    image_id: int | None = None,
) -> dict[str, Any]:
    """Where this package came from: bytes, parse revision, and manifest."""

    fingerprint = ""
    run_id = ""
    build_id = ""
    try:
        manifest = index.fetchone(
            """
            SELECT build_id, run_id, created_at, configured_fingerprint, profile
            FROM processing_manifests
            ORDER BY created_at DESC, build_id DESC
            LIMIT 1
            """
        )
    except sqlite3.OperationalError:
        manifest = None
    if manifest is not None:
        fingerprint = str(manifest["configured_fingerprint"] or "")
        run_id = str(manifest["run_id"] or "")
        build_id = str(manifest["build_id"] or "")

    rulesets: dict[str, str] = {}
    engine = ""
    if image_id is not None:
        try:
            ocr_run = index.fetchone(
                """
                SELECT engine, engine_version, ruleset_version FROM ocr_runs
                WHERE image_id = ? ORDER BY id DESC LIMIT 1
                """,
                (image_id,),
            )
        except sqlite3.OperationalError:
            ocr_run = None
        if ocr_run is not None:
            engine = " ".join(
                part
                for part in (
                    str(ocr_run["engine"] or ""),
                    str(ocr_run["engine_version"] or ""),
                )
                if part
            )
        try:
            layout_run = index.fetchone(
                """
                SELECT ruleset_version FROM layout_runs
                WHERE image_id = ? ORDER BY id DESC LIMIT 1
                """,
                (image_id,),
            )
            relation = index.fetchone(
                """
                SELECT rule_version FROM structural_relations
                WHERE image_id = ? ORDER BY id DESC LIMIT 1
                """,
                (image_id,),
            )
        except sqlite3.OperationalError:
            layout_run = None
            relation = None
        if layout_run is not None:
            rulesets["layout"] = str(layout_run["ruleset_version"])
        if relation is not None:
            rulesets["structural_relation"] = str(relation["rule_version"])
    try:
        schema_version = int(
            index.fetchone("PRAGMA user_version")[0]
        )
    except (sqlite3.OperationalError, TypeError):
        schema_version = 0

    return {
        "schema_version": EVIDENCE_PACKAGE_VERSION,
        "index_schema_version": schema_version,
        "index_directory": str(Path(index_directory)),
        "build_id": build_id,
        "run_id": run_id,
        "processing_fingerprint": fingerprint,
        "source_sha256": str(row["source_sha256"]),
        "logical_document_id": str(row["logical_document_id"] or ""),
        "source_revision_id": str(row["source_revision_id"] or ""),
        "parse_revision_id": str(row["parse_revision_id"] or ""),
        "ruleset_versions": rulesets,
        "ocr_engine": engine,
    }


def _region_payload(region: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "region_index": region["region_index"],
        "reading_order": region["reading_order"],
        "bbox": region["bbox"],
        "text": region["text"],
        "text_confidence": region["text_confidence"],
        "region_confidence": region["region_confidence"],
        "key_mark_confidence": region["key_mark_confidence"],
        "language": region["language"],
        "critical_tokens": region["critical_tokens"],
        "normalization_suggestion": region["normalization_suggestion"],
    }


def _with_region(
    reference: Mapping[str, Any], region: Mapping[str, Any]
) -> dict[str, Any]:
    """The same reference, narrowed to the region a derived item came from."""

    narrowed = dict(reference)
    region_index = region.get("region_index")
    payload: dict[str, Any] = {"region_index": region_index}
    if region.get("bbox") is not None:
        payload["bbox"] = region["bbox"]
    if region.get("source_part") is not None:
        payload["source_part"] = region["source_part"]
    narrowed["region"] = payload
    locator = dict(narrowed.get("locator") or {})
    if region_index is not None:
        locator["region_index"] = region_index
    narrowed["locator"] = locator
    return narrowed


def _region_of(locator: Mapping[str, Any]) -> dict[str, Any] | None:
    if "region_index" in locator:
        return {"region_index": locator["region_index"]}
    return None


def _image_locator(row: Mapping[str, Any]) -> dict[str, Any]:
    locator: dict[str, Any] = {"relationship_id": str(row["relationship_id"])}
    if row["sheet_name"]:
        locator["sheet_name"] = str(row["sheet_name"])
    if row["cell_anchor"]:
        locator["cell_anchor"] = str(row["cell_anchor"])
    if row["heading"]:
        locator["heading"] = str(row["heading"])
    if row["paragraph_index"] is not None:
        locator["paragraph_index"] = int(row["paragraph_index"])
    if not row["sheet_name"] and not row["heading"]:
        locator["source_part"] = str(row["source_part"])
    return locator


def _locator_label(
    document_type: str, fields: Mapping[str, Any], asset_name: str
) -> tuple[str, str]:
    cell = fields.get("cell_reference") or fields.get("cell_anchor")
    if cell:
        sheet = str(fields.get("sheet_name") or "")
        return "xlsx_cell", f"{sheet}!{cell}" if sheet else str(cell)
    if "paragraph_index" in fields:
        return "docx_paragraph", f"paragraph {fields['paragraph_index']}"
    if "heading" in fields and fields["heading"]:
        return "docx_image", f"image under “{fields['heading']}”"
    if asset_name:
        return "image_asset", f"image {asset_name}"
    return "document", f"{document_type or 'document'} {fields.get('source_part', '')}".strip()


def _unavailable(section: str, code: str, detail: str) -> dict[str, Any]:
    return {
        "section": section,
        "code": code,
        "detail": detail,
        "is_absence": True,
    }


def _uncertainty(
    section: str,
    code: str,
    detail: str,
    *,
    geometry: bool | None = None,
) -> dict[str, Any]:
    # An uncertainty is about a layer, not a fact read out of the source, so it
    # carries the layer it concerns instead of a source reference.
    entry: dict[str, Any] = {
        "kind": "uncertainty",
        "section": section,
        "code": code,
        "detail": detail,
    }
    if geometry is not None:
        # Geometry and OCR uncertainty are separate facts, so a reader never has
        # to guess which one a number came from.
        entry["geometry"] = geometry
    return entry


def _page_sections(
    sections: Mapping[str, Sequence[dict[str, Any]]],
    requested: Sequence[str],
    limit: int,
    offset: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], int]:
    """Page over one flat item list in section order.

    Every item carries its own source reference, so a page boundary can never
    separate a statement from the source it came from.
    """

    ordered: list[tuple[str, dict[str, Any]]] = []
    totals: dict[str, int] = {}
    for name in PACKAGE_SECTIONS:
        if name not in requested:
            continue
        items = list(sections.get(name, ()))
        totals[name] = len(items)
        ordered.extend((name, item) for item in items)

    window = ordered[offset : offset + limit]
    page: dict[str, list[dict[str, Any]]] = {name: [] for name in requested}
    for name, item in window:
        page[name].append(item)
    return page, totals, len(ordered)


def _page_payload(
    limit: int,
    offset: int,
    *,
    total: int,
    returned: int,
    totals: Mapping[str, int],
) -> dict[str, Any]:
    next_offset = offset + limit
    has_more = next_offset < total
    return {
        "limit": limit,
        "cursor": str(offset) if offset else "",
        "next_cursor": str(next_offset) if has_more else "",
        "has_more": has_more,
        "total_items": total,
        "returned_items": returned,
        "section_totals": dict(totals),
    }


def _cursor_offset(cursor: str) -> int:
    text = str(cursor or "").strip()
    if not text:
        return 0
    if not text.isdigit():
        raise ValueError(
            f"cursor must be the next_cursor value from a previous page: {cursor!r}"
        )
    return int(text)


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "ASSET_REFERENCE_BOUNDARY",
    "ASSET_REFERENCE_LENGTH",
    "ASSET_REFERENCE_PREFIX",
    "ASSET_REFERENCE_VERSION",
    "DISPLAY_LOCATOR_VERSION",
    "EVIDENCE_PACKAGE_BOUNDARY",
    "EVIDENCE_PACKAGE_VERSION",
    "MAX_INLINE_ASSET_BYTES",
    "MAX_PACKAGE_UNITS",
    "MAX_PAGE_SIZE",
    "PACKAGE_SECTIONS",
    "SOURCE_REFERENCE_BOUNDARY",
    "SOURCE_REFERENCE_VERSION",
    "UNCERTAINTY_CANDIDATE_RELATION",
    "UNCERTAINTY_LAYER_UNAVAILABLE",
    "UNCERTAINTY_LOW_QUALITY_OCR",
    "UNCERTAINTY_MISSING_GEOMETRY",
    "UNIT_EVIDENCE_PREFIX",
    "UNIT_IMAGE_PREFIX",
    "asset_content",
    "asset_reference",
    "authorized_assets",
    "display_locator",
    "evidence_package",
    "evidence_packages",
    "hydrated_hit",
    "image_layout",
    "image_transcription",
    "looks_like_asset_reference",
    "parse_unit_id",
    "requested_sections",
    "resolve_asset_reference",
    "source_reference",
]
