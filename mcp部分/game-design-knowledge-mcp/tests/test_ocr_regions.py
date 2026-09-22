"""The transcription boundary, the confidence layers, and critical tokens.

Every test here is about a promise that would be expensive to break silently:
raw text survives normalization, confidence stays three numbers, OCR never
claims source-evidence status, and a dropped negation is visible on its own.
"""

from __future__ import annotations

import unittest

from game_design_knowledge.ocr_normalization import (
    CRITICAL_TOKEN_KINDS,
    NormalizationError,
    apply_changes,
    extract_critical_tokens,
    normalize_transcription,
    score_critical_tokens,
)
from game_design_knowledge.ocr_regions import (
    AggregateConfidenceError,
    BoundingBox,
    DEFAULT_QUALITY_GATE,
    EvidenceBoundaryError,
    MACHINE_SUPPORTED_EVIDENCE_STATE,
    OcrRegion,
    QualityGate,
    TRANSCRIPTION_EVIDENCE_STATE,
    assert_separate_confidence,
    assert_transcription_boundary,
    classify_outcome,
    gate_failures,
    key_mark_confidence,
    region_key_mark_confidence,
    summarize_outcomes,
)


def region(
    text: str,
    text_confidence: float | None = 0.9,
    region_confidence: float | None = None,
) -> OcrRegion:
    return OcrRegion(
        index=0,
        text=text,
        text_confidence=text_confidence,
        region_confidence=region_confidence,
    )


class NormalizationTests(unittest.TestCase):
    def test_the_raw_transcription_is_never_overwritten(self) -> None:
        raw = "等级上限 １ 0 0 ，成功率 50 %"

        suggestion = normalize_transcription(raw)

        self.assertEqual(suggestion.raw, raw)
        self.assertNotEqual(suggestion.normalized, raw)
        self.assertTrue(suggestion.changed)
        self.assertEqual(
            apply_changes(suggestion.raw, suggestion.changes),
            suggestion.normalized,
            "the recorded edits replay onto the raw text exactly",
        )

    def test_every_change_carries_a_reason_confidence_and_evidence(self) -> None:
        # One rule fires here, so every span points straight at the raw text.
        suggestion = normalize_transcription("等级上限 １００")

        self.assertTrue(suggestion.changes)
        for change in suggestion.changes:
            payload = change.as_payload()
            self.assertTrue(payload["rule"])
            self.assertTrue(payload["reason"])
            self.assertTrue(payload["evidence"])
            self.assertIsInstance(payload["rule_confidence"], float)
            self.assertEqual(
                payload["before"],
                suggestion.raw[payload["start"] : payload["end"]],
                "the span points at the text it claims to describe",
            )

    def test_untouched_text_produces_no_changes(self) -> None:
        suggestion = normalize_transcription("玩家等级上限 100")

        self.assertFalse(suggestion.changed)
        self.assertEqual(suggestion.changes, ())
        self.assertEqual(suggestion.normalized, suggestion.raw)

    def test_an_edit_that_does_not_match_its_own_span_is_rejected(self) -> None:
        suggestion = normalize_transcription("１００")
        change = suggestion.changes[0]

        with self.assertRaises(NormalizationError):
            apply_changes("abc", (change,))

    def test_rules_can_be_selected_by_name(self) -> None:
        suggestion = normalize_transcription("１００ 级 别", rules=["fullwidth_ascii"])

        self.assertEqual(suggestion.normalized, "100 级 别")


class CriticalTokenTests(unittest.TestCase):
    def test_each_kind_of_critical_token_is_named(self) -> None:
        text = "等级 ≤ 100，成功率 50%，ID_12 -> 下一关，不可跳过，攻击 +3.5"

        kinds = {token.kind for token in extract_critical_tokens(text)}

        self.assertEqual(kinds, set(CRITICAL_TOKEN_KINDS))

    def test_an_identifier_is_not_split_into_a_word_and_a_number(self) -> None:
        tokens = extract_critical_tokens("ITEM_ID_01")

        self.assertEqual([(token.kind, token.text) for token in tokens], [("identifier", "ITEM_ID_01")])

    def test_a_signed_number_is_not_also_reported_as_an_operator(self) -> None:
        tokens = extract_critical_tokens("攻击 -3.5")

        self.assertEqual([(token.kind, token.text) for token in tokens], [("number", "-3.5")])

    def test_scoring_is_per_kind_and_never_a_single_number(self) -> None:
        scores = {
            score.kind: score
            for score in score_critical_tokens("等级 100 成功率 5%", "等级 100 成功率 50%")
        }

        self.assertEqual(scores["number"].accuracy, 1.0)
        self.assertEqual(scores["percent"].accuracy, 0.0)
        self.assertEqual(scores["percent"].missed, ("50%",))
        self.assertEqual(scores["percent"].spurious, ("5%",))
        self.assertIsNone(
            scores["arrow"].accuracy,
            "a kind with no reference token reports no accuracy instead of 0 or 1",
        )

    def test_a_dropped_negation_shows_up_on_the_negation_kind(self) -> None:
        scores = {
            score.kind: score
            for score in score_critical_tokens("可以跳过", "不可跳过")
        }

        self.assertEqual(scores["negation"].accuracy, 0.0)
        self.assertEqual(scores["negation"].missed, ("不可",))

    def test_arrow_spellings_compare_as_one_token(self) -> None:
        scores = {
            score.kind: score
            for score in score_critical_tokens("A -> B", "A → B")
        }

        self.assertEqual(scores["arrow"].accuracy, 1.0)


