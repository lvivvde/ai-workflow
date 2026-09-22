"""V2-11: per-profile budgets and the run records a baseline writes."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from game_design_knowledge.run_records import (
    PROFILE_BUDGETS,
    RUN_RECORD_VERSION,
    RunRecorder,
    capability_baseline,
    disk_usage,
    profile_budget,
    read_run_records,
    write_run_record,
)


class ProfileBudgetTests(unittest.TestCase):
    def test_each_profile_reports_its_own_budget(self) -> None:
        for profile in PROFILE_BUDGETS:
            with self.subTest(profile=profile):
                budget = profile_budget(profile)
                self.assertEqual(budget["ocr_concurrency"] >= 1, True)
                self.assertIn("vector_recall", budget)
                self.assertIn("visual_models", budget)

    def test_the_budget_is_a_copy_not_the_shared_table(self) -> None:
        budget = profile_budget("baseline")
        budget["ocr_concurrency"] = 99

        self.assertNotEqual(profile_budget("baseline")["ocr_concurrency"], 99)

    def test_an_unknown_profile_is_refused(self) -> None:
        with self.assertRaises(KeyError) as caught:
            profile_budget("gpu_ultra")

        self.assertIn("gpu_ultra", str(caught.exception))

    def test_a_recorder_refuses_an_unknown_profile_at_start(self) -> None:
        with self.assertRaises(KeyError):
            RunRecorder(profile="gpu_ultra", operation="capability_baseline").start()


class RunRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-runs-")
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name)

    def test_a_finished_run_records_latency_memory_and_disk(self) -> None:
        recorder = RunRecorder(
            profile="baseline",
            operation="capability_baseline",
            model_root=self.workspace,
        )
        recorder.start()
        recorder.add_degradation("visual", "not_installed", "the pack is absent")
        record = recorder.finish()

        self.assertEqual(record["record_version"], RUN_RECORD_VERSION)
        self.assertEqual(record["status"], "succeeded")
        self.assertGreaterEqual(record["latency_seconds"], 0.0)
        self.assertEqual(record["profile"], "baseline")
        self.assertEqual(record["budget"], profile_budget("baseline"))
        self.assertEqual(record["disk"]["before"]["available"], True)
        self.assertEqual(len(record["degradation_events"]), 1)
        self.assertEqual(record["degradation_events"][0]["channel"], "visual")
        self.assertIn("one machine", record["boundary"])

    def test_a_run_that_raises_still_writes_a_record(self) -> None:
        recorder = RunRecorder(profile="recommended", operation="capability_baseline")

        with self.assertRaises(ValueError):
            with recorder:
                recorder.add_degradation("ocr", "no_usable_engine", "nothing ran")
                raise ValueError("the pipeline stopped early")

        record = recorder.as_payload()
        self.assertEqual(record["status"], "failed")
        self.assertIn("ValueError", record["error"])
        self.assertEqual(len(record["degradation_events"]), 1)

    def test_disk_usage_reports_free_space_or_says_it_cannot(self) -> None:
        report = disk_usage(self.workspace)

        self.assertTrue(report["available"])
        self.assertGreater(report["free_gb"], 0.0)


class RunRecordFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-runs-")
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name)

    def test_records_are_appended_oldest_first(self) -> None:
        path = self.workspace / "nested" / "baseline.jsonl"

        write_run_record(path, {"status": "succeeded", "index": 1})
        write_run_record(path, {"status": "succeeded", "index": 2})

        records = read_run_records(path)
        self.assertEqual([record["index"] for record in records], [1, 2])

    def test_reading_a_path_that_does_not_exist_is_empty(self) -> None:
        self.assertEqual(read_run_records(self.workspace / "absent.jsonl"), [])

    def test_a_damaged_line_is_named_with_its_number(self) -> None:
        path = self.workspace / "baseline.jsonl"
        path.write_text('{"status": "succeeded"}\nnot json\n', encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            read_run_records(path)

        self.assertIn("line 2", str(caught.exception))


class CapabilityBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-baseline-")
        self.addCleanup(self._temporary.cleanup)
        self.model_root = Path(self._temporary.name) / "models"

    def test_a_bare_machine_still_produces_a_record(self) -> None:
        record = capability_baseline(
            "baseline", model_root=self.model_root
        )

        self.assertEqual(record["operation"], "capability_baseline")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["profile"], "baseline")
        self.assertIn("limits", record["notes"])
        self.assertEqual(
            record["notes"]["limits"], profile_budget("baseline")
        )
        self.assertEqual(record["notes"]["query"]["status"], "skipped")
        self.assertTrue(record["degradation_events"])

    def test_every_unavailable_pack_becomes_a_degradation_event(self) -> None:
        record = capability_baseline("baseline", model_root=self.model_root)

        channels = {event["channel"] for event in record["degradation_events"]}
        packs = record["notes"]["packs"]
        for name, status in packs.items():
            if status != "available":
                self.assertIn(name, channels)
        self.assertEqual(record["record_version"], RUN_RECORD_VERSION)
        self.assertEqual(record["budget"], profile_budget("baseline"))


if __name__ == "__main__":
    unittest.main()
