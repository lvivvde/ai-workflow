"""The claim vocabulary shared by the invariants, the metrics, and the report.

The invariant checks and the retrieval metrics both have to answer "what did
this response claim, and where does it say the claim came from?". Keeping one
definition here stops the two from drifting apart and silently disagreeing
about what counts as a claim, a source, or a locator.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


# Response states that carry project facts rather than an explicit absence.
FOUND_FAMILY = frozenset({"found", "partial", "degraded", "stale"})

SOURCE_REFERENCE_FIELDS = ("source_document", "asset_path", "source_ref")

POSITION_FIELDS = (
    "locator",
    "position",
    "cell_reference",
    "region_id",
    "paragraph_index",
)

# Which response keys hold the claims of each tool.
CLAIM_COLLECTIONS_BY_TOOL: Mapping[str, tuple[str, ...]] = {
    "search_evidence": ("evidence",),
    "search_images": ("matches",),
    "search_config_cells": ("cells",),
    "get_sheet_range": ("cells",),
    "get_evidence": ("evidence",),
    "get_feature_evidence": ("documents", "configs", "images"),
}


def claims_for(
    tool: str,
    response: Mapping[str, Any],
    statuses: Iterable[str] | None = None,
) -> list[Mapping[str, Any]]:
    """Collect the claims a response makes, annotated with their source.

    ``statuses`` restricts collection to the given response states; the
    invariant checks pass nothing and judge the claims themselves.
    """

    if statuses is not None and response.get("status") not in set(statuses):
        return []
    claims: list[Mapping[str, Any]] = []
    for key in CLAIM_COLLECTIONS_BY_TOOL.get(tool, ()):
        value = response.get(key)
        if isinstance(value, Mapping):
            claims.append(with_response_source(value, response))
        elif isinstance(value, list):
            claims.extend(
                with_response_source(item, response)
                for item in value
                if isinstance(item, Mapping)
            )
    return claims


def with_response_source(
    claim: Mapping[str, Any], response: Mapping[str, Any]
) -> Mapping[str, Any]:
    """A response-level source locates every claim it returns."""

    if any(claim.get(key) for key in SOURCE_REFERENCE_FIELDS):
        return claim
    response_source = response.get("source_document")
    if not response_source:
        return claim
    return {**claim, "source_document": response_source}


def claim_label(claim: Mapping[str, Any]) -> str:
    for key in (
        "evidence_id",
        "image_id",
        "cell_reference",
        "statement_id",
        "package_id",
    ):
        if claim.get(key) is not None:
            return f"{key}={claim[key]}"
    return "unnamed"


def first_present(claim: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in claim and claim[key] not in (None, "", {}, []):
            return claim[key]
    return None


def claim_position(claim: Mapping[str, Any]) -> Any:
    """Where inside its source a claim says it came from.

    A claim about a standalone asset is located by the asset itself: there is no
    smaller place inside it to point at, and the index registers such a file as
    its own document (``document_type=image``, ``relationship_id=standalone``).
    A claim inside a larger file still has to name the part it read, so an
    embedded or column-oriented claim never falls back to the file.
    """

    position = first_present(claim, POSITION_FIELDS)
    if position is not None:
        return position
    if str(claim.get("document_type") or "") == "image" and claim.get("asset_path"):
        return claim.get("asset_path")
    return None


def claim_is_traceable(claim: Mapping[str, Any]) -> bool:
    """A traceable claim names a source and says where inside it to look."""

    has_source = any(claim.get(key) for key in SOURCE_REFERENCE_FIELDS)
    return has_source and claim_position(claim) is not None


def jsonable(value: Any) -> Any:
    """Turn tuples and non-string keys into plain JSON-friendly data."""

    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


__all__ = [
    "CLAIM_COLLECTIONS_BY_TOOL",
    "FOUND_FAMILY",
    "POSITION_FIELDS",
    "SOURCE_REFERENCE_FIELDS",
    "claim_is_traceable",
    "claim_label",
    "claim_position",
    "claims_for",
    "first_present",
    "jsonable",
    "with_response_source",
]
