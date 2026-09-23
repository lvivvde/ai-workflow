"""Conservative layout geometry over OCR regions: elements and reading order.

Nothing here guesses. A region becomes a visual element with the geometry it
already has, rows come from vertical overlap, columns only when two groups
actually sit side by side in the same row band, and indentation is recorded as
a ``depth_hint`` -- never promoted into a parent/child claim (spec #11 3.5,
issue #20).

Geometry confidence and OCR confidence are separate numbers on purpose: a
cleanly separated column is certain even when the text inside it was read
shakily, and a good transcription of a region whose box is missing is still a
region with no place in the reading order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .ocr_normalization import canonical_token
from .ocr_regions import BoundingBox, OcrRegion


RULESET_VERSION = "layout-regions-v1"

#: Arrows this build reads as flow notation, checked after canonical folding so
#: ``->`` and ``→`` are the same token. Anything else in the block means it is
#: text that happens to contain an arrow, not an arrow block.
VERTICAL_DIRECTIONS = {"↓": "down", "▼": "down", "↑": "up", "▲": "up"}
HORIZONTAL_DIRECTIONS = {"→": "right", "←": "left"}

#: Two boxes are "the same row" when their vertical spans overlap this much of
#: the shorter one; anything less is the next line.
ROW_OVERLAP_RATIO = 0.5
#: Horizontal offsets within this many pixels are the same indent level.
INDENT_TOLERANCE = 4.0
#: A column gap narrower than this is reported as ambiguous rather than certain.
MIN_COLUMN_GAP = 8.0

TEXT_KIND = "text"
ARROW_KIND = "arrow"
ELEMENT_KINDS = (TEXT_KIND, ARROW_KIND)

UNCERTAINTY_MISSING_GEOMETRY = "missing_geometry"
UNCERTAINTY_OVERLAPPING_BOXES = "overlapping_boxes"
UNCERTAINTY_AMBIGUOUS_COLUMNS = "ambiguous_columns"


def arrow_direction(text: str) -> str:
    """``""`` for ordinary text, else the direction an arrow-only block points.

    ``"mixed"`` means the block is nothing but arrows that disagree, which is a
    visible layout fact this build refuses to resolve into a direction.
    """

    folded = canonical_token(text)
    if not folded:
        return ""
    directions: set[str] = set()
    for character in folded:
        direction = VERTICAL_DIRECTIONS.get(character) or HORIZONTAL_DIRECTIONS.get(
            character
        )
        if direction is None:
            # The block carries something that is not an arrow, so it is text.
            return ""
        directions.add(direction)
    if len(directions) != 1:
        return "mixed"
    return directions.pop()


def arrow_axis(text: str) -> str:
    """``"vertical"``/``"horizontal"`` for an arrow-only block, else ``""``.

    This is the one part of a block's reading the transcription is trusted to
    carry. The deployed core engine's measured confusions are *within* a family
    -- a block drawn as ``↓↓`` comes back as ``↑↑``, never as ``→`` -- while
    which end of that axis the arrow points at is the thing it gets wrong
    (issue #28). So the axis is read here and the polarity is measured from the
    picture, in ``ink.py``.
    """

    direction = arrow_direction(text)
    if direction in ("up", "down"):
        return "vertical"
    if direction in ("left", "right"):
        return "horizontal"
    return ""


@dataclass(frozen=True)
class VisualElement:
    """One region as a layout element, with its place in the reading order."""

    region_index: int
    kind: str
    text: str
    bbox: BoundingBox | None = None
    direction: str = ""
    reading_order: int = 0
    row: int = 0
    column: int = 0
    depth_hint: int = 0
    text_confidence: float | None = None

    @property
    def is_arrow(self) -> bool:
        return self.kind == ARROW_KIND

    def as_payload(self) -> dict[str, Any]:
        return {
            "region_index": self.region_index,
            "kind": self.kind,
            "text": self.text,
            "direction": self.direction,
            "reading_order": self.reading_order,
            "row": self.row,
            "column": self.column,
            "depth_hint": self.depth_hint,
            "bbox": self.bbox.as_payload() if self.bbox is not None else None,
            "text_confidence": self.text_confidence,
        }


@dataclass(frozen=True)
class ReadingOrder:
    """The elements of one image in the order this build can defend."""

    elements: tuple[VisualElement, ...]
    column_count: int
    order_source: str
    geometry_confidence: float
    uncertainty: str
    detail: str
    ruleset_version: str = RULESET_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "elements": [element.as_payload() for element in self.elements],
            "column_count": self.column_count,
            "order_source": self.order_source,
            "geometry_confidence": self.geometry_confidence,
            "uncertainty": self.uncertainty,
            "detail": self.detail,
            "ruleset_version": self.ruleset_version,
        }

    def element(self, region_index: int) -> VisualElement | None:
        for candidate in self.elements:
            if candidate.region_index == region_index:
                return candidate
        return None


def build_reading_order(regions: Sequence[OcrRegion]) -> ReadingOrder:
    """Turn OCR regions into elements, rows, columns, and depth hints."""

    elements = tuple(_element(region) for region in regions)
    placed = [element for element in elements if element.bbox is not None]
    unplaced = [element for element in elements if element.bbox is None]
    if not placed:
        ordered = _reindex(_appended(unplaced))
        return ReadingOrder(
            elements=ordered,
            column_count=1,
            order_source="region_index",
            # Same value _order_certainty reports when only some regions lack a
            # box: "no box at all" and "no box for this region" are one fact.
            geometry_confidence=0.4 if elements else 1.0,
            uncertainty=UNCERTAINTY_MISSING_GEOMETRY if elements else "",
            detail=(
                "No region reported a bounding box, so the reading order falls "
                "back to the engine's own region order."
                if elements
                else "This image has no OCR regions to order."
            ),
        )

    rows = _assign_rows(placed)
    row_index = {
        element.region_index: index for index, row in enumerate(rows) for element in row
    }
    clusters, gap = _column_clusters(placed)
    side_by_side = _columns_are_side_by_side(rows, clusters)
    if side_by_side:
        ordered_groups = [
            [element for element in placed if _cluster_of(element, clusters) == index]
            for index in range(len(clusters))
        ]
        ordered = [
            element
            for group in ordered_groups
            for element in sorted(
                group, key=lambda item: (row_index[item.region_index], item.bbox.x)
            )
        ]
        column_of = {
            element.region_index: index
            for index, group in enumerate(ordered_groups)
            for element in group
        }
    else:
        ordered = sorted(
            placed, key=lambda item: (row_index[item.region_index], item.bbox.x)
        )
        column_of = {element.region_index: 0 for element in ordered}

    ordered = [*ordered, *unplaced]
    depths = _depth_hints(ordered, column_of)
    ordered = _reindex(
        [
            _with_place(
                element,
                reading_order=position,
                row=row_index.get(element.region_index, len(rows)),
                column=column_of.get(element.region_index, 0),
                depth_hint=depths.get(element.region_index, 0),
            )
            for position, element in enumerate(ordered)
        ]
    )
    uncertainty, confidence, detail = _order_certainty(
        rows=rows,
        placed=placed,
        unplaced=unplaced,
        column_count=len(clusters) if side_by_side else 1,
        gap=gap,
    )
    return ReadingOrder(
        elements=ordered,
        column_count=len(clusters) if side_by_side else 1,
        order_source="geometry",
        geometry_confidence=confidence,
        uncertainty=uncertainty,
        detail=detail,
    )


def _element(region: OcrRegion) -> VisualElement:
    direction = arrow_direction(region.text)
    return VisualElement(
        region_index=region.index,
        kind=ARROW_KIND if direction else TEXT_KIND,
        text=region.text,
        bbox=region.bbox,
        direction=direction,
        text_confidence=region.text_confidence,
    )


def _with_place(
    element: VisualElement,
    *,
    reading_order: int,
    row: int,
    column: int,
    depth_hint: int,
) -> VisualElement:
    return VisualElement(
        region_index=element.region_index,
        kind=element.kind,
        text=element.text,
        bbox=element.bbox,
        direction=element.direction,
        reading_order=reading_order,
        row=row,
        column=column,
        depth_hint=depth_hint,
        text_confidence=element.text_confidence,
    )


def _reindex(elements: Iterable[VisualElement]) -> tuple[VisualElement, ...]:
    return tuple(
        _with_place(
            element,
            reading_order=position,
            row=element.row,
            column=element.column,
            depth_hint=element.depth_hint,
        )
        for position, element in enumerate(elements)
    )


def _appended(unplaced: Sequence[VisualElement]) -> list[VisualElement]:
    return list(sorted(unplaced, key=lambda element: element.region_index))


def _vertical_overlap(first: BoundingBox, second: BoundingBox) -> float:
    top = max(first.y, second.y)
    bottom = min(first.y + first.height, second.y + second.height)
    overlap = max(0.0, bottom - top)
    shorter = min(first.height, second.height)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _horizontal_overlap(first: BoundingBox, second: BoundingBox) -> float:
    left = max(first.x, second.x)
    right = min(first.x + first.width, second.x + second.width)
    overlap = max(0.0, right - left)
    narrower = min(first.width, second.width)
    if narrower <= 0:
        return 0.0
    return overlap / narrower


def _assign_rows(
    placed: Sequence[VisualElement],
) -> list[list[VisualElement]]:
    """Group elements into row bands; returns rows top to bottom."""

    rows: list[list[VisualElement]] = []
    for element in sorted(placed, key=lambda item: (item.bbox.y, item.bbox.x)):
        for row in rows:
            if any(
                _vertical_overlap(element.bbox, member.bbox) >= ROW_OVERLAP_RATIO
                for member in row
            ):
                row.append(element)
                break
        else:
            rows.append([element])
    for row in rows:
        row.sort(key=lambda item: item.bbox.x)
    rows.sort(key=lambda row: min(item.bbox.y for item in row))
    return rows


def _column_clusters(
    placed: Sequence[VisualElement],
) -> tuple[list[tuple[float, float, float]], float]:
    """Horizontal bands that no region crosses, plus the widest gap between them.

    Rows and columns answer different questions: a band split only counts as a
    column when the two sides share a row, which is what stops indentation from
    being reported as a second column.
    """

    intervals = sorted(
        (item.bbox.x, item.bbox.x + item.bbox.width, 0.0) for item in placed
    )
    clusters: list[list[float]] = []
    for start, end, _ in intervals:
        if clusters and start < clusters[-1][1]:
            clusters[-1][1] = max(clusters[-1][1], end)
            continue
        clusters.append([start, end])
    total = len(clusters)
    widest = 0.0
    for previous, following in zip(clusters, clusters[1:]):
        widest = max(widest, following[0] - previous[1])
    return [(start, end, float(index)) for index, (start, end) in enumerate(clusters)], widest


def _cluster_of(
    element: VisualElement, clusters: Sequence[tuple[float, float, float]]
) -> int:
    for index, (start, end, _) in enumerate(clusters):
        if start <= element.bbox.x <= end:
            return index
    return 0


def _columns_are_side_by_side(
    rows: Sequence[Sequence[VisualElement]],
    clusters: Sequence[tuple[float, float, float]],
) -> bool:
    """Whether these bands are columns or just different left edges.

    Two conditions have to hold together. Each column has to carry more than
    one block -- one wide block beside one narrow one is a row, not a sheet with
    two columns -- and the columns have to share at least one row band, which is
    what makes them side by side rather than stacked.
    """

    if len(clusters) < 2:
        return False
    members = {
        index: [element for row in rows for element in row if _cluster_of(element, clusters) == index]
        for index in range(len(clusters))
    }
    if sum(len(group) > 1 for group in members.values()) < 2:
        return False
    return any(
        len({_cluster_of(element, clusters) for element in row}) > 1 for row in rows
    )


def _depth_hints(
    ordered: Sequence[VisualElement], column_of: dict[int, int]
) -> dict[int, int]:
    """Indent level per element, measured inside its own column.

    Levels are ``x``-start clusters, so a few pixels of anti-aliasing do not
    invent a nesting level. The value is a hint about how the sheet is laid
    out; it is never turned into a parent/child relation. Arrow blocks are left
    at level 0: they are notation between content lines, so reading an arrow's
    horizontal offset as an indent would describe the drawing, not the content.
    """

    hints: dict[int, int] = {}
    by_column: dict[int, list[VisualElement]] = {}
    for element in ordered:
        if element.bbox is None or element.is_arrow:
            continue
        by_column.setdefault(column_of.get(element.region_index, 0), []).append(element)
    for members in by_column.values():
        levels: list[float] = []
        for element in sorted(members, key=lambda item: item.bbox.x):
            start = element.bbox.x
            level = next(
                (
                    index
                    for index, value in enumerate(levels)
                    if abs(start - value) <= INDENT_TOLERANCE
                ),
                None,
            )
            if level is None:
                levels.append(start)
                level = len(levels) - 1
            hints[element.region_index] = level
    return hints


def _order_certainty(
    *,
    rows: Sequence[Sequence[VisualElement]],
    placed: Sequence[VisualElement],
    unplaced: Sequence[VisualElement],
    column_count: int,
    gap: float,
) -> tuple[str, float, str]:
    if unplaced:
        return (
            UNCERTAINTY_MISSING_GEOMETRY,
            0.4,
            (
                f"{len(unplaced)} region(s) carry no bounding box and were placed "
                "last by region index."
            ),
        )
    overlapping = [
        (first.region_index, second.region_index)
        for row in rows
        for position, first in enumerate(row)
        for second in row[position + 1 :]
        if _horizontal_overlap(first.bbox, second.bbox) > ROW_OVERLAP_RATIO
    ]
    if overlapping:
        return (
            UNCERTAINTY_OVERLAPPING_BOXES,
            0.7,
            (
                "Regions overlap inside one row, so their left-to-right order is "
                f"a guess: {overlapping}."
            ),
        )
    if column_count > 1 and gap < MIN_COLUMN_GAP:
        return (
            UNCERTAINTY_AMBIGUOUS_COLUMNS,
            0.6,
            (
                f"The {column_count} column bands are only {round(gap, 1)}px apart, "
                "so the split is reported but not relied on."
            ),
        )
    if column_count > 1:
        return (
            "",
            1.0,
            f"{column_count} column bands share row bands; read left to right.",
        )
    return (
        "",
        1.0,
        "All regions share one column, read top to bottom, then left to right.",
    )


__all__ = [
    "ARROW_KIND",
    "ELEMENT_KINDS",
    "HORIZONTAL_DIRECTIONS",
    "INDENT_TOLERANCE",
    "MIN_COLUMN_GAP",
    "ROW_OVERLAP_RATIO",
    "RULESET_VERSION",
    "ReadingOrder",
    "TEXT_KIND",
    "UNCERTAINTY_AMBIGUOUS_COLUMNS",
    "UNCERTAINTY_MISSING_GEOMETRY",
    "UNCERTAINTY_OVERLAPPING_BOXES",
    "VERTICAL_DIRECTIONS",
    "VisualElement",
    "arrow_axis",
    "arrow_direction",
    "build_reading_order",
]
