"""Reading order, indentation, and the vertical-arrow rules (issue #20).

Every sample here comes from the notation spec: a straight flow, a blank line,
indentation, a branch, a run of arrows, a missing endpoint, and a two-column
sheet. The point of each test is that geometry is *earned*: the confirmed case
is confirmed for the stated reason, and every other case says which condition
failed.
"""

from __future__ import annotations

import unittest

from game_design_knowledge.flow_notation import (
    CLAIM_BOUNDARY,
    CANDIDATE,
    CONFIRMED,
    NEXT_STEP,
    POINTS_TO,
    UNCERTAINTY_AMBIGUOUS_DIRECTION,
    UNCERTAINTY_BRANCH,
    UNCERTAINTY_CONSECUTIVE_ARROW,
    UNCERTAINTY_CROSS_COLUMN,
    UNCERTAINTY_MISSING_ENDPOINT,
    build_relations,
    summarize_relations,
)
from game_design_knowledge.layout import (
    ARROW_KIND,
    TEXT_KIND,
    UNCERTAINTY_MISSING_GEOMETRY,
    UNCERTAINTY_OVERLAPPING_BOXES,
    arrow_direction,
    build_reading_order,
)
from game_design_knowledge.ocr_regions import BoundingBox, OcrRegion


def region(
    index: int,
    text: str,
    x: float,
    y: float,
    width: float = 120.0,
    height: float = 20.0,
    confidence: float | None = 0.9,
) -> OcrRegion:
    return OcrRegion(
        index=index,
        text=text,
        bbox=BoundingBox(x, y, width, height),
        reading_order=index,
        text_confidence=confidence,
    )


class ArrowDirectionTests(unittest.TestCase):
    def test_only_an_arrow_only_block_becomes_an_arrow_element(self) -> None:
        self.assertEqual(arrow_direction("↓"), "down")
        self.assertEqual(arrow_direction(" -> "), "right")
        self.assertEqual(arrow_direction("↓↓"), "down")
        self.assertEqual(
            arrow_direction("↓↑"),
            "mixed",
            "arrows that disagree are a layout fact, not a direction",
        )
        self.assertEqual(
            arrow_direction("100 -> 200"),
            "",
            "text that happens to contain an arrow is text",
        )
        self.assertEqual(arrow_direction(""), "")


class ReadingOrderTests(unittest.TestCase):
    def test_rows_break_on_vertical_overlap_and_read_left_to_right(self) -> None:
        order = build_reading_order(
            (
                region(0, "right of row one", x=200.0, y=0.0),
                region(1, "left of row one", x=0.0, y=2.0),
                region(2, "row two", x=0.0, y=40.0),
            )
        )

        self.assertEqual(
            [element.region_index for element in order.elements], [1, 0, 2]
        )
        self.assertEqual(order.column_count, 1)
        self.assertEqual(order.uncertainty, "")
        self.assertEqual(order.geometry_confidence, 1.0)

    def test_two_columns_are_only_claimed_when_they_share_a_row(self) -> None:
        side_by_side = build_reading_order(
            (
                region(0, "左列第一行", x=0.0, y=0.0, width=100.0),
                region(1, "右列第一行", x=300.0, y=0.0, width=100.0),
                region(2, "左列第二行", x=0.0, y=30.0, width=100.0),
                region(3, "右列第二行", x=300.0, y=30.0, width=100.0),
            )
        )

        self.assertEqual(side_by_side.column_count, 2)
        self.assertEqual(
            [element.region_index for element in side_by_side.elements],
            [0, 2, 1, 3],
            "a column is read to the bottom before the next one starts",
        )
        self.assertEqual(side_by_side.geometry_confidence, 1.0)

        stacked = build_reading_order(
            (
                region(0, "顶格", x=0.0, y=0.0, width=100.0),
                region(1, "缩进", x=200.0, y=30.0, width=100.0),
            )
        )
        self.assertEqual(
            stacked.column_count,
            1,
            "rows that never share a row band are indentation, not columns",
        )

    def test_indentation_is_a_depth_hint_and_never_a_relation(self) -> None:
        order = build_reading_order(
            (
                region(0, "一级", x=10.0, y=0.0),
                region(1, "二级", x=30.0, y=30.0),
                region(2, "三级", x=50.0, y=60.0),
                region(3, "回到一级", x=10.0, y=90.0),
            )
        )

        self.assertEqual(
            [element.depth_hint for element in order.elements], [0, 1, 2, 0]
        )
        self.assertEqual(
            [relation for relation in build_relations(order)],
            [],
            "indentation alone never creates a parent or next relation",
        )

    def test_missing_geometry_and_overlap_are_reported_separately(self) -> None:
        no_boxes = build_reading_order(
            (
                OcrRegion(index=0, text="a"),
                OcrRegion(index=1, text="b"),
            )
        )
        self.assertEqual(no_boxes.uncertainty, UNCERTAINTY_MISSING_GEOMETRY)
        self.assertEqual(no_boxes.order_source, "region_index")
        self.assertEqual(no_boxes.geometry_confidence, 0.4)
        self.assertEqual(
            [element.region_index for element in no_boxes.elements], [0, 1]
        )

        overlapping = build_reading_order(
            (
                region(0, "left", x=0.0, y=0.0, width=100.0),
                region(1, "right", x=20.0, y=0.0, width=100.0),
            )
        )
        self.assertEqual(overlapping.uncertainty, UNCERTAINTY_OVERLAPPING_BOXES)
        self.assertEqual(overlapping.geometry_confidence, 0.7)


