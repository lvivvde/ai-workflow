"""Structural relations a picture actually shows: vertical arrows and order.

The rule this module implements is deliberately narrow, because the expensive
mistake is turning a decorative arrow into a rule. A ``next_step`` is confirmed
only when all of this holds (issue #3, spec #11 3.5):

* the arrow is its own block -- nothing but arrow glyphs, one direction;
* it is alone in its row, so two arrows never read as one step;
* exactly one aligned text block above and one below, with no arrow run in
  between;
* both endpoints sit in the same column and overlap nothing.

Everything else is emitted as a ``candidate`` with the reason it could not be
confirmed, and every relation records its endpoints, the regions that support
it, the geometric basis, the rule version, and two separate confidences.

A confirmed ``next_step`` says one thing: this block sits below that block with
an arrow between them. It is not a claim about causality, runtime order, or the
author's intent, and indentation never creates a parent/child relation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .layout import ReadingOrder, VisualElement


RELATION_RULESET_VERSION = "flow-arrow-v1"

RELATION_KINDS = ("next_step", "points_to")
RELATION_STATUSES = ("confirmed", "candidate")

NEXT_STEP = "next_step"
POINTS_TO = "points_to"
CONFIRMED = "confirmed"
CANDIDATE = "candidate"

UNCERTAINTY_AMBIGUOUS_DIRECTION = "ambiguous_direction"
UNCERTAINTY_BRANCH = "branch"
UNCERTAINTY_MISSING_ENDPOINT = "missing_endpoint"
UNCERTAINTY_CONSECUTIVE_ARROW = "consecutive_arrow"
UNCERTAINTY_CROSS_COLUMN = "cross_column"
UNCERTAINTY_OVERLAPPING_LAYOUT = "overlapping_layout"
RELATION_UNCERTAINTIES = (
    "",
    UNCERTAINTY_AMBIGUOUS_DIRECTION,
    UNCERTAINTY_BRANCH,
    UNCERTAINTY_MISSING_ENDPOINT,
    UNCERTAINTY_CONSECUTIVE_ARROW,
    UNCERTAINTY_CROSS_COLUMN,
    UNCERTAINTY_OVERLAPPING_LAYOUT,
)

#: Attached to every payload so a reader cannot mistake a visible adjacency for
#: a statement about why the flow exists.
CLAIM_BOUNDARY = (
    "A relation records visible layout only: which block sits above or beside "
    "which, with an arrow between them. It is not evidence of causality, "
    "runtime dependency, prerequisites, or design intent."
)

#: How many rows to look past while resolving an endpoint.
LOOKBACK_ROWS = 3


@dataclass(frozen=True)
class StructuralRelation:
    """One visible structural relation, with everything needed to re-check it."""

    kind: str
    status: str
    source_region: int
    target_region: int
    via_regions: tuple[int, ...]
    direction: str
    geometry_basis: str
    detail: str
    uncertainty: str = ""
    geometry_confidence: float | None = None
    ocr_confidence: float | None = None
    rule_version: str = RELATION_RULESET_VERSION

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIRMED

    @property
    def claim_boundary(self) -> str:
        """What this relation does *not* say; stored beside every relation."""

        return CLAIM_BOUNDARY

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "source_region": self.source_region,
            "target_region": self.target_region,
            "via_regions": list(self.via_regions),
            "direction": self.direction,
            "geometry_basis": self.geometry_basis,
            "detail": self.detail,
            "uncertainty": self.uncertainty,
            "geometry_confidence": self.geometry_confidence,
            "ocr_confidence": self.ocr_confidence,
            "rule_version": self.rule_version,
            "claim_boundary": self.claim_boundary,
        }


@dataclass(frozen=True)
class _Endpoint:
    """What the geometry says about one side of an arrow."""

    status: str
    element: VisualElement | None
    arrows_passed: int
    basis: str


def build_relations(order: ReadingOrder) -> tuple[StructuralRelation, ...]:
    """Every relation this build is willing to report for one image."""

    if not order.elements:
        return ()
    rows = _rows_by_index(order)
    relations: list[StructuralRelation] = []
    for element in order.elements:
        if not element.is_arrow or element.bbox is None:
            continue
        if element.direction == "mixed":
            relations.append(_ambiguous_arrow(element))
            continue
        if element.direction in {"down", "up"}:
            relations.append(_vertical_relation(order, rows, element))
            continue
        if element.direction in {"left", "right"}:
            relations.append(_horizontal_relation(rows, element))
    return tuple(relations)


def _rows_by_index(order: ReadingOrder) -> dict[int, list[VisualElement]]:
    rows: dict[int, list[VisualElement]] = {}
    for element in order.elements:
        rows.setdefault(element.row, []).append(element)
    return rows


def _ambiguous_arrow(arrow: VisualElement) -> StructuralRelation:
    return StructuralRelation(
        kind=NEXT_STEP,
        status=CANDIDATE,
        source_region=-1,
        target_region=-1,
        via_regions=(arrow.region_index,),
        direction="mixed",
        geometry_basis="the block holds arrow glyphs pointing in more than one direction",
        detail=(
            "This block is nothing but arrows that disagree, so no step order is "
            "claimed for it."
        ),
        uncertainty=UNCERTAINTY_AMBIGUOUS_DIRECTION,
        geometry_confidence=0.1,
        ocr_confidence=arrow.text_confidence,
    )


def _vertical_relation(
    order: ReadingOrder,
    rows: dict[int, list[VisualElement]],
    arrow: VisualElement,
) -> StructuralRelation:
    below = _resolve_endpoint(rows, arrow, below=True)
    above = _resolve_endpoint(rows, arrow, below=False)
    descending = arrow.direction == "down"
    source = above.element if descending else below.element
    target = below.element if descending else above.element
    passed = above.arrows_passed + below.arrows_passed
    same_row_arrows = [
        member
        for member in rows.get(arrow.row, ())
        if member.is_arrow and member.region_index != arrow.region_index
    ]
    basis = (
        f"arrow block {arrow.region_index} points {arrow.direction}; "
        f"above: {above.basis}; below: {below.basis}"
    )
    relation_uncertainty = ""
    detail = (
        "The arrow is its own block, alone in its row, with exactly one aligned "
        "block on each side in the same column."
    )
    if above.status == "branch" or below.status == "branch" or same_row_arrows:
        relation_uncertainty = UNCERTAINTY_BRANCH
        detail = (
            "More than one block competes to be an endpoint, or the arrow shares "
            "its row with another arrow, so this is a branch rather than one step."
        )
    elif (
        above.status == "cross_column"
        or below.status == "cross_column"
        or (
            source is not None
            and target is not None
            and source.column != target.column
        )
    ):
        relation_uncertainty = UNCERTAINTY_CROSS_COLUMN
        detail = (
            "The blocks the arrow reaches sit in different columns, so the arrow "
            "crosses a container boundary."
        )
    elif above.status == "missing" or below.status == "missing":
        relation_uncertainty = UNCERTAINTY_MISSING_ENDPOINT
        detail = "Only one side of the arrow has an aligned text block."
    elif passed:
        relation_uncertainty = UNCERTAINTY_CONSECUTIVE_ARROW
        detail = (
            f"The endpoints sit beyond {passed} lower-tier arrow block(s) in a row, "
            "so the step is resolved across the run rather than at its first hop."
        )
    elif _overlaps_any(arrow, source, target):
        relation_uncertainty = UNCERTAINTY_OVERLAPPING_LAYOUT
        detail = "An endpoint box overlaps the arrow box, so the layout is not clean."
    status = CONFIRMED if not relation_uncertainty else CANDIDATE
    return StructuralRelation(
        kind=NEXT_STEP,
        status=status,
        source_region=source.region_index if source is not None else -1,
        target_region=target.region_index if target is not None else -1,
        via_regions=(arrow.region_index,),
        direction=arrow.direction,
        geometry_basis=basis,
        detail=detail,
        uncertainty=relation_uncertainty,
        geometry_confidence=_geometry_confidence(status, source, target),
        ocr_confidence=_ocr_confidence(arrow, source, target),
    )


def _horizontal_relation(
    rows: dict[int, list[VisualElement]],
    arrow: VisualElement,
) -> StructuralRelation:
    members = [member for member in rows.get(arrow.row, ())]
    left = sorted(
        (
            member
            for member in members
            if not member.is_arrow
            and member.bbox is not None
            and member.bbox.x + member.bbox.width <= arrow.bbox.x
        ),
        key=lambda member: member.bbox.x + member.bbox.width,
        reverse=True,
    )
    right = sorted(
        (
            member
            for member in members
            if not member.is_arrow
            and member.bbox is not None
            and member.bbox.x >= arrow.bbox.x + arrow.bbox.width
        ),
        key=lambda member: member.bbox.x,
    )
    source = left[0] if left else None
    target = right[0] if right else None
    uncertainty = ""
    if len(left) > 1 or len(right) > 1:
        uncertainty = UNCERTAINTY_BRANCH
        detail = "More than one text block sits on one side of this arrow."
    elif source is None or target is None:
        uncertainty = UNCERTAINTY_MISSING_ENDPOINT
        detail = (
            "The arrow shares no row with a text block on one side, so no visible "
            "pointing relation is claimed for that side."
        )
    else:
        detail = (
            "A text block sits on each side of the arrow inside one row, so the "
            "picture shows which one points at which."
        )
    status = CONFIRMED if not uncertainty else CANDIDATE
    return StructuralRelation(
        kind=POINTS_TO,
        status=status,
        source_region=source.region_index if source is not None else -1,
        target_region=target.region_index if target is not None else -1,
        via_regions=(arrow.region_index,),
        direction=arrow.direction,
        geometry_basis=(
            f"arrow block {arrow.region_index} points {arrow.direction} inside row "
            f"{arrow.row}"
        ),
        detail=detail,
        uncertainty=uncertainty,
        geometry_confidence=_geometry_confidence(status, source, target),
        ocr_confidence=_ocr_confidence(arrow, source, target),
    )


def _resolve_endpoint(
    rows: dict[int, list[VisualElement]],
    arrow: VisualElement,
    *,
    below: bool,
) -> _Endpoint:
    """The single aligned text block on one side of a vertical arrow.

    A row whose blocks sit either side of the arrow's column is a fork, and a
    row whose blocks all sit in another column is a container boundary; both are
    reported as the reason this side could not be resolved, so the reader can
    tell "nothing there" apart from "something there, in the wrong place".
    """

    candidates = [
        row
        # Nearest row first: ascending below the arrow, descending above it.
        for row in sorted(rows, reverse=not below)
        if (row > arrow.row if below else row < arrow.row)
    ]
    arrows_passed = 0
    for offset, row in enumerate(candidates[:LOOKBACK_ROWS]):
        members = rows[row]
        texts = [member for member in members if not member.is_arrow]
        if not texts:
            arrows_passed += len([member for member in members if member.is_arrow])
            continue
        aligned = [member for member in texts if _aligned(member, arrow)]
        side = "below" if below else "above"
        if len(aligned) == 1:
            return _Endpoint(
                "unique",
                aligned[0],
                arrows_passed,
                (
                    f"row {row} ({offset} row(s) {side}) holds exactly one aligned "
                    f"block, region {aligned[0].region_index}"
                ),
            )
        if len(aligned) > 1:
            return _Endpoint(
                "branch",
                None,
                arrows_passed,
                f"row {row} ({side}) holds {len(aligned)} aligned blocks",
            )
        if _bracketed_by(texts, arrow):
            return _Endpoint(
                "branch",
                None,
                arrows_passed,
                (
                    f"row {row} ({side}) holds {len(texts)} blocks either side of "
                    "the arrow's column"
                ),
            )
        if all(member.column != arrow.column for member in texts):
            return _Endpoint(
                "cross_column",
                None,
                arrows_passed,
                f"row {row} ({side}) holds text in another column than the arrow",
            )
        return _Endpoint(
            "missing",
            None,
            arrows_passed,
            f"row {row} ({side}) holds text, but none of it is aligned with the arrow",
        )
    return _Endpoint(
        "missing",
        None,
        arrows_passed,
        f"no text row within {LOOKBACK_ROWS} row(s) {('below' if below else 'above')}",
    )


def _aligned(member: VisualElement, arrow: VisualElement) -> bool:
    """Whether a text block lines up with the arrow's centre column."""

    if member.bbox is None:
        return False
    centre = arrow.bbox.x + arrow.bbox.width / 2
    if member.bbox.x <= centre <= member.bbox.x + member.bbox.width:
        return True
    overlap = min(member.bbox.x + member.bbox.width, arrow.bbox.x + arrow.bbox.width)
    overlap -= max(member.bbox.x, arrow.bbox.x)
    narrower = min(member.bbox.width, arrow.bbox.width)
    return narrower > 0 and max(0.0, overlap) / narrower >= 0.5


