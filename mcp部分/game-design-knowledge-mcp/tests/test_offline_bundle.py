"""V2-11: offline capability bundles, verification, and removal."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from game_design_knowledge.capabilities import (
    CORE_PACK,
    PACKS,
    PACKS_BY_NAME,
    VISUAL_MODEL_PIN_PATH,
    visual_model_pin_bytes,
)
from game_design_knowledge.evaluation.network_guard import block_network
from game_design_knowledge.offline import (
    BUNDLE_ARTIFACT_MISSING,
    BUNDLE_CHECKSUM_MISMATCH,
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_MANIFEST_INVALID,
    BUNDLE_MISSING,
    BUNDLE_PACK_ABSENT,
    BUNDLE_PIN_MISMATCH,
    BUNDLE_PLATFORM_MISMATCH,
    BUNDLE_PYTHON_MISMATCH,
    BUNDLE_UNKNOWN_PACK,
    BUNDLE_VERSION,
    BundleError,
    BundlePlatform,
    OfflineBundle,
    capability_doctor,
    capability_manifests,
    host_platform,
    manifest_bytes,
    path_report,
    platform_compatible,
    tesseract_report,
    uninstall_capability,
)
from game_design_knowledge.revisions import file_sha256


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIRECTORY = PROJECT_ROOT / "capabilities" / "manifests"
SCHEMA_PATH = PROJECT_ROOT / "capabilities" / "bundle.schema.json"


def _wheel_content(distribution: str, version: str) -> bytes:
    return f"offline wheel for {distribution}=={version}\n".encode("utf-8")


class BundleBuilder:
    """A synthetic offline bundle, so the contract is tested without sizes."""

    def __init__(self, root: Path, *, host: BundlePlatform | None = None) -> None:
        self.root = Path(root)
        self.host = host or host_platform()
        self.packs: dict[str, dict[str, object]] = {}

    def add_pack(self, pack: str, *, with_wheels: bool = True) -> "BundleBuilder":
        spec = PACKS_BY_NAME[pack]
        self.packs[pack] = {"version": spec.version, "wheels": [], "models": []}
        if with_wheels:
            for artifact in spec.python_artifacts:
                self.add_wheel(pack, artifact.distribution, artifact.version)
            for dependency in spec.python_dependencies:
                self.add_wheel(pack, dependency.distribution, dependency.version)
        for artifact in spec.model_artifacts:
            self.add_model(pack, artifact.relative_path)
        return self

    def _entry(self, pack: str) -> dict[str, object]:
        if pack not in self.packs:
            self.packs[pack] = {"version": "1.0.0", "wheels": [], "models": []}
        return self.packs[pack]

    def add_wheel(
        self,
        pack: str,
        distribution: str,
        version: str,
        *,
        filename: str | None = None,
        sha256: str | None = None,
    ) -> "BundleBuilder":
        entry = self._entry(pack)
        name = filename or (
            f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
        )
        path = self.root / "wheels" / pack / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_wheel_content(distribution, version))
        entry["wheels"].append(  # type: ignore[union-attr]
            {
                "distribution": distribution,
                "version": version,
                "filename": name,
                "sha256": sha256 or file_sha256(path),
            }
        )
        return self

    def add_model(
        self,
        pack: str,
        relative_path: str,
        *,
        content: bytes | None = None,
        sha256: str | None = None,
    ) -> "BundleBuilder":
        entry = self._entry(pack)
        path = self.root / "models" / pack / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(visual_model_pin_bytes() if content is None else content)
        entry["models"].append(  # type: ignore[union-attr]
            {"relative_path": relative_path, "sha256": sha256 or file_sha256(path)}
        )
        return self

    def manifest(self, *, bundle_version: object = BUNDLE_VERSION) -> dict[str, object]:
        return {
            "bundle_version": bundle_version,
            "created_at": "2026-01-01T00:00:00+00:00",
            "platform": {
                "os": self.host.os_name,
                "architecture": self.host.architecture,
                "python": self.host.python,
            },
            "packs": self.packs,
        }

    def write(self, *, bundle_version: object = BUNDLE_VERSION) -> OfflineBundle:
        self.write_raw(self.manifest(bundle_version=bundle_version))
        return OfflineBundle.read(self.root, host=self.host)

    def write_raw(self, manifest: object) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / BUNDLE_MANIFEST_FILENAME
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path


class OfflineBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-bundle-")
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name)

    def _bundle(self, pack: str = "visual") -> OfflineBundle:
        return BundleBuilder(self.workspace / "bundle").add_pack(pack).write()

    def _wheel_files(self) -> list[Path]:
        return sorted((self.workspace / "bundle" / "wheels").rglob("*.whl"))

    # -- verification -----------------------------------------------------

    def test_a_complete_bundle_verifies_every_declared_byte(self) -> None:
        report = self._bundle("visual").verify("visual")

        self.assertEqual(report["status"], "verified")
        self.assertTrue(report["compatible"])
        self.assertEqual(report["reason"], "")
        checks = {check["check"]: check for check in report["checks"]}
        self.assertTrue(checks["wheel:ollama"]["passed"])
        self.assertTrue(checks["model:qwen2.5-vl-3b/pin.json"]["passed"])
        self.assertTrue(checks["platform"]["passed"])
        self.assertEqual(report["wheels"][0]["distribution"], "ollama")

    def test_a_corrupted_wheel_is_a_checksum_mismatch(self) -> None:
        bundle = self._bundle("visual")
        self._wheel_files()[0].write_bytes(b"truncated")

        report = bundle.verify("visual")

        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["reason"], BUNDLE_CHECKSUM_MISMATCH)
        self.assertIn("does not match the pinned", report["detail"])

    def test_a_deleted_wheel_is_a_missing_artifact(self) -> None:
        bundle = self._bundle("visual")
        self._wheel_files()[0].unlink()

        self.assertEqual(bundle.verify("visual")["reason"], BUNDLE_ARTIFACT_MISSING)

    def test_a_drifted_version_is_a_pin_mismatch(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack(
            "visual", with_wheels=False
        )
        builder.add_wheel("visual", "ollama", "0.3.2")

        report = builder.write().verify("visual")

        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["reason"], BUNDLE_PIN_MISMATCH)
        self.assertIn("0.3.2", report["detail"])

    def test_an_extra_wheel_is_refused_instead_of_installed(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack("visual")
        builder.add_wheel("visual", "some-other-package", "9.9.9")

        report = builder.write().verify("visual")

        self.assertEqual(report["reason"], BUNDLE_PIN_MISMATCH)
        self.assertIn("some-other-package", report["detail"])

    def test_a_missing_dependency_wheel_is_a_broken_bundle(self) -> None:
        """A pack carries its closure, so an incomplete closure is refused."""

        builder = BundleBuilder(self.workspace / "bundle").add_pack("core")
        missing = CORE_PACK.python_dependencies[0].distribution
        entry = builder.packs["core"]
        entry["wheels"] = [  # type: ignore[union-attr]
            wheel
            for wheel in entry["wheels"]  # type: ignore[union-attr]
            if wheel["distribution"] != missing
        ]

        report = builder.write().verify("core")

        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["reason"], BUNDLE_PIN_MISMATCH)
        self.assertIn(missing, report["detail"])

    def test_an_undeclared_transitive_wheel_is_still_refused(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack("core")
        builder.add_wheel("core", "some-transitive-package", "9.9.9")

        report = builder.write().verify("core")

        self.assertEqual(report["reason"], BUNDLE_PIN_MISMATCH)
        self.assertIn("some-transitive-package", report["detail"])

    def test_the_requirements_cover_every_wheel_the_bundle_carries(self) -> None:
        """Hash-checking mode installs the closure, so the closure is listed."""

        bundle = self._bundle("core")
        entry = bundle.entry("core")
        requirements = bundle.requirements("core")

        for wheel in entry["wheels"]:
            self.assertIn(
                f"{wheel['distribution']}=={wheel['version']} "
                f"--hash=sha256:{wheel['sha256']}",
                requirements,
            )

    def test_the_core_pack_pins_the_closure_rapidocr_imports(self) -> None:
        pinned = CORE_PACK.pinned_python()

        self.assertLessEqual(
            {
                "numpy",
                "opencv-python",
                "shapely",
                "pyclipper",
                "pyyaml",
                "pillow",
                "six",
                "coloredlogs",
                "flatbuffers",
                "protobuf",
                "sympy",
                "onnxruntime",
                "rapidocr-onnxruntime",
            },
            set(pinned),
        )
        artifact = next(
            entry
            for entry in CORE_PACK.python_artifacts
            if entry.distribution == "rapidocr-onnxruntime"
        )
        self.assertEqual(pinned["rapidocr-onnxruntime"], artifact.version)

    def test_a_pack_does_not_own_its_dependencies(self) -> None:
        """Dependencies ride along; they are not the pack's to remove."""

        for pack in PACKS:
            owned = {artifact.distribution for artifact in pack.python_artifacts}
            declared = {
                dependency.distribution for dependency in pack.python_dependencies
            }
            with self.subTest(pack=pack.name):
                self.assertEqual(owned & declared, set())

    def test_a_model_the_build_does_not_pin_is_refused(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack("visual")
        builder.add_model(
            "visual", "qwen2.5-vl-3b/extra.bin", content=b"an unpinned model file"
        )

        report = builder.write().verify("visual")

        self.assertEqual(report["reason"], BUNDLE_PIN_MISMATCH)
        self.assertIn("extra.bin", report["detail"])

    def test_a_model_file_that_drifted_is_a_checksum_mismatch(self) -> None:
        bundle = self._bundle("visual")
        pinned = bundle.models_directory("visual") / VISUAL_MODEL_PIN_PATH
        pinned.write_bytes(b"a model file that was edited after packing")

        report = bundle.verify("visual")

        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["reason"], BUNDLE_CHECKSUM_MISMATCH)
        self.assertIn("pin.json", report["detail"])

    def test_another_platform_or_python_is_reported_before_installing(self) -> None:
        bundle = self._bundle("visual")
        other_os = BundlePlatform(
            "darwin", bundle.host.architecture, bundle.host.python
        )
        other_python = BundlePlatform(
            bundle.host.os_name, bundle.host.architecture, "3.9"
        )

        self.assertEqual(
            platform_compatible(bundle.platform, other_os)[1],
            BUNDLE_PLATFORM_MISMATCH,
        )
        self.assertEqual(
            platform_compatible(bundle.platform, other_python)[1],
            BUNDLE_PYTHON_MISMATCH,
        )
        mismatched = OfflineBundle.read(bundle.root, host=other_os)
        self.assertEqual(
            mismatched.verify("visual")["reason"], BUNDLE_PLATFORM_MISMATCH
        )
        with self.assertRaises(BundleError) as caught:
            mismatched.install("visual", confirmed=True)
        self.assertEqual(caught.exception.reason, BUNDLE_PLATFORM_MISMATCH)

    # -- manifest shape ---------------------------------------------------

    def test_a_bundle_that_is_not_there_is_named_as_such(self) -> None:
        with self.assertRaises(BundleError) as caught:
            OfflineBundle.read(self.workspace / "absent")
        self.assertEqual(caught.exception.reason, BUNDLE_MISSING)

    def test_a_manifest_without_a_platform_is_refused(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack("visual")
        builder.write_raw({"bundle_version": 1, "packs": builder.packs})

        with self.assertRaises(BundleError) as caught:
            OfflineBundle.read(builder.root)
        self.assertEqual(caught.exception.reason, BUNDLE_MANIFEST_INVALID)

    def test_an_unknown_bundle_version_is_refused(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack("visual")
        builder.write_raw(builder.manifest(bundle_version=99))

        with self.assertRaises(BundleError) as caught:
            OfflineBundle.read(builder.root)
        self.assertEqual(caught.exception.reason, BUNDLE_MANIFEST_INVALID)

    def test_an_unknown_pack_name_is_refused(self) -> None:
        builder = BundleBuilder(self.workspace / "bundle").add_pack("visual")
        manifest = builder.manifest()
        manifest["packs"] = {"super_ocr": builder.packs["visual"]}
        builder.write_raw(manifest)

        with self.assertRaises(BundleError) as caught:
            OfflineBundle.read(builder.root)
        self.assertEqual(caught.exception.reason, BUNDLE_UNKNOWN_PACK)

    def test_a_pack_the_bundle_does_not_carry_is_reported_not_guessed(self) -> None:
        bundle = self._bundle("core")

        report = bundle.verify("visual")

        self.assertEqual(report["status"], "absent")
        self.assertEqual(report["reason"], BUNDLE_PACK_ABSENT)
        self.assertEqual(report["models"], [])
        self.assertEqual(bundle.plan("visual")["status"], "absent")

    # -- install ----------------------------------------------------------

    def test_an_offline_install_copies_models_and_defers_python(self) -> None:
        bundle = self._bundle("visual")
        model_root = self.workspace / "models"

        with block_network([]):
            with mock.patch(
                "game_design_knowledge.offline.subprocess.run",
                side_effect=AssertionError("the installer spawned a process"),
            ):
                preview = bundle.install("visual", model_root=model_root)
                self.assertEqual(preview["status"], "confirmation_required")
                self.assertFalse(model_root.exists())
                installed = bundle.install(
                    "visual", model_root=model_root, confirmed=True
                )

        self.assertEqual(installed["status"], "installed")
        self.assertTrue(installed["offline"])
        self.assertEqual(installed["network_access"], "never")
        self.assertTrue(installed["facts_untouched"])
        self.assertTrue(installed["lexical_index_untouched"])
        copied = model_root / "visual" / VISUAL_MODEL_PIN_PATH
        self.assertTrue(copied.is_file())
        self.assertEqual(
            file_sha256(copied),
            PACKS_BY_NAME["visual"].model_artifacts[0].sha256,
        )
        self.assertFalse(installed["python"]["applied"])
        command = installed["python"]["command"]
        self.assertEqual(
            command[:6],
            [sys.executable, "-m", "pip", "install", "--no-index", "--find-links"],
        )
        self.assertIn("--only-binary", command)
        self.assertIn("--require-hashes", command)
        requirements = Path(installed["python"]["requirements_path"])
        self.assertTrue(requirements.is_file())
        self.assertIn("--hash=sha256:", requirements.read_text(encoding="utf-8"))

    def test_an_incomplete_bundle_cannot_be_installed(self) -> None:
        bundle = self._bundle("visual")
        self._wheel_files()[0].unlink()

        plan = bundle.plan("visual")
        self.assertEqual(plan["status"], "incomplete")
        self.assertEqual(plan["blocking"], [BUNDLE_ARTIFACT_MISSING])
        self.assertIn("fix bundle_artifact_missing", plan["actions"][0])
        with self.assertRaises(BundleError) as caught:
            bundle.install("visual", confirmed=True)
        self.assertEqual(caught.exception.reason, BUNDLE_ARTIFACT_MISSING)

    # -- removal ----------------------------------------------------------

    def test_removal_needs_confirmation_and_leaves_python_artifacts_alone(self) -> None:
        bundle = self._bundle("visual")
        model_root = self.workspace / "models"
        bundle.install("visual", model_root=model_root, confirmed=True)

        preview = uninstall_capability("visual", model_root=model_root)
        self.assertEqual(preview["status"], "confirmation_required")
        self.assertTrue((model_root / "visual").is_dir())

        removed = uninstall_capability(
            "visual", model_root=model_root, confirmed=True
        )

        self.assertEqual(removed["status"], "removed")
        self.assertFalse((model_root / "visual").exists())
        self.assertEqual(removed["python_artifacts"]["kept"], ["ollama"])
        self.assertEqual(
            removed["python_artifacts"]["uninstall_command"][:4],
            [sys.executable, "-m", "pip", "uninstall"],
        )
        self.assertFalse(removed["requires_reindex"])

    def test_a_locked_model_file_stops_the_removal_with_a_reason(self) -> None:
        bundle = self._bundle("visual")
        model_root = self.workspace / "models"
        bundle.install("visual", model_root=model_root, confirmed=True)
        locked = model_root / "visual" / VISUAL_MODEL_PIN_PATH

        with mock.patch(
            "game_design_knowledge.offline.ModelStore.remove",
            side_effect=PermissionError(32, "The process cannot access the file"),
        ):
            with self.assertRaises(BundleError) as caught:
                uninstall_capability(
                    "visual", model_root=model_root, confirmed=True
                )

        self.assertEqual(caught.exception.reason, "capability_removal_failed")
        self.assertIn(str(Path(VISUAL_MODEL_PIN_PATH)), caught.exception.detail)
        self.assertTrue(locked.is_file())

    def test_an_unknown_pack_cannot_be_uninstalled(self) -> None:
        with self.assertRaises(BundleError) as caught:
            uninstall_capability("super_ocr", model_root=self.workspace / "models")
        self.assertEqual(caught.exception.reason, BUNDLE_UNKNOWN_PACK)


class CapabilityDoctorTests(unittest.TestCase):
    """The pre-install report: what this machine has, and what it is missing."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-doctor-")
        self.addCleanup(self._temporary.cleanup)
        self.model_root = Path(self._temporary.name) / "models"

    def _tesseract_run(self, listing: str):
        """A stand-in for the local Tesseract, so languages are deterministic."""

        def runner(command: list[str]) -> tuple[int, str]:
            if "--list-langs" in command:
                return 0, listing
            return 0, "tesseract 5.3.0\n leptonica-1.82\n"

        return runner

    def test_a_ready_machine_reports_what_it_has(self) -> None:
        report = capability_doctor(
            packs=["core"],
            model_root=self.model_root,
            executable="tesseract",
            run=self._tesseract_run(
                "List of available languages (3):\nchi_sim\neng\nosd\n"
            ),
        )

        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["tesseract"]["available"])
        self.assertEqual(report["tesseract"]["reason"], "")
        self.assertEqual(report["tesseract"]["missing_languages"], [])
        self.assertEqual(report["packs"][0]["name"], "core")
        self.assertTrue(report["paths"]["blocking"] == [])
        self.assertIn("Windows 10/11", report["boundary"])
        self.assertEqual(
            report["policy"]["network_access"],
            "none during install, verification or indexing",
        )

    def test_a_machine_without_chinese_says_which_pack_is_missing(self) -> None:
        report = capability_doctor(
            packs=["core"],
            model_root=self.model_root,
            executable="tesseract",
            run=self._tesseract_run("List of available languages (2):\neng\nosd\n"),
        )

        tesseract = report["tesseract"]
        self.assertEqual(tesseract["reason"], "tesseract_language_pack_missing")
        self.assertEqual(tesseract["missing_languages"], ["chi_sim"])
        self.assertEqual(tesseract["compatibility_fallback"], "degraded")
        self.assertTrue(
            any("chi_sim" in action for action in report["actions"]),
            report["actions"],
        )

    def test_no_tesseract_at_all_is_reported_as_missing(self) -> None:
        with mock.patch(
            "game_design_knowledge.offline.shutil.which", return_value=None
        ):
            report = tesseract_report()

        self.assertFalse(report["available"])
        self.assertEqual(report["reason"], "tesseract_missing")
        self.assertEqual(report["compatibility_fallback"], "unavailable")

    def test_the_configured_ocr_language_moves_the_requirement(self) -> None:
        with mock.patch.dict(os.environ, {"GAME_DESIGN_OCR_LANG": "jpn+eng"}):
            report = tesseract_report(
                executable="tesseract",
                run=self._tesseract_run(
                    "List of available languages (2):\nchi_sim\neng\n"
                ),
            )

        self.assertEqual(report["required_languages"], ["jpn", "eng"])
        self.assertEqual(report["missing_languages"], ["jpn"])

    def test_an_unknown_pack_is_refused_with_the_known_names(self) -> None:
        with self.assertRaises(BundleError) as caught:
            capability_doctor(
                packs=["super_ocr"],
                model_root=self.model_root,
                executable="tesseract",
                run=self._tesseract_run(""),
            )

        self.assertEqual(caught.exception.reason, BUNDLE_UNKNOWN_PACK)
        self.assertIn("core", caught.exception.detail)

    def test_a_relative_model_root_blocks_the_install(self) -> None:
        report = path_report(Path("models"))

        self.assertIn("absolute", report["blocking"])
        checks = {check["check"]: check for check in report["checks"]}
        self.assertEqual(checks["absolute"]["reason"], "model_root_not_absolute")

    def test_a_path_that_is_too_long_blocks_the_install(self) -> None:
        long_root = Path("C:/") / "nested" / Path(*["segment"] * 40) / "models"
        report = path_report(long_root)

        self.assertIn("short_enough", report["blocking"])
        checks = {check["check"]: check for check in report["checks"]}
        self.assertEqual(checks["short_enough"]["reason"], "model_root_path_long")


class CapabilityManifestTests(unittest.TestCase):
    """The committed manifests are the shipped pins, byte for byte."""

    def test_a_pack_name_and_its_object_produce_the_same_bytes(self) -> None:
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                self.assertEqual(manifest_bytes(pack.name), manifest_bytes(pack))

    def test_the_committed_manifests_match_this_build(self) -> None:
        for pack in PACKS:
            path = MANIFEST_DIRECTORY / f"{pack.name}.json"
            with self.subTest(pack=pack.name):
                self.assertTrue(path.is_file(), path)
                self.assertEqual(path.read_bytes(), manifest_bytes(pack))
                self.assertIn(pack.name, capability_manifests())

    def test_the_schema_names_the_fields_and_packs_this_build_writes(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            set(schema["required"]), {"bundle_version", "platform", "packs"}
        )
        self.assertEqual(schema["properties"]["bundle_version"]["const"], BUNDLE_VERSION)
        packs = schema["properties"]["packs"]
        self.assertEqual(
            set(packs["propertyNames"]["enum"]), set(PACKS_BY_NAME)
        )
        self.assertEqual(
            set(packs["additionalProperties"]["required"]),
            {"version", "wheels", "models"},
        )


if __name__ == "__main__":
    unittest.main()