class FlowNotationTests(unittest.TestCase):
    def test_a_straight_flow_confirms_one_next_step(self) -> None:
        order = build_reading_order(
            (
                region(0, "点击购买", x=100.0, y=0.0, width=80.0),
                region(1, "↓", x=135.0, y=30.0, width=10.0),
                region(2, "扣除钻石", x=100.0, y=60.0, width=80.0),
            )
        )

        relations = build_relations(order)

        self.assertEqual(len(relations), 1)
        relation = relations[0]
        self.assertEqual(relation.kind, NEXT_STEP)
        self.assertEqual(relation.status, CONFIRMED)
        self.assertEqual(
            (relation.source_region, relation.via_regions, relation.target_region),
            (0, (1,), 2),
            "a confirmed step points back at the arrow and both flow nodes",
        )
        self.assertEqual(relation.geometry_confidence, 1.0)
        self.assertEqual(relation.ocr_confidence, 0.9)
        self.assertIn("arrow", relation.geometry_basis)
        self.assertIn("visible layout only", relation.as_payload()["claim_boundary"])
        self.assertIn("causality", CLAIM_BOUNDARY)

    def test_a_blank_line_between_the_blocks_changes_nothing(self) -> None:
        order = build_reading_order(
            (
                region(0, "打开背包", x=0.0, y=0.0, width=80.0),
                region(1, "↓", x=35.0, y=40.0, width=10.0),
                region(2, "选择道具", x=0.0, y=90.0, width=80.0),
            )
        )

        relation = build_relations(order)[0]

        self.assertEqual(relation.status, CONFIRMED)
        self.assertEqual((relation.source_region, relation.target_region), (0, 2))

    def test_a_branch_is_a_candidate_and_says_why(self) -> None:
        order = build_reading_order(
            (
                region(0, "开始", x=40.0, y=0.0, width=200.0),
                region(1, "↓", x=60.0, y=30.0, width=10.0),
                region(2, "↓", x=200.0, y=30.0, width=10.0),
                region(3, "结果", x=40.0, y=60.0, width=200.0),
            )
        )

        relation = build_relations(order)[0]

        self.assertEqual(relation.status, CANDIDATE)
        self.assertEqual(
            relation.uncertainty,
            UNCERTAINTY_BRANCH,
            "two arrows side by side in one row are a branch, not one step",
        )
        self.assertEqual(relation.geometry_confidence, 0.5)

    def test_endpoints_of_a_branching_row_are_not_confirmed(self) -> None:
        order = build_reading_order(
            (
                region(0, "开始", x=60.0, y=0.0, width=80.0),
                region(1, "↓", x=90.0, y=30.0, width=10.0),
                region(2, "结果 A", x=0.0, y=60.0, width=80.0),
                region(3, "结果 B", x=160.0, y=60.0, width=80.0),
            )
        )

        relation = build_relations(order)[0]

        self.assertEqual(relation.status, CANDIDATE)
        self.assertEqual(relation.uncertainty, UNCERTAINTY_BRANCH)

    def test_consecutive_arrows_resolve_across_the_run_as_a_candidate(self) -> None:
        order = build_reading_order(
            (
                region(0, "第一段", x=0.0, y=0.0, width=80.0),
                region(1, "↓", x=35.0, y=30.0, width=10.0),
                region(2, "↓", x=35.0, y=60.0, width=10.0),
                region(3, "第二段", x=0.0, y=90.0, width=80.0),
            )
        )

        relations = build_relations(order)

        self.assertEqual(len(relations), 2, "one relation per arrow block")
        for relation in relations:
            self.assertEqual(relation.status, CANDIDATE)
            self.assertEqual(relation.uncertainty, UNCERTAINTY_CONSECUTIVE_ARROW)
            self.assertEqual(
                (relation.source_region, relation.target_region),
                (0, 3),
                "the endpoints are the text blocks around the whole run",
            )

    def test_a_decorative_arrow_without_an_endpoint_is_never_confirmed(self) -> None:
        order = build_reading_order(
            (
                region(0, "示意图", x=0.0, y=0.0, width=80.0),
                region(1, "↓", x=35.0, y=60.0, width=10.0),
            )
        )

        relation = build_relations(order)[0]

        self.assertEqual(relation.status, CANDIDATE)
        self.assertEqual(relation.uncertainty, UNCERTAINTY_MISSING_ENDPOINT)
        self.assertEqual(relation.target_region, -1)
        self.assertEqual(relation.geometry_confidence, 0.2)

    def test_an_arrow_between_two_columns_is_a_candidate(self) -> None:
        order = build_reading_order(
            (
                region(0, "左列文本", x=0.0, y=0.0, width=100.0),
                region(1, "右列文本", x=300.0, y=0.0, width=100.0),
                region(2, "↓", x=45.0, y=30.0, width=10.0),
                region(3, "右列下段", x=300.0, y=60.0, width=100.0),
            )
        )

        relation = build_relations(order)[0]

        self.assertEqual(relation.status, CANDIDATE)
        self.assertEqual(relation.uncertainty, UNCERTAINTY_CROSS_COLUMN)

    def test_arrows_that_disagree_are_reported_without_a_direction(self) -> None:
        order = build_reading_order((region(0, "↓↑", x=0.0, y=0.0, width=10.0),))

        relation = build_relations(order)[0]

        self.assertEqual(relation.status, CANDIDATE)
        self.assertEqual(relation.uncertainty, UNCERTAINTY_AMBIGUOUS_DIRECTION)
        self.assertEqual(relation.source_region, -1)
        self.assertEqual(relation.target_region, -1)

    def test_a_horizontal_arrow_records_which_block_points_at_which(self) -> None:
        order = build_reading_order(
            (
                region(0, "点击按钮", x=0.0, y=0.0, width=80.0),
                region(1, "→", x=100.0, y=2.0, width=20.0),
                region(2, "打开面板", x=140.0, y=0.0, width=80.0),
            )
        )

        relation = build_relations(order)[0]

        self.assertEqual(relation.kind, POINTS_TO)
        self.assertEqual(relation.status, CONFIRMED)
        self.assertEqual((relation.source_region, relation.target_region), (0, 2))

    def test_summary_counts_confirmed_and_uncertain_relations(self) -> None:
        order = build_reading_order(
            (
                region(0, "a", x=0.0, y=0.0, width=80.0),
                region(1, "↓", x=35.0, y=30.0, width=10.0),
                region(2, "b", x=0.0, y=60.0, width=80.0),
                region(3, "→", x=200.0, y=0.0, width=20.0),
            )
        )

        summary = summarize_relations(build_relations(order))

        self.assertEqual(summary["relations"], 2)
        self.assertEqual(summary["confirmed_relations"], 1)
        self.assertEqual(summary["candidate_relations"], 1)
        self.assertEqual(summary["next_step_relations"], 1)
        self.assertEqual(summary["points_to_relations"], 1)

    def test_an_arrow_element_is_not_a_flow_node(self) -> None:
        order = build_reading_order(
            (
                region(0, "上", x=0.0, y=0.0, width=80.0),
                region(1, "↓", x=35.0, y=30.0, width=10.0),
                region(2, "下", x=0.0, y=60.0, width=80.0),
            )
        )

        kinds = {element.region_index: element.kind for element in order.elements}

        self.assertEqual(kinds[1], ARROW_KIND)
        self.assertEqual(kinds[0], TEXT_KIND)
        self.assertEqual(kinds[2], TEXT_KIND)


if __name__ == "__main__":
    unittest.main()