class ConfidenceLayerTests(unittest.TestCase):
    def test_a_region_reports_three_separate_confidences(self) -> None:
        payload = region("等级 100", text_confidence=0.9, region_confidence=0.8).as_payload()

        self.assertEqual(payload["text_confidence"], 0.9)
        self.assertEqual(payload["region_confidence"], 0.8)
        self.assertIsNone(payload["key_mark_confidence"])

    def test_a_single_aggregate_score_is_rejected(self) -> None:
        with self.assertRaises(AggregateConfidenceError):
            assert_separate_confidence({"text": "x", "confidence": 0.9})
        with self.assertRaises(AggregateConfidenceError):
            assert_separate_confidence({"regions": [{"score": 1}]})

    def test_the_key_mark_confidence_is_not_the_average(self) -> None:
        regions = (
            OcrRegion(index=0, text="等级上限 100", text_confidence=0.99),
            OcrRegion(index=1, text="攻击 -3.5", text_confidence=0.2),
        )

        self.assertEqual(key_mark_confidence(regions), 0.2)
        self.assertEqual(
            key_mark_confidence((OcrRegion(index=0, text="等级说明", text_confidence=0.99),)),
            None,
            "a region without a critical token does not set the key-mark layer",
        )

    def test_a_provider_key_mark_score_is_used_instead_of_the_text_score(self) -> None:
        region_with_own_score = OcrRegion(
            index=0,
            text="攻击 -3.5",
            text_confidence=0.95,
            key_mark_confidence=0.4,
        )

        self.assertEqual(region_key_mark_confidence(region_with_own_score), 0.4)
        self.assertEqual(key_mark_confidence((region_with_own_score,)), 0.4)
        self.assertEqual(
            region_key_mark_confidence(OcrRegion(index=0, text="等级说明", text_confidence=0.9)),
            None,
        )

    def test_a_shaky_critical_mark_fails_the_gate_a_clean_average_would_pass(self) -> None:
        clean_only = (
            OcrRegion(index=0, text="等级说明", text_confidence=0.99),
            OcrRegion(index=1, text="攻击数值", text_confidence=0.9),
        )
        with_mark = (
            OcrRegion(index=0, text="等级说明", text_confidence=0.99),
            OcrRegion(index=1, text="攻击 -3.5", text_confidence=0.2),
        )

        self.assertEqual(gate_failures(clean_only), ())
        self.assertEqual(
            gate_failures(with_mark), ("key_mark_confidence_below_threshold",)
        )
        self.assertEqual(
            classify_outcome("succeeded", with_mark).quality_status, "rejected"
        )

    def test_confidence_is_only_graded_where_the_engine_reports_it(self) -> None:
        unscored = (OcrRegion(index=0, text="等级 100", text_confidence=None),)

        self.assertEqual(gate_failures(unscored), ())
        self.assertEqual(classify_outcome("succeeded", unscored).quality_status, "accepted")


