"""The arrow block's own pixels, read as a direction (issue #28).

The transcription cannot separate "the engine read this arrow right" from "the
engine mirrored it": measured on the deployed core engine, a block drawn as
``↓↓`` comes back as ``↑↑`` with a text confidence of 0.58-0.87 while a real
``↑↑`` comes back with 0.77-0.89. The ink does separate them, so this file pins
down the measurement itself: which end the head is on, when this build refuses
to call a direction at all, and that a block is only read along the axis its
own shape supports.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

try:
    from tests.document_fixtures import arrow_png, png_bytes
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import arrow_png, png_bytes
from game_design_knowledge.ink import (
    INK_MARGIN,
    INK_MIN_PIXELS,
    INK_RULESET_VERSION,
    InkReading,
    arrow_ink_readings,
    read_arrow_ink,
)
from game_design_knowledge.layout import arrow_axis
from game_design_knowledge.ocr_regions import BoundingBox, OcrRegion


BIG_BOX = (60, 40, 120, 60)
VERTICAL_BOX = (59, 202, 60, 64)
WIDE_BOX = (100, 100, 120, 30)


class ArrowInkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def picture(self, direction: str, box: tuple[int, int, int, int]) -> Path:
        path = self.root / f"{direction}-{box[2]}x{box[3]}.png"
        path.write_bytes(arrow_png(direction, box=box, size=(320, 320)))
        return path

    def test_a_drawn_arrow_is_read_back_as_the_direction_it_points(self) -> None:
        for direction in ("up", "down", "left", "right"):
            box = VERTICAL_BOX if direction in ("up", "down") else WIDE_BOX
            with self.subTest(direction=direction):
                reading = self.read(direction, box)

                self.assertIsNotNone(reading)
                self.assertEqual(reading.direction, direction)
                self.assertEqual(
                    reading.axis,
                    "vertical" if direction in ("up", "down") else "horizontal",
                )

    def test_the_reading_says_where_the_widest_band_of_ink_sits(self) -> None:
        reading = self.read("down", VERTICAL_BOX)

        self.assertGreaterEqual(reading.share, 0.5 + INK_MARGIN)
        self.assertIn("down its box from the top", reading.basis)
        self.assertEqual(reading.as_payload()["ruleset_version"], INK_RULESET_VERSION)

    def test_the_engine_mirroring_a_down_arrow_does_not_change_the_pixels(self) -> None:
        """The exact shape issue #28 was filed about.

        A block drawn top-down is read by the engine as ``↑↑``; the ink still
        says down, which is what lets the relation layer refuse to confirm it.
        """

        path = self.picture("down", VERTICAL_BOX)
        region = OcrRegion(
            index=0,
            text="↑↑",
            bbox=BoundingBox(*VERTICAL_BOX),
            text_confidence=0.81,
        )

        readings = arrow_ink_readings(path, (region,))

        self.assertEqual(readings[0].direction, "down")

    def test_only_arrow_blocks_are_ever_looked_at(self) -> None:
        path = self.picture("down", VERTICAL_BOX)
        regions = (
            OcrRegion(
                index=0,
                text="扣除钻石",
                bbox=BoundingBox(*VERTICAL_BOX),
                text_confidence=0.99,
            ),
            OcrRegion(
                index=1,
                text="↑↑",
                bbox=BoundingBox(*VERTICAL_BOX),
                text_confidence=0.81,
            ),
        )

        readings = arrow_ink_readings(path, regions)

        self.assertEqual(sorted(readings), [1])

    def test_a_block_with_no_box_or_no_picture_yields_no_reading(self) -> None:
        path = self.picture("down", VERTICAL_BOX)

        self.assertEqual(
            arrow_ink_readings(
                path, (OcrRegion(index=0, text="↓"),)
            ),
            {},
            "a block with no geometry has no shape to measure",
        )
        self.assertEqual(
            arrow_ink_readings(
                self.root / "missing.png",
                (OcrRegion(index=0, text="↓", bbox=BoundingBox(*VERTICAL_BOX)),),
            ),
            {},
            "a picture this build cannot open is reported as no reading",
        )
        self.assertEqual(
            arrow_ink_readings(
                path,
                (
                    OcrRegion(
                        index=0,
                        text="↓",
                        bbox=BoundingBox(900, 900, 40, 40),
                    ),
                ),
            ),
            {},
            "a box outside the picture has no pixels to read",
        )

    def test_a_block_with_no_shape_in_it_is_never_given_a_direction(self) -> None:
        blank = self.root / "blank.png"
        blank.write_bytes(png_bytes(200, 200, colour=(255, 255, 255)))

        region = OcrRegion(
            index=0, text="↓", bbox=BoundingBox(40, 40, 80, 80)
        )

        self.assertEqual(
            arrow_ink_readings(blank, (region,)),
            {},
            f"under {INK_MIN_PIXELS} ink pixels is not a shape worth reading",
        )

    def test_a_uniform_block_is_too_symmetrical_to_call(self) -> None:
        """A filled box has its peak everywhere, so the band sits in the middle."""

        filled = self.root / "filled.png"
        filled.write_bytes(png_bytes(200, 200, colour=(0, 0, 0)))

        region = OcrRegion(index=0, text="↓", bbox=BoundingBox(40, 40, 80, 80))

        self.assertIsNone(
            read_arrow_ink(_open_image(filled), region.bbox, "vertical"),
            "a blob is not an arrow, and guessing one would be the whole bug",
        )

    def test_a_wide_block_is_not_read_along_the_vertical_axis(self) -> None:
        """A horizontal arrow block has a top-heavy row profile; shapes decide."""

        path = self.picture("right", WIDE_BOX)
        region = OcrRegion(
            index=0, text="↑", bbox=BoundingBox(*WIDE_BOX), text_confidence=0.9
        )

        self.assertEqual(
            arrow_ink_readings(path, (region,)),
            {},
            "a block wider than it is tall is not a block that points up",
        )

    def test_the_axis_the_transcription_claims_is_the_axis_that_is_measured(self) -> None:
        self.assertEqual(arrow_axis("↑↑"), "vertical")
        self.assertEqual(arrow_axis("→"), "horizontal")
        self.assertEqual(arrow_axis("↑→"), "", "arrows that disagree have no axis")
        self.assertEqual(arrow_axis("100 → 200"), "", "that is text, not a block")

    def test_a_reading_a_caller_states_has_to_be_a_real_direction(self) -> None:
        reading = InkReading.measured("down", 0.71)

        self.assertEqual((reading.axis, reading.share), ("vertical", 0.71))
        self.assertEqual(reading.as_payload()["direction"], "down")
        with self.assertRaises(ValueError):
            InkReading.measured("sideways", 0.5)

    def read(self, direction: str, box: tuple[int, int, int, int]) -> InkReading | None:
        path = self.picture(direction, box)
        region = OcrRegion(
            index=0,
            text="↓" if direction in ("up", "down") else "→",
            bbox=BoundingBox(*box),
        )
        return arrow_ink_readings(path, (region,)).get(0)


def _open_image(path: Path):
    from PIL import Image

    return Image.open(path)


if __name__ == "__main__":
    unittest.main()
