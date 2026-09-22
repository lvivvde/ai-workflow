"""V2-03: capability packs, explicit installation, and model residency."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from game_design_knowledge.capabilities import (
    CORE_PACK,
    ENHANCED_OCR_PACK,
    PACKS,
    PACK_TO_EXECUTION_STATUS,
    VISUAL_MODEL_PIN_PATH,
    VISUAL_PACK,
    CapabilityRuntime,
    HardwareProfile,
    ModelStore,
    PACKS_BY_NAME,
    detect_pack,
    visual_model_pin_bytes,
)
from game_design_knowledge.ocr import (
    DEFAULT_CHAIN,
    RAPIDOCR_ENGINE,
    TESSERACT_ENGINE,
    select_engine,
)


def _generous_machine() -> HardwareProfile:
    """A machine that meets every declared minimum, so tests are host-agnostic."""

    return HardwareProfile(
        os_name="Windows",
        os_version="11",
        architecture="AMD64",
        cpu_cores=32,
        ram_gb=128.0,
        free_disk_gb=2048.0,
        gpu="NVIDIA Test",
        recommended="visual",
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TrackingHandle:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class CapabilityPackTests(unittest.TestCase):
    def test_pack_status_maps_to_one_execution_status(self) -> None:
        for status in (
            "available",
            "degraded",
            "not_installed",
            "insufficient_resources",
            "self_check_failed",
        ):
            self.assertIn(status, PACK_TO_EXECUTION_STATUS)
        for pack in PACKS:
            report = detect_pack(
                pack, model_root=Path("missing-model-root"), hardware=_generous_machine()
            )
            self.assertEqual(
                report.execution_status, PACK_TO_EXECUTION_STATUS[report.status]
            )
            self.assertTrue(report.checks, "every verdict carries the checks behind it")
            self.assertTrue(report.license)

    def test_a_machine_below_the_minimums_reports_insufficient_resources(self) -> None:
        small = HardwareProfile(
            os_name="Windows",
            os_version="11",
            architecture="AMD64",
            cpu_cores=2,
            ram_gb=4.0,
            free_disk_gb=1.0,
            gpu="none",
            recommended="baseline",
        )
        report = detect_pack(ENHANCED_OCR_PACK, model_root=Path("nowhere"), hardware=small)

        self.assertIn(report.status, {"not_installed", "insufficient_resources"})
        self.assertFalse(report.usable)
        hardware_check = next(
            check for check in report.checks if check["check"] == "hardware"
        )
        self.assertFalse(hardware_check["passed"])

    def test_the_core_pack_is_not_optional_and_declares_rapidocr(self) -> None:
        self.assertFalse(CORE_PACK.optional)
        self.assertIn(
            "rapidocr-onnxruntime",
            {artifact.distribution for artifact in CORE_PACK.python_artifacts},
        )
        self.assertTrue(ENHANCED_OCR_PACK.optional)
        self.assertTrue(VISUAL_PACK.optional)

    def test_the_visual_pack_pin_matches_the_bytes_it_ships(self) -> None:
        artifact = next(
            entry
            for entry in VISUAL_PACK.model_artifacts
            if entry.relative_path == VISUAL_MODEL_PIN_PATH
        )
        import hashlib

        self.assertEqual(
            hashlib.sha256(visual_model_pin_bytes()).hexdigest(), artifact.sha256
        )

    def test_installation_reads_only_a_local_directory_and_verifies_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "downloaded"
            store = ModelStore(workspace / "models")

            preview = store.install(VISUAL_PACK, source, confirmed=False)
            self.assertEqual(preview["status"], "confirmation_required")
            self.assertFalse(
                (workspace / "models").exists(),
                "a preview never writes anything",
            )

            (source / VISUAL_MODEL_PIN_PATH).parent.mkdir(parents=True)
            (source / VISUAL_MODEL_PIN_PATH).write_bytes(b"not the pinned bytes")
            with self.assertRaises(ValueError) as raised:
                store.install(VISUAL_PACK, source, confirmed=True)
            self.assertIn("pinned checksum", str(raised.exception))

            (source / VISUAL_MODEL_PIN_PATH).write_bytes(visual_model_pin_bytes())
            installed = store.install(VISUAL_PACK, source, confirmed=True)
            self.assertEqual(installed["status"], "installed")
            self.assertTrue(store.verify(VISUAL_PACK)["ok"])
            report = detect_pack(
                VISUAL_PACK, model_root=store.root, hardware=_generous_machine()
            )
            model_check = next(
                check
                for check in report.checks
                if check["check"] == f"model:{VISUAL_MODEL_PIN_PATH}"
            )
            self.assertTrue(model_check["passed"], model_check["detail"])
            self.assertNotIn(VISUAL_MODEL_PIN_PATH, report.missing)

    def test_python_only_packs_have_nothing_to_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ModelStore(Path(temporary_directory) / "models")
            result = store.install(ENHANCED_OCR_PACK, Path(temporary_directory), confirmed=True)

        self.assertEqual(result["status"], "no_model_artifacts")

    def test_removal_requires_confirmation_and_only_touches_model_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            store = ModelStore(workspace / "models")
            target = store.path_for(VISUAL_PACK, VISUAL_MODEL_PIN_PATH)
            target.parent.mkdir(parents=True)
            target.write_bytes(visual_model_pin_bytes())

            self.assertEqual(
                store.remove(VISUAL_PACK, confirmed=False)["status"],
                "confirmation_required",
            )
            self.assertTrue(target.is_file())
            self.assertEqual(
                store.remove(VISUAL_PACK, confirmed=True)["status"], "removed"
            )
            self.assertFalse(target.is_file())


class ModelResidencyTests(unittest.TestCase):
    def test_low_memory_mode_unloads_everything_at_the_end_of_a_batch(self) -> None:
        clock = FakeClock()
        handles: list[TrackingHandle] = []
        runtime = CapabilityRuntime(
            model_root=Path("missing"),
            hardware=_generous_machine(),
            low_memory=True,
            clock=clock,
            exit_hook=False,
        )

        def loader(pack):
            handle = TrackingHandle()
            handles.append(handle)
            return handle

        runtime.loader = loader
        with runtime:
            runtime.acquire("core")
            runtime.acquire("visual")
            self.assertEqual(
                [entry["pack"] for entry in runtime.residency()["packs"]],
                ["visual"],
                "low-memory mode never keeps a second pack resident",
            )
            self.assertTrue(handles[0].closed)
        self.assertEqual(runtime.residency()["packs"], [])
        self.assertTrue(all(handle.closed for handle in handles))

    def test_an_idle_pack_is_released_once_its_timeout_passes(self) -> None:
        clock = FakeClock()
        handle = TrackingHandle()
        runtime = CapabilityRuntime(
            model_root=Path("missing"),
            hardware=_generous_machine(),
            idle_timeout=60.0,
            clock=clock,
            exit_hook=False,
        )
        runtime.loader = lambda pack: handle

        runtime.acquire("visual")
        self.assertEqual(len(runtime.residency()["packs"]), 1)

        clock.advance(59.0)
        self.assertEqual(runtime.sweep(), [])
        self.assertEqual(len(runtime.residency()["packs"]), 1)

        clock.advance(2.0)
        self.assertEqual(runtime.sweep(), ["visual"])
        self.assertTrue(handle.closed)

    def test_residency_is_reused_inside_a_batch_and_explicitly_releasable(self) -> None:
        clock = FakeClock()
        loads: list[str] = []
        runtime = CapabilityRuntime(
            model_root=Path("missing"),
            hardware=_generous_machine(),
            idle_timeout=600.0,
            clock=clock,
            exit_hook=False,
        )

        def loader(pack):
            loads.append(pack.name)
            return TrackingHandle()

        runtime.loader = loader
        with runtime:
            runtime.acquire("core")
            runtime.acquire("core")
            runtime.acquire("core")
            self.assertEqual(loads, ["core"], "one load per batch, reused after that")
        self.assertEqual(runtime.release_all(), ["core"])
        self.assertEqual(runtime.residency()["packs"], [])

    def test_the_process_exit_hook_is_registered_by_default(self) -> None:
        runtime = CapabilityRuntime(model_root=Path("missing"), exit_hook=True)
        self.assertTrue(runtime.exit_hook_registered)
        runtime.acquire("core")
        self.assertIn(
            "core", [entry["pack"] for entry in runtime.residency()["packs"]]
        )

    def test_a_real_process_exit_releases_residency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "closed.txt"
            script = textwrap.dedent(
                f"""
                from pathlib import Path

                from game_design_knowledge.capabilities import CapabilityRuntime


                class Handle:
                    def close(self):
                        Path({str(marker)!r}).write_text("closed", encoding="utf-8")


                runtime = CapabilityRuntime(model_root=Path("missing"), exit_hook=True)
                runtime.loader = lambda pack: Handle()
                runtime.acquire("core")
                assert runtime.residency()["packs"], "the pack is resident before exit"
                print("resident")
                """
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                env=environment,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("resident", completed.stdout)
            self.assertTrue(
                marker.is_file(),
                "the interpreter exit hook closed the resident handle",
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "closed")

    def test_a_loaded_pack_is_reloaded_when_its_installed_status_changes(self) -> None:
        clock = FakeClock()
        loads: list[str] = []
        runtime = CapabilityRuntime(
            model_root=Path("missing"),
            hardware=_generous_machine(),
            clock=clock,
            exit_hook=False,
        )
        runtime.loader = lambda pack: loads.append(pack.name) or TrackingHandle()

        runtime.acquire("core")
        self.assertEqual(len(loads), 1)
        runtime.acquire("core")
        self.assertEqual(len(loads), 1, "an unchanged status reuses the loaded handle")

        from game_design_knowledge import capabilities as module

        original = module.detect_pack

        def available(*args, **kwargs):
            status = original(*args, **kwargs)
            return module.PackStatus(
                **{
                    **status.__dict__,
                    "status": "available",
                    "execution_status": "succeeded",
                }
            )

        module.detect_pack = available
        try:
            runtime.acquire("core")
        finally:
            module.detect_pack = original
        self.assertEqual(len(loads), 2, "a changed capability status drops the handle")

    def test_status_reports_the_policy_that_forbids_silent_downloads(self) -> None:
        runtime = CapabilityRuntime(
            model_root=Path("missing"),
            hardware=_generous_machine(),
            exit_hook=False,
        )
        status = runtime.status()

        self.assertEqual(status["policy"]["downloads"], "never automatic; install explicitly from a local directory")
        self.assertEqual(status["policy"]["background_scanning"], "none")
        self.assertNotIn(
            "download",
            " ".join(name for name in dir(ModelStore) if not name.startswith("_")),
            "the model store exposes no download path at all",
        )


class OcrDegradationChainTests(unittest.TestCase):
    def test_the_chain_records_every_engine_it_considered(self) -> None:
        selection = select_engine()

        self.assertEqual([engine.name for engine in DEFAULT_CHAIN], ["rapidocr", "paddleocr", "tesseract"])
        self.assertEqual(
            [entry["engine"] for entry in selection.chain], ["rapidocr", "paddleocr", "tesseract"]
        )
        payload = selection.as_payload()
        self.assertIn("execution_status", payload)
        self.assertIn("fallback_used", payload)
        for entry in payload["chain"]:
            self.assertIn("usable", entry)
            self.assertIn("detail", entry)

    def test_the_compatibility_fallback_can_be_switched_off_explicitly(self) -> None:
        selection = select_engine(allow_compatibility_fallback=False)

        if selection.engine is None:
            self.assertEqual(selection.reason_code, "no_usable_engine")
        self.assertNotEqual(selection.engine, TESSERACT_ENGINE)
        entry = next(
            item for item in selection.chain if item["engine"] == "tesseract"
        )
        if entry["available"]:
            self.assertFalse(entry["usable"])
            self.assertIn("disabled by configuration", entry["detail"])

    def test_an_unimplemented_engine_is_never_reported_as_ready(self) -> None:
        usable, detail = RAPIDOCR_ENGINE.usable()

        self.assertFalse(usable)
        self.assertIn("not implemented", detail)
        status, _, error = RAPIDOCR_ENGINE.transcribe(Path("whatever.png"))
        self.assertEqual(status, "unavailable")
        self.assertIn("not implemented", error or "")


if __name__ == "__main__":
    unittest.main()
