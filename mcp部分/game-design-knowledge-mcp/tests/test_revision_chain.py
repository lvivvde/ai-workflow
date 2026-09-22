from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from game_design_knowledge.freshness import freshness_report
from game_design_knowledge.ingest import rebuild_shared_index
from game_design_knowledge.snapshots import IndexSnapshotStore
from game_design_knowledge.state import DurableState, state_directory_for

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


def delete_derived_index(project_root: Path, index_directory: Path) -> None:
    """Delete everything the derived index owns, including its snapshots."""

    shutil.rmtree(index_directory)
    for snapshot in index_directory.parent.glob(
        f".{index_directory.name}.build-*"
    ):
        shutil.rmtree(snapshot)


class RevisionChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory(prefix="gdk-revisions-")
        self.project_root = Path(self._workspace.name) / "project"
        self.index_directory = self.project_root / ".index" / "knowledge"
        self.document = self.project_root / "docs" / "玩法.docx"
        write_docx(self.document, "每日开放5次")
        self.state = DurableState(self.project_root, state_directory_for(self.project_root))

    def tearDown(self) -> None:
        self._workspace.cleanup()

    def test_deleting_and_rebuilding_the_index_keeps_reviews_and_identity(self) -> None:
        rebuild_shared_index(self.project_root, self.index_directory)
        documents = self.state.documents()
        self.assertEqual(len(documents), 1)
        document = next(iter(documents.values()))
        first_parse = document.active_parse_revision
        self.state.append_review_event(
            "accept_transcription",
            subject_type="ocr_text",
            subject_id="image:1",
            actor="策划",
        )
        self.state.confirm_alias(
            "skill_dash", "冲刺", "冲锋", source="catalog.json", confirmed_by="策划"
        )
        bundles_before = self.state.bundle_ids()

        delete_derived_index(self.project_root, self.index_directory)
        self.assertFalse(self.index_directory.exists())
        self.assertTrue(self.state.exists())

        rebuild_shared_index(self.project_root, self.index_directory)

        rebuilt = self.state.documents()
        self.assertEqual(list(rebuilt), list(documents))
        self.assertEqual(
            rebuilt[document.document_id].relative_path, document.relative_path
        )
        self.assertEqual(
            rebuilt[document.document_id].active_parse_revision,
            first_parse,
            "unchanged bytes reproduce the same parse revision",
        )
        self.assertEqual(
            [event.action for event in self.state.review_events()],
            ["accept_transcription", "confirm_alias"],
        )
        self.assertEqual(
            self.state.read_dictionary()["entries"][0]["aliases"][0]["name"], "冲锋"
        )
        self.assertEqual(self.state.bundle_ids(), bundles_before)
        self.assertTrue(
            self.state.verify_bundle(bundles_before[0])["ok"],
            "the bundle still verifies after the index was rebuilt",
        )
        self.assertTrue(
            freshness_report(self.project_root, self.index_directory)["is_fresh"]
        )

    def test_changed_bytes_add_revisions_without_erasing_history(self) -> None:
        rebuild_shared_index(self.project_root, self.index_directory)
        first_source = self.state.source_revisions()[0]
        first_parse = self.state.parse_revisions()[0]
        first_bundle = self.state.bundle_ids()[0]

        write_docx(self.document, "每日开放8次")
        rebuild_shared_index(self.project_root, self.index_directory)

        self.assertEqual(len(self.state.source_revisions()), 2)
        self.assertEqual(len(self.state.parse_revisions()), 2)
        active = self.state.active_parse_revision()
        document = next(iter(active))
        self.assertNotEqual(active[document].parse_revision_id, first_parse.parse_revision_id)
        self.assertEqual(active[document].source_revision_id, self.state.source_revisions()[-1].source_revision_id)
        self.assertIn(first_source.source_revision_id, {record.source_revision_id for record in self.state.source_revisions()})
        self.assertTrue((self.state.directory / first_source.archive_path).is_file())

        self.assertTrue(self.state.verify_bundle(first_bundle)["ok"])
        store = IndexSnapshotStore(self.index_directory)
        published = store.current_snapshot()["parse_revisions"]
        self.assertEqual(
            published[document],
            active[document].parse_revision_id,
            "the published index names the active parse revision",
        )
        report = freshness_report(self.project_root, self.index_directory)
        self.assertTrue(report["is_fresh"], report["dimensions"])

    def test_tampering_with_an_archived_source_breaks_bundle_verification(self) -> None:
        rebuild_shared_index(self.project_root, self.index_directory)
        bundle_id = self.state.bundle_ids()[0]
        source = self.state.source_revisions()[0]
        archived = self.state.directory / source.archive_path

        archived.write_bytes(b"tampered")

        verified = self.state.verify_bundle(bundle_id)
        self.assertFalse(verified["ok"])
        self.assertIn(
            "archived_bytes", [check["check"] for check in verified["checks"] if not check["passed"]]
        )


if __name__ == "__main__":
    unittest.main()
