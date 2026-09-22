from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from game_design_knowledge.revisions import ProcessingManifest, ProcessingStage
from game_design_knowledge.state import (
    DurableState,
    StateError,
    StateLocked,
    StateVersionError,
)


def default_manifest() -> ProcessingManifest:
    return ProcessingManifest(
        stages=(ProcessingStage(name="source_parse", ruleset_version="v1"),)
    )


class DurableStateTests(unittest.TestCase):
    def test_changed_bytes_create_a_new_source_and_parse_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()

            first = state.archive_source("docs/玩法.docx", b"first bytes")
            second = state.archive_source("docs/玩法.docx", b"second bytes")

            self.assertNotEqual(first.source_revision_id, second.source_revision_id)
            self.assertEqual(len(state.source_revisions()), 2)
            self.assertNotEqual(first.archive_path, second.archive_path)
            self.assertTrue(
                (state.directory / first.archive_path).is_file(),
                "the first revision's bytes stay archived after a change",
            )

            first_parse = state.record_parse_revision(
                first.source_revision_id,
                database_schema_version=3,
                processing_manifest=default_manifest(),
            )
            second_parse = state.record_parse_revision(
                second.source_revision_id,
                database_schema_version=3,
                processing_manifest=default_manifest(),
            )

            self.assertNotEqual(first_parse.parse_revision_id, second_parse.parse_revision_id)
            self.assertEqual(len(state.parse_revisions()), 2)
            active = state.active_parse_revision()
            self.assertEqual(
                active[first.document_id].parse_revision_id,
                second_parse.parse_revision_id,
                "the newest parse revision becomes active without deleting history",
            )

    def test_identical_bytes_are_archived_once_but_still_journalled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()

            first = state.archive_source("docs/玩法.docx", b"same bytes")
            second = state.archive_source("docs/玩法.docx", b"same bytes")

            self.assertEqual(first.source_revision_id, second.source_revision_id)
            self.assertEqual(state.status()["archived_objects"], 1)
            self.assertEqual(len(state.source_revisions()), 2)

    def test_review_events_and_dictionary_survive_a_deleted_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            index_directory = project_root / ".index" / "knowledge"
            index_directory.mkdir(parents=True)
            (index_directory / "knowledge.sqlite").write_bytes(b"derived bytes")

            state = DurableState(project_root)
            state.initialize()
            state.confirm_alias(
                "skill_dash",
                "冲刺",
                "冲锋",
                source="catalog.json",
                confirmed_by="策划",
            )
            state.append_review_event(
                "accept_transcription",
                subject_type="ocrized_text",
                subject_id="image:12",
                actor="策划",
            )
            document, _ = state.register_document("docs/玩法.docx")

            shutil.rmtree(index_directory)
            self.assertFalse(index_directory.exists())

            reopened = DurableState(project_root)
            dictionary = reopened.read_dictionary()
            self.assertEqual(dictionary["entries"][0]["aliases"][0]["name"], "冲锋")
            self.assertEqual(
                [event.action for event in reopened.review_events()],
                ["confirm_alias", "accept_transcription"],
            )
            self.assertEqual(
                reopened.documents()[document.document_id].relative_path,
                "docs/玩法.docx",
            )

    def test_dictionary_rejects_an_unconfirmed_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()

            with self.assertRaises(StateError):
                state.write_dictionary(
                    {
                        "entries": [
                            {
                                "feature_key": "skill_dash",
                                "canonical_name": "冲刺",
                                "aliases": [{"name": "冲锋"}],
                            }
                        ]
                    }
                )
            with self.assertRaises(StateError):
                state.confirm_alias(
                    "skill_dash",
                    "冲刺",
                    "",
                    source="catalog.json",
                    confirmed_by="策划",
                )

    def test_a_newer_state_schema_is_reported_instead_of_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state = DurableState(root)
            state.initialize()
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            manifest["state_schema_version"] = 99
            manifest["documents"]["doc-future"] = {"document_id": "doc-future"}
            state.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaises(StateVersionError) as raised:
                state.load()
            self.assertIn("99", str(raised.exception))

            before = state.manifest_path.read_text(encoding="utf-8")
            with self.assertRaises(StateVersionError):
                state.register_document("docs/新文档.docx")
            self.assertEqual(
                state.manifest_path.read_text(encoding="utf-8"),
                before,
                "a newer state file is never rewritten by an older reader",
            )

    def test_initialize_never_silently_overwrites_and_force_keeps_a_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()
            state.archive_source("docs/玩法.docx", b"bytes")

            with self.assertRaises(StateError):
                state.initialize()

            reset = state.initialize(force=True)
            self.assertEqual(reset.state_schema_version, 1)
            self.assertTrue(list(state.backup_directory.glob("pre-init-*")))
            self.assertEqual(state.source_revisions(), [])

    def test_unknown_source_revision_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()

            with self.assertRaises(StateError):
                state.record_parse_revision(
                    "src-does-not-exist",
                    database_schema_version=3,
                    processing_manifest=default_manifest(),
                )

    def test_second_writer_is_locked_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()

            with state.lock():
                other_process = DurableState(Path(temporary_directory))
                with self.assertRaises(StateLocked):
                    with other_process.lock(timeout=0.0):
                        pass
            with state.lock():
                pass

    def test_published_bundle_verifies_hash_manifest_and_locators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()
            source = state.archive_source("docs/玩法.docx", b"bytes")
            parse = state.record_parse_revision(
                source.source_revision_id,
                database_schema_version=3,
                processing_manifest=default_manifest(),
            )
            payload = {
                "retrieval_units": [
                    {
                        "unit_id": "evidence:1",
                        "unit_type": "paragraph",
                        "locator": {"paragraph_index": 0},
                    }
                ]
            }

            bundle = state.write_bundle(parse.parse_revision_id, payload)

            verified = state.verify_bundle(bundle["bundle_id"])
            self.assertTrue(verified["ok"], verified["checks"])
            self.assertEqual(verified["parse_revision_id"], parse.parse_revision_id)

            payload_path = Path(bundle["directory"]) / "payload.json"
            tampered = json.loads(payload_path.read_text(encoding="utf-8"))
            tampered["retrieval_units"][0]["locator"] = {"paragraph_index": 7}
            payload_path.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertFalse(state.verify_bundle(bundle["bundle_id"])["ok"])

    def test_bundle_without_locators_is_never_a_valid_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = DurableState(Path(temporary_directory))
            state.initialize()
            source = state.archive_source("docs/玩法.docx", b"bytes")
            parse = state.record_parse_revision(
                source.source_revision_id,
                database_schema_version=3,
                processing_manifest=default_manifest(),
            )
            state.write_bundle(parse.parse_revision_id, {"retrieval_units": []})

            verified = state.verify_bundle(state.bundle_ids()[0])
            self.assertFalse(verified["ok"])
            self.assertIn(
                "retrieval_unit_locators", [check["check"] for check in verified["checks"]]
            )


if __name__ == "__main__":
    unittest.main()
