"""V2-07: what a token means, and how far that meaning is allowed to reach.

The reading side of the Designer Notation Dictionary. Every test here asks the
same kind of question a caller would: does this token mean anything in *this*
place, which source answers it, what stayed out of the answer, and did a stale
revision quietly inherit a decision nobody re-made?
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from game_design_knowledge.notation import (
    AMBIGUOUS,
    CANDIDATE_ONLY,
    QUARANTINED,
    RESOLVED,
    candidates_from_index,
    dictionary_view,
    migration_candidates,
    normalize_scope,
    resolve,
    validate_entry,
)
from game_design_knowledge.review import ReviewError, plan_review_action
from game_design_knowledge.shared_index import SharedIndexRead
from game_design_knowledge.state import DurableState, StateError

try:
    from tests.notation_fixtures import (
        DOCUMENT,
        MIXED_ARROW_REGION,
        NEXT_STEP,
        OTHER_DOCUMENT,
        STEP_ARROW_REGION,
        apply_action,
        build_project,
    )
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from notation_fixtures import (
        DOCUMENT,
        MIXED_ARROW_REGION,
        NEXT_STEP,
        OTHER_DOCUMENT,
        STEP_ARROW_REGION,
        apply_action,
        build_project,
    )


class NotationDictionaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-notation-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = build_project(self.root)
        self.database_path = self.index_directory / "knowledge.sqlite"
        self.state = DurableState(self.root)

    def _index(self) -> SharedIndexRead:
        return SharedIndexRead(self.database_path)

    def _confirmed_entry(self, authority: str) -> dict[str, object]:
        dictionary = self.state.read_notation()
        matches = [
            entry
            for entry in dictionary["entries"]
            if str(entry["authority"]) == authority
        ]
        self.assertEqual(len(matches), 1, f"expected one {authority} entry")
        return matches[0]

    def test_a_scope_is_normalised_and_can_never_leave_the_project(self) -> None:
        self.assertEqual(
            normalize_scope("project"), {"kind": "project", "value": "*"}
        )
        self.assertEqual(
            normalize_scope("region", document="docs\\docx\\loop.docx", region="region-3"),
            {
                "kind": "region",
                "value": "docs/docx/loop.docx#region-3",
                "document": "docs/docx/loop.docx",
                "region": "region-3",
            },
        )
        self.assertEqual(
            normalize_scope("document_type", "XLSX"),
            {"kind": "document_type", "value": "xlsx"},
        )
        for kind, value in (
            ("planet", "earth"),
            ("document", "C:/elsewhere/loop.docx"),
            ("document", "/etc/loop.docx"),
            ("document", "../../outside.docx"),
        ):
            with self.assertRaises(ValueError):
                normalize_scope(kind, value)
        with self.assertRaises(ValueError):
            normalize_scope("region", document=DOCUMENT)

    def test_a_disputed_arrow_stays_a_candidate_and_never_a_dictionary_entry(
        self,
    ) -> None:
        with self._index() as index:
            candidates = candidates_from_index(index, project_root=self.root, document=DOCUMENT)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["notation_token"], NEXT_STEP)
            self.assertEqual(candidate["region"], MIXED_ARROW_REGION)
            self.assertEqual(candidate["direction"], "mixed")
            self.assertEqual(candidate["uncertainty"], "ambiguous_direction")
            self.assertTrue(candidate["claim_boundary"])
            self.assertFalse(candidate["supports_project_fact"])
            self.assertTrue(candidate["requires_review_action"])
            self.assertEqual(
                candidate["scope"]["value"], f"{DOCUMENT}#{MIXED_ARROW_REGION}"
            )

            view = dictionary_view(self.state, index, document=DOCUMENT)
            self.assertEqual(view["dictionary"]["entries"], [])
            self.assertEqual(view["dictionary"]["counts"]["confirmed"], 0)
            self.assertTrue(view["candidates"])
            self.assertFalse(view["supports_project_fact"]["candidates"])

        unconfirmed = {
            "record_type": "notation_entry",
            "entry_schema_version": 1,
            "entry_id": "nota-unconfirmed",
            "notation_token": NEXT_STEP,
            "meaning": "whatever the model guessed",
            "scope": {"kind": "region", "value": f"{DOCUMENT}#region-3"},
            "authority": "candidate_interpretation",
            "status": "candidate",
            "confirmed_by": "",
            "confirmed_at": "",
        }
        with self.assertRaises(ValueError):
            validate_entry(unconfirmed)
        with self.assertRaises(StateError):
            self.state.write_notation({"entries": [unconfirmed]})
        self.assertEqual(self.state.read_notation()["entries"], [])

    def test_a_confirmed_meaning_stays_inside_the_region_it_was_made_for(
        self,
    ) -> None:
        with self._index() as index:
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="advance to the next state",
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="the legend printed beside this picture",
                actor="alice",
            )

            inside = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(inside["status"], RESOLVED)
            self.assertTrue(inside["supports_project_fact"])
            self.assertEqual(inside["resolved"]["meaning"], "advance to the next state")
            self.assertEqual(inside["resolved"]["authority"], "region_legend")

            neighbours = (
                {"document": DOCUMENT, "region": STEP_ARROW_REGION},
                {"document": OTHER_DOCUMENT, "region": MIXED_ARROW_REGION},
            )
            for query in neighbours:
                outside = resolve(
                    self.state,
                    index,
                    notation_token=NEXT_STEP,
                    document=query["document"],
                    region=query["region"],
                )
                self.assertIsNone(outside["resolved"])
                self.assertFalse(outside["supports_project_fact"])
                self.assertEqual(outside["status"], "not_found")

            # The same document without naming a region cannot borrow a regional
            # reading; the candidate for that picture is still offered, apart.
            anywhere = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
            )
            self.assertIsNone(anywhere["resolved"])
            self.assertFalse(anywhere["supports_project_fact"])
            self.assertEqual(anywhere["status"], CANDIDATE_ONLY)

    def test_a_document_type_scope_answers_that_type_and_no_other(self) -> None:
        with self._index() as index:
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="the step column of this workbook",
                scope_kind="document_type",
                scope_value="xlsx",
                reason="the sheet's own header row",
                actor="bob",
            )
            for_xlsx = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document="docs/xlsx/plan.xlsx",
                document_type="xlsx",
            )
            self.assertEqual(for_xlsx["status"], RESOLVED)
            self.assertEqual(for_xlsx["resolved"]["meaning"], "the step column of this workbook")
            self.assertEqual(
                for_xlsx["resolved"]["authority"], "document_type_definition"
            )

            for_docx = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                document_type="docx",
            )
            self.assertIsNone(for_docx["resolved"])
            self.assertFalse(for_docx["supports_project_fact"])

    def test_two_sources_that_disagree_need_a_recorded_resolution(self) -> None:
        with self._index() as index:
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="advance to the next state",
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="the legend printed beside this picture",
                actor="alice",
            )
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="restart the round",
                scope_kind="project",
                reason="a studio-wide convention from the style guide",
                actor="alice",
            )

            disputed = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(disputed["status"], AMBIGUOUS)
            self.assertTrue(disputed["requires_resolution"])
            self.assertIsNone(disputed["resolved"])
            self.assertFalse(disputed["supports_project_fact"])
            self.assertEqual(
                disputed["authority_winner"]["meaning"], "advance to the next state"
            )
            self.assertEqual(
                [conflict["authority"] for conflict in disputed["conflicts"]],
                ["region_legend", "project_dictionary"],
            )
            self.assertEqual(
                [layer["authority"] for layer in disputed["layers"]],
                ["region_legend", "project_dictionary"],
            )

            project_entry = self._confirmed_entry("project_dictionary")
            apply_action(
                self.state,
                index,
                action="resolve_conflict",
                entry_id=str(project_entry["entry_id"]),
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                resolution_kind="human_choice",
                reason="the studio list changed this quarter",
                actor="alice",
            )

            settled = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(settled["status"], RESOLVED)
            self.assertTrue(settled["supports_project_fact"])
            self.assertEqual(settled["resolution"]["kind"], "human_choice")
            self.assertEqual(settled["resolution"]["source_evidence_modified"], False)
            self.assertEqual(settled["resolved"]["meaning"], "restart the round")

            history = dictionary_view(self.state, index, include_history=True)
            superseded = [
                entry
                for entry in history["history"]
                if entry["status"] == "superseded"
            ]
            self.assertEqual(len(superseded), 1)
            self.assertEqual(superseded[0]["meaning"], "advance to the next state")

    def test_the_higher_source_can_be_recorded_as_the_authority(self) -> None:
        with self._index() as index:
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="advance to the next state",
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="the legend printed beside this picture",
                actor="alice",
            )
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="restart the round",
                scope_kind="project",
                reason="a studio-wide convention",
                actor="alice",
            )
            project_entry = self._confirmed_entry("project_dictionary")
            with self.assertRaises(ReviewError):
                plan_review_action(
                    self.state,
                    index,
                    action="resolve_conflict",
                    entry_id=str(project_entry["entry_id"]),
                    document=DOCUMENT,
                    region=MIXED_ARROW_REGION,
                    resolution_kind="authority",
                    reason="pretending the studio convention outranks the legend",
                    actor="alice",
                )

            legend_entry = self._confirmed_entry("region_legend")
            apply_action(
                self.state,
                index,
                action="resolve_conflict",
                entry_id=str(legend_entry["entry_id"]),
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="the legend beside the picture is the local authority",
                actor="alice",
            )
            settled = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(settled["status"], RESOLVED)
            self.assertEqual(settled["resolution"]["kind"], "authority")
            self.assertEqual(settled["resolution"]["winner_authority"], "region_legend")
            self.assertEqual(settled["resolution"]["loser_authorities"], ["project_dictionary"])
            self.assertEqual(settled["resolved"]["meaning"], "advance to the next state")

    def test_an_outside_reading_is_quarantined_next_to_the_project_s_own(self) -> None:
        with self._index() as index:
            offered = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                external_common_knowledge="arrows usually mean sequence",
            )
            self.assertEqual(offered["status"], CANDIDATE_ONLY)
            self.assertIsNone(offered["resolved"])
            self.assertFalse(offered["supports_project_fact"])
            self.assertEqual(
                offered["quarantined"]["meaning"], "arrows usually mean sequence"
            )
            self.assertFalse(offered["quarantined"]["supports_project_fact"])

            unknown = resolve(
                self.state,
                index,
                notation_token="legend_brace",
                document=DOCUMENT,
                external_common_knowledge="a brace groups alternatives",
            )
            self.assertEqual(unknown["status"], QUARANTINED)
            self.assertIsNone(unknown["resolved"])
            self.assertFalse(unknown["supports_project_fact"])

            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="advance to the next state",
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="the legend printed beside this picture",
                actor="alice",
            )
            confirmed = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                external_common_knowledge="arrows usually mean sequence",
            )
            self.assertEqual(confirmed["status"], RESOLVED)
            self.assertEqual(confirmed["resolved"]["meaning"], "advance to the next state")
            self.assertEqual(
                confirmed["quarantined"]["meaning"], "arrows usually mean sequence"
            )
            self.assertNotEqual(
                confirmed["resolved"]["meaning"], confirmed["quarantined"]["meaning"]
            )

    def test_a_new_revision_turns_the_old_confirmation_into_a_candidate(self) -> None:
        with self._index() as index:
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="advance to the next state",
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="the legend printed beside this picture",
                actor="alice",
            )
            before = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            first_revision = before["resolved"]["version"]["parse_revision_id"]
            self.assertTrue(first_revision)

        # The document changes, so a new revision is parsed and becomes active.
        build_project(self.root, text="Tap the jump button to jump")
        with self._index() as index:
            candidates = migration_candidates(self.state, document=DOCUMENT)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate["from_parse_revision"], first_revision)
            self.assertNotEqual(candidate["to_parse_revision"], first_revision)
            self.assertTrue(candidate["requires_review_event"])
            self.assertFalse(candidate["auto_applied"])
            self.assertFalse(candidate["supports_project_fact"])

            stale = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertIsNone(stale["resolved"])
            self.assertFalse(stale["supports_project_fact"])
            self.assertEqual(stale["status"], CANDIDATE_ONLY)
            self.assertTrue(
                any(
                    item["from_parse_revision"] == first_revision
                    for item in stale["migration_candidates"]
                )
            )

            # A new explicit review event is what makes it apply again.
            apply_action(
                self.state,
                index,
                action="confirm",
                notation_token=NEXT_STEP,
                meaning="advance to the next state",
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
                reason="re-confirmed on the new revision",
                actor="alice",
            )
            migrated = resolve(
                self.state,
                index,
                notation_token=NEXT_STEP,
                document=DOCUMENT,
                region=MIXED_ARROW_REGION,
            )
            self.assertEqual(migrated["status"], RESOLVED)
            self.assertTrue(migrated["supports_project_fact"])
            self.assertNotEqual(
                migrated["resolved"]["version"]["parse_revision_id"], first_revision
            )
            self.assertEqual(migration_candidates(self.state, document=DOCUMENT), [])
            history = self.state.read_notation()["entries"]
            self.assertEqual(
                sorted(str(entry["status"]) for entry in history),
                ["confirmed", "superseded"],
            )


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
