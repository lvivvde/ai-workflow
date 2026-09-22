"""Frozen public contract for the V1 MCP surface.

V2 may add tools, optional parameters, and response fields. It must not rename,
delete, re-default, or change the meaning of anything recorded here.

Compatibility is checked against this public contract, never against SQLite
bytes: the derived index is disposable, so byte equality would produce
false-positive regressions while still missing real contract breaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


READ_RESULTS = frozenset({"found", "not_found", "ambiguous", "stale"})
PREVIEW_STATES = frozenset({"confirmation_required"})
APPLIED_STATES = frozenset({"completed"})

INDEX_STATUS_FIELDS = frozenset(
    {
        "schema_version",
        "database_path",
        "indexed_at",
        "documents_indexed",
        "images_indexed",
        "ocr_succeeded",
        "ocr_failed",
        "ocr_unavailable",
        "stale_documents",
        "catalog_configured",
        "catalog_is_stale",
        "is_stale",
    }
)

DOCUMENT_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "source_document",
        "document_type",
        "evidence_type",
        "text",
        "section_path",
        "locator",
        "block_ordinal",
        "score",
    }
)

DOCX_LOCATOR_FIELDS = frozenset({"paragraph_index"})
XLSX_LOCATOR_FIELDS = frozenset({"sheet_name", "cell_reference"})

CONFIG_CELL_FIELDS = frozenset(
    {
        "source_document",
        "workbook",
        "sheet_name",
        "cell_reference",
        "row_index",
        "column_index",
        "raw_value",
        "display_text",
        "formula",
        "data_type",
        "style_id",
        "merged_range",
    }
)

# ``get_sheet_range`` reports the document, workbook, and sheet once at the
# response level, so its cells carry only the cell-local columns.
SHEET_RANGE_CELL_FIELDS = CONFIG_CELL_FIELDS - {
    "source_document",
    "workbook",
    "sheet_name",
}

IMAGE_FIELDS = frozenset(
    {
        "source_document",
        "document_type",
        "asset_path",
        "heading",
        "paragraph_index",
        "context_text",
        "sheet_name",
        "cell_anchor",
        "ocr_status",
        "ocr_text",
    }
)

IMAGE_MATCH_FIELDS = IMAGE_FIELDS | {"image_id", "score"}


@dataclass(frozen=True)
class ToolContract:
    """The parts of one MCP tool the V1 clients are allowed to depend on."""

    name: str
    kind: str
    required_parameters: tuple[str, ...]
    default_parameters: Mapping[str, Any] = field(default_factory=dict)
    # Keys every non-error response carries, whatever its status.
    response_keys: frozenset[str] = frozenset()
    # Extra keys that only appear once the tool has a payload to return.
    result_response_keys: frozenset[str] = frozenset()
    # Extra keys of the explicit "nothing matched" answer, which must not be
    # confused with a payload that happens to be empty.
    absent_response_keys: frozenset[str] = frozenset()
    applied_response_keys: frozenset[str] = frozenset()
    allowed_status: frozenset[str] = frozenset()

    @property
    def optional_parameters(self) -> tuple[str, ...]:
        return tuple(self.default_parameters)


V1_TOOL_CONTRACTS: dict[str, ToolContract] = {
    "index_status": ToolContract(
        name="index_status",
        kind="read",
        required_parameters=(),
        response_keys=INDEX_STATUS_FIELDS,
        allowed_status=READ_RESULTS,
    ),
    "search_images": ToolContract(
        name="search_images",
        kind="read",
        required_parameters=("query",),
        default_parameters={"limit": 10},
        response_keys=frozenset({"status", "matches", "index_status"}),
        allowed_status=READ_RESULTS,
    ),
    "search_evidence": ToolContract(
        name="search_evidence",
        kind="read",
        required_parameters=("query",),
        default_parameters={
            "document_type": None,
            "evidence_type": None,
            "limit": 20,
        },
        response_keys=frozenset(
            {
                "status",
                "query",
                "match_type",
                "evidence",
                "conflicts",
                "limitations",
                "index_status",
            }
        ),
        allowed_status=READ_RESULTS,
    ),
    "get_evidence": ToolContract(
        name="get_evidence",
        kind="read",
        required_parameters=("evidence_id",),
        default_parameters={"context_before": 1, "context_after": 1},
        response_keys=frozenset(
            {
                "status",
                "evidence_id",
                "evidence",
                "context_before",
                "context_after",
                "index_status",
            }
        ),
        allowed_status=READ_RESULTS,
    ),
    "search_config_cells": ToolContract(
        name="search_config_cells",
        kind="read",
        required_parameters=("query",),
        default_parameters={"workbook": None, "sheet": None, "limit": 50},
        response_keys=frozenset(
            {"status", "query", "match_type", "cells", "limitations", "index_status"}
        ),
        allowed_status=READ_RESULTS,
    ),
    "get_sheet_range": ToolContract(
        name="get_sheet_range",
        kind="read",
        required_parameters=("workbook", "sheet", "range"),
        response_keys=frozenset(
            {
                "status",
                "workbook",
                "sheet",
                "range",
                "cells",
                "limitations",
                "index_status",
            }
        ),
        # ``source_document`` only arrives with real cells.
        result_response_keys=frozenset({"source_document"}),
        allowed_status=READ_RESULTS,
    ),
    "find_feature": ToolContract(
        name="find_feature",
        kind="read",
        required_parameters=("name",),
        response_keys=frozenset(
            {
                "status",
                "query",
                "match_type",
                "feature",
                "matched_alias",
                "limitations",
                "index_status",
            }
        ),
        allowed_status=READ_RESULTS,
    ),
    "get_feature_evidence": ToolContract(
        name="get_feature_evidence",
        kind="read",
        required_parameters=("name",),
        default_parameters={
            "include_documents": True,
            "include_configs": True,
            "include_images": True,
        },
        response_keys=frozenset(
            {
                "status",
                "query",
                "resolved_name",
                "feature",
                "documents",
                "configs",
                "images",
                "limitations",
                "index_status",
            }
        ),
        # An unresolved name has no match type to report.
        result_response_keys=frozenset({"match_type"}),
        allowed_status=READ_RESULTS,
    ),
    "get_image_context": ToolContract(
        name="get_image_context",
        kind="read",
        required_parameters=("image_id",),
        response_keys=frozenset({"status", "index_status"}),
        # The image fields only arrive for an indexed image; a miss echoes the
        # requested id instead, and never repeats it on a hit.
        result_response_keys=IMAGE_FIELDS,
        absent_response_keys=frozenset({"image_id", "limitations"}),
        allowed_status=READ_RESULTS,
    ),
    "plan_document_import": ToolContract(
        name="plan_document_import",
        kind="write",
        required_parameters=("source_paths",),
        default_parameters={"destination": "docs", "operation": "copy"},
        response_keys=frozenset(
            {
                "status",
                "project_root",
                "destination",
                "operation",
                "items",
                "plan_token",
                "will_rebuild_shared_index",
                "limitations",
            }
        ),
        allowed_status=PREVIEW_STATES,
    ),
    "import_documents": ToolContract(
        name="import_documents",
        kind="write",
        required_parameters=("source_paths", "plan_token"),
        default_parameters={
            "destination": "docs",
            "operation": "copy",
            "confirmed": False,
        },
        response_keys=frozenset(
            {
                "status",
                "project_root",
                "destination",
                "operation",
                "items",
                "plan_token",
                "will_rebuild_shared_index",
                "limitations",
            }
        ),
        applied_response_keys=frozenset(
            {
                "status",
                "destination",
                "operation",
                "files",
                "index_directory",
                "index_report",
                "git_paths_to_commit",
                "index_status",
            }
        ),
        allowed_status=PREVIEW_STATES | APPLIED_STATES,
    ),
    "rebuild_shared_index": ToolContract(
        name="rebuild_shared_index",
        kind="write",
        required_parameters=(),
        default_parameters={"confirmed": False},
        response_keys=frozenset(
            {
                "status",
                "project_root",
                "index_directory",
                "will_rebuild_shared_index",
                "limitations",
            }
        ),
        applied_response_keys=frozenset({"status", "project_root", "index_directory"}),
        allowed_status=PREVIEW_STATES | APPLIED_STATES,
    ),
}


def tool_surface(tools: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Normalize MCP ``list_tools`` output into plain comparable data."""

    surface: dict[str, dict[str, Any]] = {}
    for tool in tools:
        schema = _field(tool, "input_schema") or _field(tool, "inputSchema") or {}
        properties = schema.get("properties") or {}
        defaults = {
            name: definition["default"]
            for name, definition in properties.items()
            if isinstance(definition, dict) and "default" in definition
        }
        name = str(_field(tool, "name"))
        surface[name] = {
            "required_parameters": tuple(schema.get("required") or ()),
            "default_parameters": defaults,
        }
    return surface


