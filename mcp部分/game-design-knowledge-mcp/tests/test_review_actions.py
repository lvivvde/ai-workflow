"""V2-07: the writing side -- preview first, then an audit trail that lasts.

Every write in this module is supposed to be previewable, refusable, reversible
and harmless to the derived index. These tests hold it to that: a token that no
longer matches is refused, a correction keeps the old value on the record, a
rejection is undone by a later event rather than by editing history, and the
SQLite index and the source bytes come out of it all unchanged.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from game_design_knowledge.notation import (
    CANDIDATE_ONLY,
    RESOLVED,
    candidates_from_index,
    dictionary_view,
    resolve,
)
from game_design_knowledge.review import (
    ReviewError,
    apply_review_action,
    plan_review_action,
)
from game_design_knowledge.shared_index import SharedIndexRead
from game_design_knowledge.state import DurableState

try:
    from tests.notation_fixtures import (
        DOCUMENT,
        MIXED_ARROW_REGION,
        NEXT_STEP,
        apply_action,
        build_project,
    )
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from notation_fixtures import (
        DOCUMENT,
        MIXED_ARROW_REGION,
        NEXT_STEP,
        apply_action,
        build_project,
    )


def confirm_intent(**overrides: object) -> dict[str, object]:
    intent: dict[str, object] = {
        "action": "confirm",
        "notation_token": NEXT_STEP,
        "meaning": "advance to the next state",
        "document": DOCUMENT,
        "region": MIXED_ARROW_REGION,
        "reason": "the legend printed beside this picture",
        "actor": "alice",
    }
    intent.update(overrides)
    return intent


class ReviewActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-review-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = build_project(self.root)
        self.database_path = self.index_directory / "knowledge.sqlite"
        self.state = DurableState(self.root)

    def _index(self) -> SharedIndexRead:
        return SharedIndexRead(self.database_path)

    def _entries(self) -> list[dict[str, object]]:
        return self.state.read_notation()["entries"]

    def _entry(self, status: str = "confirmed") -> dict[str, object]:
        matches = [
            entry for entry in self._entries() if str(entry["status"]) == status
        ]
        self.assertEqual(len(matches), 1, f"expected one {status} entry")
        return matches[0]

    def _confirm(self, index: SharedIndexRead, **overrides: object) -> dict[str, object]:
        return apply_action(self.state, index, **confirm_intent(**overrides))

    def test_applying_needs_the_token_the_preview_returned(self) -> None:
        with self._index() as index:
            intent = confirm_intent()
            with self.assertRaises(ReviewError):
                apply_review_action(self.state, index, plan_token="", **intent)
            with self.assertRaises(ReviewError):
                apply_review_action(self.state, index, plan_token="0" * 64, **intent)
            self.assertEqual(self._entries(), [])
            self.assertEqual(self.state.review_events(), [])

            plan = plan_review_action(self.state, index, **intent)
            self.assertEqual(plan["status"], "confirmation_required")
            self.assertTrue(plan["requires_plan_token"])
            self.assertEqual(plan["writes"], ["notation.json", "journal/review_events.jsonl"])
            self.assertFalse(plan["derived_knowledge_index_modified"])
            self.assertFalse(plan["source_evidence_modified"])
            self.assertFalse(plan["propagates_beyond_scope"])
            self.assertIsNone(plan["preview"]["before"])
            self.assertEqual(
                plan["preview"]["after"]["meaning"], "advance to the next state"
            )
            self.assertEqual(plan["preview"]["after"]["basis"]["reason"], intent["reason"])
            self.assertEqual(self._entries(), [], "planning alone writes nothing")

            applied = apply_review_action(
                self.state, index, plan_token=str(plan["plan_token"]), **intent
            )
            self.assertEqual(applied["status"], "applied")
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["entry"]["status"], "confirmed")
            self.assertEqual(applied["review_event"]["actor"], "alice")
            self.assertEqual(
                applied["review_event"]["payload"]["reason"], intent["reason"]
            )
            self.assertFalse(applied["derived_knowledge_index_modified"])
            self.assertEqual(len(self._entries()), 1)

    def test_a_token_stops_matching_once_the_dictionary_moves(self) -> None:
        with self._index() as index:
            first_intent = confirm_intent()
            first = plan_review_action(self.state, index, **first_intent)
            second_intent = confirm_intent(
                scope_kind="project",
                document="",
                region="",
                meaning="restart the round",
            )
            second = plan_review_action(self.state, index, **second_intent)
            apply_review_action(
                self.state,
                index,
                plan_token=str(second["plan_token"]),
                **second_intent,
            )
            with self.assertRaises(ReviewError):
                apply_review_action(
                    self.state, index, plan_token=str(first["plan_token"]), **first_intent
                )
            self.assertEqual(len(self._entries()), 1)
            self.assertEqual(len(self.state.review_events()), 1)

    def test_a_correction_keeps_the_old_value_on_the_record(self) -> None:
        with self._index() as index:
            self._confirm(index)
            original = self._entry()
            intent = {
                "action": "correct",
                "entry_id": str(original["entry_id"]),
                "meaning": "move to the next round",
                "reason": "the legend was re-read against the picture",
                "actor": "alice",
            }
            plan = plan_review_action(self.state, index, **intent)
            self.assertEqual(
                plan["preview"]["before"]["meaning"], "advance to the next state"
            )
            self.assertEqual(plan["preview"]["after"]["meaning"], "move to the next round")
            self.assertEqual(
                plan["preview"]["after"]["supersedes"], original["entry_id"]
            )

            applied = apply_review_action(
                self.state, index, plan_token=str(plan["plan_token"]), **intent
            )
            self.assertEqual(applied["entry"]["meaning"], "move to the next round")
            statuses = sorted(str(entry["status"]) for entry in self._entries())
            self.assertEqual(statuses, ["confirmed", "superseded"])
            superseded = self._entry("superseded")
            self.assertEqual(superseded["meaning"], "advance to the next state")
            self.assertEqual(superseded["superseded_by"], applied["entry"]["entry_id"])

            events = self.state.review_events()
            self.assertEqual([event.action for event in events], ["confirm", "correct"])
            payload = events[-1].payload
            self.assertEqual(payload["before"]["meaning"], "advance to the next state")
            self.assertEqual(payload["after"]["meaning"], "move to the next round")
            self.assertEqual(payload["reason"], intent["reason"])
            self.assertFalse(payload["source_evidence_modified"])

            after = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(after["resolved"]["meaning"], "move to the next round")
            view = dictionary_view(self.state, index, include_history=True)
            self.assertEqual(view["dictionary"]["counts"]["superseded"], 1)
            self.assertEqual(len(view["history"]), 1)

    def test_a_rejection_stops_the_reading_and_a_later_event_brings_it_back(
        self,
    ) -> None:
        with self._index() as index:
            self._confirm(index)
            entry = self._entry()
            rejected = apply_action(
                self.state,
                index,
                action="reject",
                entry_id=str(entry["entry_id"]),
                reason="the legend belongs to a different picture",
                actor="alice",
            )
            self.assertEqual(rejected["entry"]["status"], "rejected")
            self.assertFalse(rejected["derived_knowledge_index_modified"])

            stopped = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(stopped["status"], CANDIDATE_ONLY)
            self.assertIsNone(stopped["resolved"])
            self.assertFalse(stopped["supports_project_fact"])

            again = self._confirm(index, reason="the legend does cover this picture")
            self.assertEqual(again["status"], "applied")
            revived = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(revived["status"], RESOLVED)
            self.assertTrue(revived["supports_project_fact"])
            self.assertEqual(
                sorted(str(item["status"]) for item in self._entries()),
                ["confirmed", "rejected"],
            )
            self.assertEqual(
                [event.action for event in self.state.review_events()],
                ["confirm", "reject", "confirm"],
            )

    def test_rejecting_a_candidate_writes_only_the_journal(self) -> None:
        with self._index() as index:
            candidate = candidates_from_index(index, project_root=self.root, document=DOCUMENT)[0]
            intent = {
                "action": "reject",
                "notation_token": NEXT_STEP,
                "document": DOCUMENT,
                "region": MIXED_ARROW_REGION,
                "candidate_id": candidate["candidate_id"],
                "reason": "the arrow block is a layout artefact, not notation",
                "actor": "alice",
            }
            plan = plan_review_action(self.state, index, **intent)
            self.assertEqual(plan["writes"], ["journal/review_events.jsonl"])
            self.assertEqual(plan["candidate"]["candidate_id"], candidate["candidate_id"])
            applied = apply_review_action(
                self.state, index, plan_token=str(plan["plan_token"]), **intent
            )
            self.assertIsNone(applied["entry"])
            self.assertEqual(self._entries(), [])
            self.assertEqual(len(self.state.review_events()), 1)

            view = dictionary_view(self.state, index, document=DOCUMENT)
            derived = view["candidates"][0]
            self.assertTrue(derived["rejected"])
            self.assertFalse(derived["supports_project_fact"])
            settled = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(settled["status"], "not_found")
            self.assertIsNone(settled["resolved"])
            self.assertFalse(settled["supports_project_fact"])

    def test_ignoring_a_candidate_silences_it_without_confirming_anything(
        self,
    ) -> None:
        with self._index() as index:
            candidate = candidates_from_index(index, project_root=self.root, document=DOCUMENT)[0]
            applied = apply_action(
                self.state,
                index,
                action="ignore",
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                candidate_id=candidate["candidate_id"],
                reason="we already know this picture, stop asking",
                actor="alice",
            )
            self.assertEqual(applied["action"], "ignore")
            self.assertEqual(self._entries(), [])
            view = dictionary_view(self.state, index, document=DOCUMENT)
            derived = view["candidates"][0]
            self.assertTrue(derived["ignored"])
            self.assertNotIn("rejected", derived)
            self.assertFalse(derived["supports_project_fact"])
            settled = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(settled["status"], "not_found")
            self.assertIsNone(settled["resolved"])

    def test_the_derived_index_and_the_source_survive_every_action(self) -> None:
        database_before = self.database_path.read_bytes()
        document_before = (self.root / DOCUMENT).read_bytes()
        with self._index() as index:
            candidate = candidates_from_index(index, project_root=self.root, document=DOCUMENT)[0]
            results = [
                self._confirm(index),
                apply_action(
                    self.state,
                    index,
                    action="correct",
                    entry_id=str(self._entry()["entry_id"]),
                    meaning="move to the next round",
                    reason="re-read against the picture",
                    actor="alice",
                ),
                apply_action(
                    self.state,
                    index,
                    action="reject",
                    entry_id=str(self._entry()["entry_id"]),
                    reason="that was the wrong legend",
                    actor="alice",
                ),
                self._confirm(index, meaning="move to the next round"),
                apply_action(
                    self.state,
                    index,
                    action="ignore",
                    notation_token=NEXT_STEP,
                    document=DOCUMENT,
                    region=MIXED_ARROW_REGION,
                    candidate_id=candidate["candidate_id"],
                    reason="stop offering this picture",
                    actor="alice",
                ),
            ]
        for result in results:
            self.assertFalse(result["derived_knowledge_index_modified"])
            self.assertFalse(result["source_evidence_modified"])

        self.assertEqual(self.database_path.read_bytes(), database_before)
        self.assertEqual((self.root / DOCUMENT).read_bytes(), document_before)
        with self._index() as index:
            images = index.fetchone("SELECT COUNT(*) AS total FROM images")
            relations = index.fetchone(
                "SELECT COUNT(*) AS total FROM structural_relations"
            )
        self.assertEqual(images["total"], 1)
        self.assertEqual(relations["total"], 2)
        self.assertEqual(
            [event.action for event in self.state.review_events()],
            ["confirm", "correct", "reject", "confirm", "ignore"],
        )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