class OutcomeStateTests(unittest.TestCase):
    def test_success_that_clears_the_gate_is_machine_supported(self) -> None:
        outcome = classify_outcome("succeeded", (region("等级上限 100"),))

        self.assertEqual(outcome.execution_status, "succeeded")
        self.assertEqual(outcome.quality_status, "accepted")
        self.assertEqual(outcome.evidence_state, MACHINE_SUPPORTED_EVIDENCE_STATE)
        self.assertEqual(outcome.v1_status, "succeeded")
        self.assertTrue(outcome.machine_supported)

    def test_an_image_without_text_is_a_rejected_success(self) -> None:
        outcome = classify_outcome("succeeded", ())

        self.assertEqual(
            (outcome.execution_status, outcome.quality_status, outcome.reason_code),
            ("succeeded", "rejected", "no_text_detected"),
        )
        self.assertEqual(outcome.evidence_state, TRANSCRIPTION_EVIDENCE_STATE)

    def test_a_timeout_keeps_what_it_managed_to_read(self) -> None:
        partial = classify_outcome("timeout", (region("等级"),))
        empty = classify_outcome("timeout", ())

        self.assertEqual(partial.execution_status, "partial")
        self.assertEqual(partial.quality_status, "uncertain")
        self.assertEqual(partial.reason_code, "ocr_timeout")
        self.assertEqual(partial.v1_status, "succeeded")
        self.assertTrue(partial.retryable)
        self.assertEqual(empty.execution_status, "failed")
        self.assertEqual(empty.v1_status, "failed")

    def test_each_failure_kind_gets_its_own_state(self) -> None:
        expected = {
            "corrupt_image": ("failed", "rejected", "corrupt_image", "failed", False),
            "unsupported_format": (
                "failed",
                "rejected",
                "unsupported_image_format",
                "failed",
                False,
            ),
            "missing_language": (
                "unavailable",
                "rejected",
                "missing_language_pack",
                "unavailable",
                False,
            ),
            "models_missing": (
                "unavailable",
                "rejected",
                "models_not_installed",
                "unavailable",
                False,
            ),
            "unavailable": (
                "unavailable",
                "rejected",
                "no_usable_engine",
                "unavailable",
                False,
            ),
        }

        for provider_status, want in expected.items():
            with self.subTest(provider_status=provider_status):
                outcome = classify_outcome(provider_status)
                self.assertEqual(
                    (
                        outcome.execution_status,
                        outcome.quality_status,
                        outcome.reason_code,
                        outcome.v1_status,
                        outcome.retryable,
                    ),
                    want,
                )
                self.assertEqual(outcome.evidence_state, TRANSCRIPTION_EVIDENCE_STATE)
                self.assertTrue(
                    outcome.corrective_action,
                    "a reported failure also says what to do about it",
                )

    def test_an_unknown_provider_status_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            classify_outcome("probably_fine")

    def test_transcription_cannot_be_upgraded_to_source_evidence(self) -> None:
        for state in ("explicit", "verified"):
            with self.subTest(state=state):
                with self.assertRaises(EvidenceBoundaryError):
                    assert_transcription_boundary(state)
        with self.assertRaises(EvidenceBoundaryError):
            assert_transcription_boundary("probably-true")

    def test_the_gate_is_configurable(self) -> None:
        strict = QualityGate(min_text_confidence=0.99)

        self.assertEqual(
            classify_outcome("succeeded", (region("等级 100", 0.9),)).quality_status,
            "accepted",
        )
        rejected = classify_outcome("succeeded", (region("等级 100", 0.9),), gate=strict)
        self.assertEqual(rejected.quality_status, "rejected")
        self.assertIn("text_confidence_below_threshold", rejected.gate_failures)
        self.assertEqual(DEFAULT_QUALITY_GATE.min_text_confidence, 0.5)

    def test_low_quality_counts_only_text_that_failed_the_gate(self) -> None:
        summary = summarize_outcomes(
            (
                classify_outcome("succeeded", (region("等级 100", 0.2),)),
                classify_outcome("unavailable"),
                classify_outcome("corrupt_image"),
                classify_outcome("succeeded", (region("等级 100", 0.9),)),
            )
        )

        self.assertEqual(
            summary["low_quality_images"],
            1,
            "an unavailable engine and a corrupt image are their own categories",
        )
        self.assertEqual(summary["v1_unavailable"], 1)
        self.assertEqual(summary["v1_failed"], 1)
        self.assertEqual(summary["machine_supported_images"], 1)


class BoundingBoxTests(unittest.TestCase):
    def test_a_polygon_becomes_an_axis_aligned_box(self) -> None:
        box = BoundingBox.from_polygon([[10, 20], [30, 20], [30, 45], [10, 45]])

        self.assertEqual(box, BoundingBox(10.0, 20.0, 20.0, 25.0))
        self.assertEqual(box.as_payload()["height"], 25.0)

    def test_boxes_union_and_degrade_cleanly(self) -> None:
        self.assertEqual(
            BoundingBox(0, 0, 10, 10).union(BoundingBox(5, 5, 10, 10)),
            BoundingBox(0, 0, 15, 15),
        )
        self.assertIsNone(BoundingBox.from_polygon([]))
        self.assertIsNone(BoundingBox.from_payload({"x": "nope"}))


if __name__ == "__main__":
    unittest.main()