def _bracketed_by(
    texts: Sequence[VisualElement], arrow: VisualElement
) -> bool:
    """Whether the arrow's column falls between two text blocks in its row."""

    centre = arrow.bbox.x + arrow.bbox.width / 2
    left = any(
        member.bbox.x + member.bbox.width <= centre
        for member in texts
        if member.bbox is not None
    )
    right = any(
        member.bbox.x >= centre for member in texts if member.bbox is not None
    )
    return left and right


def _overlaps_any(
    arrow: VisualElement, *elements: VisualElement | None
) -> bool:
    for element in elements:
        if element is None or element.bbox is None:
            continue
        left = max(arrow.bbox.x, element.bbox.x)
        right = min(
            arrow.bbox.x + arrow.bbox.width, element.bbox.x + element.bbox.width
        )
        top = max(arrow.bbox.y, element.bbox.y)
        bottom = min(
            arrow.bbox.y + arrow.bbox.height, element.bbox.y + element.bbox.height
        )
        if right - left > 0 and bottom - top > 0:
            return True
    return False


def _geometry_confidence(
    status: str, source: VisualElement | None, target: VisualElement | None
) -> float:
    if status == CONFIRMED:
        return 1.0
    if source is None or target is None:
        return 0.2
    return 0.5


def _ocr_confidence(*elements: VisualElement | None) -> float | None:
    """The transcription layer of the regions this relation rests on.

    Kept apart from ``geometry_confidence`` so a certain layout built on shaky
    text is visible as exactly that.
    """

    known = [
        element.text_confidence
        for element in elements
        if element is not None and element.text_confidence is not None
    ]
    if not known:
        return None
    return min(known)


