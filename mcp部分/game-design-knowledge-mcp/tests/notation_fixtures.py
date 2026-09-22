"""A small project whose picture holds one agreed step and one disputed arrow.

The image is the smallest thing that can produce both kinds of notation at once:
a vertical arrow between two text blocks resolves into a machine-supported
``next_step``, and an arrow block that points both ways stays a candidate with
its own uncertainty code. Everything the notation tests do starts from here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from game_design_knowledge.indexer import index_documents
from game_design_knowledge.ocr_regions import (
    BoundingBox,
    OcrRegion,
    RegionObservation,
)
from game_design_knowledge.recording import record_build_revisions
from game_design_knowledge.review import apply_review_action, plan_review_action

try:
    from tests.document_fixtures import png_bytes, write_docx_with_image
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import png_bytes, write_docx_with_image


DOCUMENT = "docs/docx/loop.docx"
OTHER_DOCUMENT = "docs/docx/other.docx"
PARAGRAPH = "Tap the fire button to shoot"
NEXT_STEP = "next_step"
MIXED_ARROW_REGION = "region-3"
STEP_ARROW_REGION = "region-1"


def region(
    index: int, text: str, x: float, y: float, width: float = 80.0
) -> OcrRegion:
    return OcrRegion(
        index=index,
        text=text,
        bbox=BoundingBox(x, y, width, 20.0),
        reading_order=index,
        text_confidence=0.93,
        region_confidence=0.97,
        language="eng",
    )


def flow_observation() -> RegionObservation:
    """Text, one arrow that agrees with its neighbours, one arrow that does not."""

    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status="succeeded",
        regions=(
            region(0, "Start round", 100.0, 0.0),
            region(1, DOWN_ARROW, 135.0, 30.0, width=10.0),
            region(2, "Score a point", 100.0, 60.0),
            region(3, BOTH_WAYS, 135.0, 90.0, width=10.0),
            region(4, "End round", 100.0, 120.0),
        ),
        language="eng",
    )


# Kept in one place so the glyphs are readable in the source and checkable in a
# test: a lone down arrow, and a block that points both ways.
DOWN_ARROW = "\u2193"
BOTH_WAYS = "\u2193\u2191"


def build_project(root: Path, *, text: str = PARAGRAPH) -> Path:
    """Write the document, index it, and register its revisions."""

    index_directory = root / ".index" / "knowledge"
    write_docx_with_image(root / DOCUMENT, text, png_bytes())
    index_documents(
        root,
        index_directory,
        ocr_engine="rapidocr",
        ocr_providers={"rapidocr": lambda path: flow_observation()},
    )
    record_build_revisions(root, index_directory)
    return index_directory


def apply_action(state: Any, index: Any = None, **intent: Any) -> dict[str, Any]:
    """Plan and apply one review action, as an operator holding the token does."""

    plan = plan_review_action(state, index, **intent)
    return apply_review_action(
        state, index, plan_token=str(plan["plan_token"]), **intent
    )
