"""Which way an arrow block points, measured from the block's own pixels.

The transcription cannot answer this. While the corpus was built (issue #26)
the deployed core engine, RapidOCR 1.3.24, was measured on 30+ arrow renderings
(32-200px; msyh / msyhbd / simhei / simsun; drawn polygons; wide horizontal
spacing). A block drawn as ``↓↓`` comes back as ``↑↑`` with a text confidence
of 0.58-0.87, and a block that really is ``↑↑`` comes back as ``↑↑`` with
0.77-0.89. The two populations overlap, so *no* threshold on the transcription
separates "read it right" from "mirrored it" (issue #28).

The ink itself does separate them. Inside a tight box around an arrow, the
widest run of ink sits at the arrowhead, so "how far down the box the widest
band of ink sits" is a geometric fact about the picture. Measured on the same
4 fonts (msyh / msyhbd / simhei / simsun) x 5 sizes (40-140px):

    ↑ / ↑↑   0.16 - 0.33 of the box height   (head near the top)
    ↓ / ↓↓   0.65 - 0.82                    (head near the bottom)
    →        0.61 - 0.80 of the box width    (head near the right)
    ←        0.19 - 0.37                    (head near the left)

Two limits keep this honest:

* **It only speaks about polarity along one axis.** The engine's measured
  confusions are within a family (a vertical block read as the other vertical
  direction), never across one, and the cross-axis profile is not clean -- a
  ``→`` has a decisively *top-heavy row* profile (0.24-0.46), because the head
  triangle sits high above the stem. So the axis comes from the transcription
  (``layout.arrow_axis``), the block's own shape has to agree with it
  (``_axis_fits``), and the polarity is measured here.
* **A reading that is not clearly on one side of the middle is no reading.**
  How far a band sits must clear ``INK_MARGIN`` from the middle, so a
  decoration, a blob or an unlucky box yields nothing and the relation it
  belongs to is reported as unverified instead of half-confirmed.

Nothing here decodes a picture unless the caller asks about an arrow-only block,
and a picture this build cannot read yields no readings rather than an error:
the caller then has a transcription and no corroboration, which is exactly the
state the relation layer is asked to report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .layout import arrow_axis
from .ocr_regions import BoundingBox, OcrRegion


INK_RULESET_VERSION = "arrow-ink-v1"

#: A pixel darker than this counts as ink. Both 128 and 160 were measured; the
#: separation between the two polarities does not depend on which one is used.
INK_THRESHOLD = 128
#: Fewer ink pixels than this is not a shape worth reading.
INK_MIN_PIXELS = 16
#: How far the widest band of ink must sit from the middle of the box, as a
#: share of the box, before this build will call a direction at all. The
#: measured extremes clear it by a wide margin on a real arrow.
INK_MARGIN = 0.10
#: A band is every position whose ink count reaches this share of the peak.
_BAND_REACH = 0.9

_VERTICAL = "vertical"
_HORIZONTAL = "horizontal"
_AXES = (_VERTICAL, _HORIZONTAL)

_MIN_BOX_SIDE = 3


@dataclass(frozen=True)
class InkReading:
    """The ink inside one arrow block, reduced to a direction.

    ``share`` is where the widest band of ink sits along the axis, as a share of
    the box: ``0.21`` means a fifth of the way down a vertical box from its top.
    """

    direction: str
    axis: str
    share: float
    ruleset_version: str = INK_RULESET_VERSION

    @classmethod
    def measured(cls, direction: str, share: float) -> "InkReading":
        """A reading a caller already has, without a picture in hand.

        This is the seam the relation tests use: they need the *evidence* a
        real measurement would produce, not a second copy of the measurement.
        """

        if direction not in ("up", "down", "left", "right"):
            raise ValueError(f"not an arrow direction: {direction!r}")
        axis = _VERTICAL if direction in ("up", "down") else _HORIZONTAL
        return cls(direction=direction, axis=axis, share=float(share))

    @property
    def basis(self) -> str:
        """The measurement in the reader's own terms, for a claim to cite."""

        if self.axis == _VERTICAL:
            place = f"{self.share:.2f} of the way down its box from the top"
        else:
            place = f"{self.share:.2f} of the way across its box from the left"
        return f"the widest band of ink inside the block sits {place}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "axis": self.axis,
            "share": round(float(self.share), 6),
            "ruleset_version": self.ruleset_version,
        }