def summarize_relations(
    relations: Sequence[StructuralRelation],
) -> dict[str, int]:
    """Counters for the build report and the layout detail reads."""

    summary = {
        "relations": len(relations),
        "confirmed_relations": 0,
        "candidate_relations": 0,
        "next_step_relations": 0,
        "points_to_relations": 0,
        "uncertain_relations": 0,
    }
    for relation in relations:
        summary["confirmed_relations"] += relation.confirmed
        summary["candidate_relations"] += not relation.confirmed
        summary["next_step_relations"] += relation.kind == NEXT_STEP
        summary["points_to_relations"] += relation.kind == POINTS_TO
        summary["uncertain_relations"] += bool(relation.uncertainty)
    return summary


__all__ = [
    "CANDIDATE",
    "CLAIM_BOUNDARY",
    "CONFIRMED",
    "LOOKBACK_ROWS",
    "NEXT_STEP",
    "POINTS_TO",
    "RELATION_KINDS",
    "RELATION_RULESET_VERSION",
    "RELATION_STATUSES",
    "RELATION_UNCERTAINTIES",
    "StructuralRelation",
    "UNCERTAINTY_AMBIGUOUS_DIRECTION",
    "UNCERTAINTY_BRANCH",
    "UNCERTAINTY_CONSECUTIVE_ARROW",
    "UNCERTAINTY_CROSS_COLUMN",
    "UNCERTAINTY_MISSING_ENDPOINT",
    "UNCERTAINTY_OVERLAPPING_LAYOUT",
    "build_relations",
    "summarize_relations",
]