def contract_violations(surface: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Report every way the live tool surface breaks the frozen V1 contract."""

    violations: list[str] = []
    for name, contract in V1_TOOL_CONTRACTS.items():
        actual = surface.get(name)
        if actual is None:
            violations.append(f"{name}: tool is missing from the server")
            continue

        actual_required = tuple(actual.get("required_parameters") or ())
        if actual_required != contract.required_parameters:
            violations.append(
                f"{name}: required parameters changed from "
                f"{list(contract.required_parameters)} to {list(actual_required)}"
            )

        actual_defaults = dict(actual.get("default_parameters") or {})
        for parameter, default in contract.default_parameters.items():
            if parameter not in actual_defaults:
                violations.append(
                    f"{name}: optional parameter {parameter!r} lost its default "
                    f"({default!r})"
                )
            elif actual_defaults[parameter] != default:
                violations.append(
                    f"{name}: default for {parameter!r} changed from {default!r} "
                    f"to {actual_defaults[parameter]!r}"
                )
    return violations


def missing_response_keys(
    contract: ToolContract, response: Mapping[str, Any], applied: bool = False
) -> list[str]:
    """Return frozen V1 response keys the live response no longer carries.

    Write tools have their own key set once they apply, and read tools add
    payload keys only for the statuses that carry a payload, so the required
    set is built from the response's own status instead of assuming one shape.
    """

    required = set(contract.response_keys)
    if applied and contract.applied_response_keys:
        required = set(contract.applied_response_keys)
    else:
        status = response.get("status")
        if status == "not_found":
            required |= contract.absent_response_keys
        elif status is not None:
            required |= contract.result_response_keys
    return sorted(required - set(response))


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


__all__ = [
    "APPLIED_STATES",
    "CONFIG_CELL_FIELDS",
    "DOCUMENT_EVIDENCE_FIELDS",
    "DOCX_LOCATOR_FIELDS",
    "IMAGE_FIELDS",
    "IMAGE_MATCH_FIELDS",
    "INDEX_STATUS_FIELDS",
    "PREVIEW_STATES",
    "READ_RESULTS",
    "SHEET_RANGE_CELL_FIELDS",
    "ToolContract",
    "V1_TOOL_CONTRACTS",
    "XLSX_LOCATOR_FIELDS",
    "contract_violations",
    "missing_response_keys",
    "tool_surface",
]