def read_arrow_ink(
    image: Any, bbox: BoundingBox | None, axis: str
) -> InkReading | None:
    """Read one arrow block's pointing direction out of its own pixels.

    ``None`` means the box holds no shape this build is willing to read a
    direction from: no geometry, a box outside the picture, almost no ink, or a
    band of ink that sits too close to the middle to call.
    """

    if bbox is None or axis not in _AXES:
        return None
    box = _clamp(bbox, image.size)
    if box is None or not _axis_fits(box, axis):
        return None
    return _direction_from(_profile(image, box, axis), axis)


def arrow_ink_readings(
    asset_path: Path, regions: Sequence[OcrRegion]
) -> dict[int, InkReading]:
    """Measure every arrow-only block in one picture, keyed by region index.

    Only blocks whose transcription is nothing but arrow glyphs are looked at,
    and only blocks with geometry: a text block's box has no pointing direction
    to read. A picture that is missing or cannot be decoded yields no readings,
    which leaves the relations that rest on those blocks unverified.
    """

    targets = [
        (region.index, region.bbox, arrow_axis(region.text)) for region in regions
    ]
    targets = [target for target in targets if target[2] and target[1] is not None]
    if not targets:
        return {}
    image = _open(asset_path)
    if image is None:
        return {}
    readings: dict[int, InkReading] = {}
    try:
        image.load()
        for index, bbox, axis in targets:
            reading = read_arrow_ink(image, bbox, axis)
            if reading is not None:
                readings[index] = reading
    except OSError:
        # A half-readable picture is a picture this build did not fully look
        # at; whatever was measured before the failure still stands.
        return readings
    finally:
        image.close()
    return readings


def _open(asset_path: Path) -> Any:
    """The picture, or ``None`` when this build cannot decode it."""

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is part of the core pack
        return None
    try:
        return Image.open(Path(asset_path))
    except OSError:
        return None


def _clamp(
    bbox: BoundingBox, size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """The part of a region's box that actually lies inside the picture."""

    width, height = int(size[0]), int(size[1])
    left = max(0, min(width, int(round(bbox.x))))
    top = max(0, min(height, int(round(bbox.y))))
    right = max(0, min(width, int(round(bbox.x + bbox.width))))
    bottom = max(0, min(height, int(round(bbox.y + bbox.height))))
    if right - left < _MIN_BOX_SIDE or bottom - top < _MIN_BOX_SIDE:
        return None
    return left, top, right - left, bottom - top


def _axis_fits(box: tuple[int, int, int, int], axis: str) -> bool:
    """Whether the block's shape agrees with the axis it was transcribed on.

    A vertical arrow block is at least as tall as it is wide, a horizontal one
    at least as wide as it is tall; a 60x64px ``↑↑`` pair is the closest a real
    block comes to square. The check matters because the *cross*-axis profile is
    not clean: a ``→`` block has a decisively top-heavy row profile (0.24-0.46
    across the same 4 fonts x 5 sizes), so a horizontal block the engine
    happened to read as ``↑`` would otherwise agree with its own ink for the
    wrong reason. Shapes that disagree get no reading, which leaves the relation
    unverified instead of confirmed.
    """

    _, _, width, height = box
    if axis == _VERTICAL:
        return height >= width
    return width >= height


def _profile(
    image: Any, box: tuple[int, int, int, int], axis: str
) -> list[int]:
    """Ink pixels per row (vertical) or per column (horizontal) inside a box."""

    left, top, width, height = box
    crop = image.crop((left, top, left + width, top + height)).convert("L")
    pixels = crop.load()
    if axis == _VERTICAL:
        return [
            sum(1 for x in range(width) if pixels[x, y] < INK_THRESHOLD)
            for y in range(height)
        ]
    return [
        sum(1 for y in range(height) if pixels[x, y] < INK_THRESHOLD)
        for x in range(width)
    ]


def _direction_from(profile: Sequence[int], axis: str) -> InkReading | None:
    """The direction a profile points, or ``None`` when it does not say."""

    total = sum(profile)
    peak = max(profile, default=0)
    if total < INK_MIN_PIXELS or peak <= 0:
        return None
    band = [index for index, count in enumerate(profile) if count >= _BAND_REACH * peak]
    share = (sum(band) / len(band)) / len(profile)
    if share <= 0.5 - INK_MARGIN:
        direction = "up" if axis == _VERTICAL else "left"
    elif share >= 0.5 + INK_MARGIN:
        direction = "down" if axis == _VERTICAL else "right"
    else:
        return None
    return InkReading(direction=direction, axis=axis, share=share)


__all__ = [
    "INK_MARGIN",
    "INK_MIN_PIXELS",
    "INK_RULESET_VERSION",
    "INK_THRESHOLD",
    "InkReading",
    "arrow_ink_readings",
    "read_arrow_ink",
]
