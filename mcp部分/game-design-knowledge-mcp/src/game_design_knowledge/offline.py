"""Offline capability bundles: install, verify, and remove without a network.

A capability bundle is a directory an operator prepares once on a machine with
network access and then copies to an air-gapped Windows machine. It holds the
wheels for the pack's Python artifacts, the model files the pack pins, and a
manifest that pins every byte by SHA256.

Installation never resolves anything over the network. Python artifacts are
installed from the bundle's own wheel directory with ``--no-index``,
``--only-binary`` and ``--require-hashes``, and model files are copied from the
bundle after their checksums match both the bundle manifest and the pack's own
pin. A wheel that is missing, a checksum that disagrees, a bundle built for
another platform or Python, or a pack whose declared pins drifted from this
build all stop the install with a reason instead of a partial result.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence

from .capabilities import (
    HARDWARE_PROFILES,
    PACKS,
    PACKS_BY_NAME,
    CapabilityPack,
    ModelStore,
    default_model_root,
    detect_hardware,
    detect_pack,
)
from .revisions import file_sha256


BUNDLE_VERSION = 1
BUNDLE_MANIFEST_FILENAME = "bundle.json"
WHEELS_DIRECTORY = "wheels"
MODELS_DIRECTORY = "models"
REQUIREMENTS_FILENAME = "offline-requirements.txt"

#: Why a bundle could not be used. Every one of these is reported, so a failed
#: install says which artifact and which check, rather than "install failed".
BUNDLE_MISSING = "bundle_missing"
BUNDLE_MANIFEST_INVALID = "bundle_manifest_invalid"
BUNDLE_UNKNOWN_PACK = "bundle_unknown_pack"
BUNDLE_PACK_ABSENT = "bundle_pack_absent"
BUNDLE_PLATFORM_MISMATCH = "bundle_platform_mismatch"
BUNDLE_PYTHON_MISMATCH = "bundle_python_mismatch"
BUNDLE_ARTIFACT_MISSING = "bundle_artifact_missing"
BUNDLE_CHECKSUM_MISMATCH = "bundle_checksum_mismatch"
BUNDLE_PIN_MISMATCH = "bundle_pin_mismatch"
TESSERACT_MISSING = "tesseract_missing"
TESSERACT_LANGUAGE_PACK_MISSING = "tesseract_language_pack_missing"
DISK_TOO_SMALL = "disk_too_small"
PATH_NOT_ABSOLUTE = "model_root_not_absolute"
PATH_NOT_WRITABLE = "model_root_not_writable"
PATH_LONG = "model_root_path_long"
REMOVAL_FAILED = "capability_removal_failed"

BUNDLE_REASONS = (
    BUNDLE_MISSING,
    BUNDLE_MANIFEST_INVALID,
    BUNDLE_UNKNOWN_PACK,
    BUNDLE_PACK_ABSENT,
    BUNDLE_PLATFORM_MISMATCH,
    BUNDLE_PYTHON_MISMATCH,
    BUNDLE_ARTIFACT_MISSING,
    BUNDLE_CHECKSUM_MISMATCH,
    BUNDLE_PIN_MISMATCH,
)

#: The Tesseract compatibility fallback is not an equal substitute for the core
#: engine, so the languages it must carry are named rather than assumed.
DEFAULT_TESSERACT_LANGUAGES = ("chi_sim", "eng")
OCR_LANGUAGE_ENV = "GAME_DESIGN_OCR_LANG"


class BundleError(RuntimeError):
    """An offline bundle this build refuses instead of guessing at."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class BundlePlatform:
    """The machine a bundle was built for."""

    os_name: str
    architecture: str
    python: str

    def as_payload(self) -> dict[str, str]:
        return {
            "os": self.os_name,
            "architecture": self.architecture,
            "python": self.python,
        }


def host_platform() -> BundlePlatform:
    """This machine, in the same shape a bundle declares."""

    return BundlePlatform(
        os_name=platform.system().lower(),
        architecture=platform.machine(),
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
    )


def platform_compatible(
    bundle: BundlePlatform, host: BundlePlatform
) -> tuple[bool, str, str]:
    """Whether a bundle's platform may be installed on this host."""

    if bundle.os_name != host.os_name:
        return (
            False,
            BUNDLE_PLATFORM_MISMATCH,
            f"the bundle was built for {bundle.os_name!r}, this machine is "
            f"{host.os_name!r}",
        )
    if bundle.architecture != host.architecture:
        return (
            False,
            BUNDLE_PLATFORM_MISMATCH,
            f"the bundle was built for {bundle.architecture!r}, this machine is "
            f"{host.architecture!r}",
        )
    if bundle.python != host.python:
        return (
            False,
            BUNDLE_PYTHON_MISMATCH,
            f"the bundle carries wheels for Python {bundle.python}, this "
            f"interpreter is Python {host.python}",
        )
    return (True, "", "")


class OfflineBundle:
    """One pre-downloaded bundle directory, and what it may install."""

    def __init__(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        *,
        host: BundlePlatform | None = None,
    ) -> None:
        self.root = Path(root)
        self.manifest = dict(manifest)
        self.host = host or host_platform()
        self.platform = BundlePlatform(
            os_name=str((manifest.get("platform") or {}).get("os") or ""),
            architecture=str(
                (manifest.get("platform") or {}).get("architecture") or ""
            ),
            python=str((manifest.get("platform") or {}).get("python") or ""),
        )
        self.packs: dict[str, dict[str, Any]] = {
            str(name): dict(entry)
            for name, entry in (manifest.get("packs") or {}).items()
        }

    # -- reading ----------------------------------------------------------

    @classmethod
    def read(cls, root: Path, *, host: BundlePlatform | None = None) -> "OfflineBundle":
        """Load a bundle directory, or say exactly what is wrong with it."""

        directory = Path(root)
        if not directory.is_dir():
            raise BundleError(
                BUNDLE_MISSING, f"the bundle directory does not exist: {directory}"
            )
        manifest_path = directory / BUNDLE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise BundleError(
                BUNDLE_MANIFEST_INVALID,
                f"{directory} has no {BUNDLE_MANIFEST_FILENAME}",
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BundleError(
                BUNDLE_MANIFEST_INVALID, f"{manifest_path} is not readable JSON: {error}"
            ) from error
        if not isinstance(manifest, dict):
            raise BundleError(
                BUNDLE_MANIFEST_INVALID,
                f"{manifest_path} must hold a JSON object",
            )
        if manifest.get("bundle_version") != BUNDLE_VERSION:
            raise BundleError(
                BUNDLE_MANIFEST_INVALID,
                f"{manifest_path} declares bundle_version "
                f"{manifest.get('bundle_version')!r}, this build reads "
                f"{BUNDLE_VERSION}",
            )
        declared = manifest.get("platform")
        if not isinstance(declared, dict) or not all(
            isinstance(declared.get(key), str) and declared.get(key)
            for key in ("os", "architecture", "python")
        ):
            raise BundleError(
                BUNDLE_MANIFEST_INVALID,
                f"{manifest_path} must declare platform.os, platform.architecture "
                "and platform.python",
            )
        packs = manifest.get("packs")
        if not isinstance(packs, dict) or not packs:
            raise BundleError(
                BUNDLE_MANIFEST_INVALID, f"{manifest_path} declares no packs"
            )
        for name, entry in packs.items():
            if name not in PACKS_BY_NAME:
                raise BundleError(
                    BUNDLE_UNKNOWN_PACK,
                    f"{manifest_path} declares unknown pack {name!r}; known packs "
                    f"are {sorted(PACKS_BY_NAME)}",
                )
            if not isinstance(entry, dict):
                raise BundleError(
                    BUNDLE_MANIFEST_INVALID, f"pack {name!r} must be a JSON object"
                )
            for key in ("version", "wheels", "models"):
                if key not in entry:
                    raise BundleError(
                        BUNDLE_MANIFEST_INVALID,
                        f"pack {name!r} in {manifest_path} is missing {key!r}",
                    )
        return cls(directory, manifest, host=host)

    # -- layout -----------------------------------------------------------

    def wheels_directory(self, pack: str) -> Path:
        """Where this pack's wheels live: one directory per pack."""

        return self.root / WHEELS_DIRECTORY / pack

    def models_directory(self, pack: str) -> Path:
        """Where this pack's model files live, in their store-relative layout."""

        return self.root / MODELS_DIRECTORY / pack

    def requirements_path(self, pack: str, model_root: Path) -> Path:
        return Path(model_root) / pack / REQUIREMENTS_FILENAME

    def entry(self, pack: str) -> dict[str, Any]:
        return dict(self.packs.get(pack) or {})

    def requirements(self, pack: str) -> str:
        """A hashed requirements file for this pack, resolved from the bundle."""

        entry = self.entry(pack)
        lines = [
            "# generated from the offline bundle; every pin carries its hash",
            "# install with: pip install --no-index --find-links wheels "
            "--only-binary :all: --require-hashes -r this-file",
        ]
        for wheel in entry.get("wheels") or ():
            lines.append(
                f"{wheel.get('distribution')}=={wheel.get('version')} "
                f"--hash=sha256:{wheel.get('sha256')}"
            )
        return "\n".join(lines) + "\n"

    def install_command(
        self, pack: str, *, python_executable: str, requirements: Path
    ) -> list[str]:
        """The exact offline pip command, with nothing that could reach a network."""

        return [
            python_executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(self.wheels_directory(pack)),
            "--only-binary",
            ":all:",
            "--require-hashes",
            "-r",
            str(requirements),
        ]

    def uninstall_command(
        self, pack: str, *, python_executable: str
    ) -> list[str]:
        """The pip command that would remove this pack's Python distributions.

        Reported, never run: removing distributions from an interpreter is the
        operator's decision, and the model files are what this tool owns.
        """

        pack_spec = PACKS_BY_NAME[pack]
        return [
            python_executable,
            "-m",
            "pip",
            "uninstall",
            "-y",
            *[artifact.distribution for artifact in pack_spec.python_artifacts],
        ]

    # -- verification -----------------------------------------------------

    def verify(self, pack: str) -> dict[str, Any]:
        """Check every declared byte, and the pins they are supposed to satisfy."""

        if pack not in PACKS_BY_NAME:
            raise BundleError(
                BUNDLE_UNKNOWN_PACK,
                f"unknown capability pack {pack!r}; known packs are "
                f"{sorted(PACKS_BY_NAME)}",
            )
        if pack not in self.packs:
            return {
                "pack": pack,
                "status": "absent",
                "reason": BUNDLE_PACK_ABSENT,
                "detail": f"this bundle does not carry {pack}",
                "checks": [],
                "wheels": [],
                "models": [],
                "compatible": False,
                "compatibility": {
                    "ok": False,
                    "reason": BUNDLE_PACK_ABSENT,
                    "detail": f"this bundle does not carry {pack}",
                },
                "bundle": str(self.root),
                "platform": self.platform.as_payload(),
                "host": self.host.as_payload(),
            }

        spec = PACKS_BY_NAME[pack]
        entry = self.entry(pack)
        checks: list[dict[str, Any]] = []
        wheels: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []

        declared_wheels = {
            str(wheel.get("distribution")): wheel for wheel in entry.get("wheels") or ()
        }
        pinned = {artifact.distribution: artifact for artifact in spec.python_artifacts}
        for name, artifact in pinned.items():
            wheel = declared_wheels.get(name)
            if wheel is None:
                checks.append(
                    _check(
                        f"pin:{name}",
                        False,
                        f"this build pins {name}=={artifact.version}, the bundle "
                        "does not carry it",
                        reason=BUNDLE_PIN_MISMATCH,
                    )
                )
                continue
            if str(wheel.get("version")) != artifact.version:
                checks.append(
                    _check(
                        f"pin:{name}",
                        False,
                        f"this build pins {name}=={artifact.version}, the bundle "
                        f"carries {wheel.get('version')}",
                        reason=BUNDLE_PIN_MISMATCH,
                    )
                )
                continue
            path = self.wheels_directory(pack) / str(wheel.get("filename") or "")
            digest, problem = _verify_file(path, str(wheel.get("sha256") or ""))
            entry_check = _check(
                f"wheel:{name}",
                problem == "",
                (
                    f"{wheel.get('filename')} sha256 {digest}"
                    if problem == ""
                    else problem
                ),
                reason=(
                    BUNDLE_ARTIFACT_MISSING
                    if "does not exist" in problem
                    else BUNDLE_CHECKSUM_MISMATCH
                ),
            )
            checks.append(entry_check)
            wheels.append(
                {
                    "distribution": name,
                    "version": artifact.version,
                    "filename": str(wheel.get("filename") or ""),
                    "path": str(path),
                    "sha256": str(wheel.get("sha256") or ""),
                    "verified": bool(entry_check["passed"]),
                }
            )

        extras = sorted(set(declared_wheels) - set(pinned))
        for name in extras:
            checks.append(
                _check(
                    f"pin:{name}",
                    False,
                    f"the bundle carries {name}, which this build's {pack} pack "
                    "does not declare",
                    reason=BUNDLE_PIN_MISMATCH,
                )
            )

        pinned_models = {
            artifact.relative_path: artifact for artifact in spec.model_artifacts
        }
        declared_models = {
            str(model.get("relative_path")): model
            for model in entry.get("models") or ()
        }
        for relative, artifact in pinned_models.items():
            model = declared_models.get(relative)
            if model is None or str(model.get("sha256")) != artifact.sha256:
                checks.append(
                    _check(
                        f"pin:model:{relative}",
                        False,
                        f"this build pins {relative} at {artifact.sha256}",
                        reason=BUNDLE_PIN_MISMATCH,
                    )
                )
                continue
            path = self.models_directory(pack) / relative
            digest, problem = _verify_file(path, artifact.sha256)
            model_check = _check(
                f"model:{relative}",
                problem == "",
                f"sha256 {digest}" if problem == "" else problem,
                reason=(
                    BUNDLE_ARTIFACT_MISSING
                    if "does not exist" in problem
                    else BUNDLE_CHECKSUM_MISMATCH
                ),
            )
            checks.append(model_check)
            models.append(
                {
                    "relative_path": relative,
                    "path": str(path),
                    "sha256": artifact.sha256,
                    "verified": bool(model_check["passed"]),
                }
            )
        for relative in sorted(set(declared_models) - set(pinned_models)):
            checks.append(
                _check(
                    f"pin:model:{relative}",
                    False,
                    f"the bundle carries model {relative}, which this build's "
                    f"{pack} pack does not pin",
                    reason=BUNDLE_PIN_MISMATCH,
                )
            )

        ok, reason, detail = platform_compatible(self.platform, self.host)
        checks.append(
            _check(
                "platform",
                ok,
                "built for this machine and interpreter"
                if ok
                else f"{reason}: {detail}",
                reason=reason or BUNDLE_PLATFORM_MISMATCH,
            )
        )
        verified = all(check["passed"] for check in checks)
        return {
            "pack": pack,
            "version": str(entry.get("version") or ""),
            "status": "verified" if verified else "incomplete",
            "reason": "" if verified else _first_reason(checks),
            "detail": "" if verified else _first_detail(checks),
            "checks": checks,
            "wheels": wheels,
            "models": models,
            "compatible": ok,
            "compatibility": {"ok": ok, "reason": reason, "detail": detail},
            "bundle": str(self.root),
            "platform": self.platform.as_payload(),
            "host": self.host.as_payload(),
        }

    # -- lifecycle --------------------------------------------------------

    def plan(
        self,
        pack: str,
        *,
        model_root: Path | None = None,
        python_executable: str | None = None,
    ) -> dict[str, Any]:
        """What installing this pack would do, without doing any of it."""

        root = Path(model_root) if model_root is not None else default_model_root()
        executable = python_executable or sys.executable
        verified = self.verify(pack)
        requirements = self.requirements_path(pack, root)
        blocking = [
            check["reason"]
            for check in verified["checks"]
            if not check["passed"] and check.get("reason")
        ]
        ready = verified["status"] == "verified"
        return {
            "status": "ready" if ready else verified["status"],
            "pack": pack,
            "version": verified.get("version", ""),
            "bundle": str(self.root),
            "model_root": str(root),
            "platform": verified["platform"],
            "host": verified["host"],
            "compatible": verified["compatible"],
            "compatibility": verified["compatibility"],
            "wheels": verified["wheels"],
            "models": verified["models"],
            "checks": verified["checks"],
            "blocking": blocking,
            "python": {
                "executable": executable,
                "requirements_path": str(requirements),
                "requirements": self.requirements(pack),
                "command": self.install_command(
                    pack, python_executable=executable, requirements=requirements
                ),
            },
            "offline": True,
            "network_access": "never",
            "actions": (
                [
                    f"write {requirements}",
                    "copy the pinned model files into the model store",
                    "pip install --no-index --find-links "
                    f"{self.wheels_directory(pack)}",
                ]
                if ready
                else [f"fix {reason} before installing {pack}" for reason in blocking]
            ),
        }

    def install(
        self,
        pack: str,
        *,
        model_root: Path | None = None,
        confirmed: bool = False,
        python_executable: str | None = None,
        apply_python: bool = False,
    ) -> dict[str, Any]:
        """Install one pack from this bundle, strictly, after explicit consent."""

        plan = self.plan(
            pack, model_root=model_root, python_executable=python_executable
        )
        if not confirmed:
            return {
                "status": "confirmation_required",
                "pack": pack,
                "plan": plan,
                "message": (
                    "installing copies the pack's model files into "
                    f"{plan['model_root']} and prints the offline pip command; "
                    "confirm before applying"
                ),
            }
        if plan["status"] != "ready":
            raise BundleError(
                str(plan["blocking"][0] if plan["blocking"] else BUNDLE_PACK_ABSENT),
                f"bundle cannot install {pack}: " + "; ".join(plan["actions"]),
            )

        root = Path(plan["model_root"])
        requirements = self.requirements_path(pack, root)
        requirements.parent.mkdir(parents=True, exist_ok=True)
        requirements.write_text(self.requirements(pack), encoding="utf-8", newline="\n")
        store = ModelStore(root)
        models = store.install(
            pack, self.models_directory(pack), confirmed=True
        )
        command = self.install_command(
            pack, python_executable=plan["python"]["executable"], requirements=requirements
        )
        python_result: dict[str, Any] = {
            "applied": False,
            "command": command,
            "requirements_path": str(requirements),
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
        if apply_python:
            completed = subprocess.run(  # noqa: S603 - an explicit operator action
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            python_result.update(
                {
                    "applied": True,
                    "returncode": completed.returncode,
                    "stdout_tail": _tail(completed.stdout),
                    "stderr_tail": _tail(completed.stderr),
                }
            )
        return {
            "status": (
                "installed"
                if not apply_python or python_result["returncode"] == 0
                else "python_install_failed"
            ),
            "pack": pack,
            "version": plan["version"],
            "model_root": str(root),
            "models": models,
            "python": python_result,
            "verification": self.verify(pack),
            "offline": True,
            "network_access": "never",
            "facts_untouched": True,
            "lexical_index_untouched": True,
        }

    def uninstall(
        self,
        pack: str,
        *,
        model_root: Path | None = None,
        confirmed: bool = False,
        python_executable: str | None = None,
    ) -> dict[str, Any]:
        """Remove a pack's model files; never the facts or the lexical index."""

        return uninstall_capability(
            pack,
            model_root=model_root,
            confirmed=confirmed,
            python_executable=python_executable,
        )

    # -- diagnostics ------------------------------------------------------

    def doctor(
        self,
        *,
        packs: Sequence[str] | None = None,
        model_root: Path | None = None,
        run: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    ) -> dict[str, Any]:
        """Explain this machine's readiness for the offline packs."""

        return capability_doctor(
            packs=packs,
            model_root=model_root,
            bundle=self,
            run=run,
        )


def uninstall_capability(
    pack: str,
    *,
    model_root: Path | None = None,
    confirmed: bool = False,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Remove one pack's model files, and only those.

    The Python distributions are reported rather than removed: an interpreter
    is shared with other tools, so dropping packages from it is an operator
    decision. Facts, evidence and the lexical index are never touched, which is
    why a removal never asks for a reindex.
    """

    if pack not in PACKS_BY_NAME:
        raise BundleError(
            BUNDLE_UNKNOWN_PACK,
            f"unknown capability pack {pack!r}; known packs are "
            f"{sorted(PACKS_BY_NAME)}",
        )
    root = Path(model_root) if model_root is not None else default_model_root()
    spec = PACKS_BY_NAME[pack]
    command = [
        python_executable or sys.executable,
        "-m",
        "pip",
        "uninstall",
        "-y",
        *[artifact.distribution for artifact in spec.python_artifacts],
    ]
    artifacts = {
        "kept": [artifact.distribution for artifact in spec.python_artifacts],
        "uninstall_command": command,
    }
    if not confirmed:
        return {
            "status": "confirmation_required",
            "pack": pack,
            "model_root": str(root),
            "directory": str(root / pack),
            "python_artifacts": artifacts,
            "message": (
                "removing a capability only removes its model files; documents, "
                "facts, evidence and the lexical index stay intact"
            ),
        }
    try:
        removed = ModelStore(root).remove(pack, confirmed=True)
    except OSError as error:
        # Windows reports a locked artifact here: the removal stops with the
        # reason and the surviving files instead of pretending it finished.
        survivors = sorted(
            str(path.relative_to(root / pack))
            for path in (root / pack).rglob("*")
            if path.is_file()
        )
        raise BundleError(
            REMOVAL_FAILED,
            f"{pack} could not be removed ({error}); "
            f"{len(survivors)} file(s) are still in place: "
            + ", ".join(survivors),
        ) from error
    return {
        **removed,
        "model_root": str(root),
        "python_artifacts": artifacts,
        "facts_untouched": True,
        "lexical_index_untouched": True,
        "requires_reindex": False,
    }


def capability_doctor(
    *,
    packs: Sequence[str] | None = None,
    model_root: Path | None = None,
    bundle: OfflineBundle | None = None,
    run: Callable[[Sequence[str]], tuple[int, str]] | None = None,
    executable: str | None = None,
) -> dict[str, Any]:
    """Everything an operator needs before an offline install on this machine."""

    root = Path(model_root) if model_root is not None else default_model_root()
    hardware = detect_hardware(root)
    names = list(packs) if packs else [pack.name for pack in PACKS]
    for name in names:
        if name not in PACKS_BY_NAME:
            raise BundleError(
                BUNDLE_UNKNOWN_PACK,
                f"unknown capability pack {name!r}; known packs are "
                f"{sorted(PACKS_BY_NAME)}",
            )

    disk = disk_report(root, names)
    tesseract = tesseract_report(executable=executable, run=run)
    paths = path_report(root)
    packed = [
        detect_pack(PACKS_BY_NAME[name], model_root=root, hardware=hardware).as_payload()
        for name in names
    ]
    verified = [bundle.verify(name) for name in names] if bundle else []

    actions: list[str] = []
    for name in names:
        shortfall = disk["packs"].get(name, {})
        if shortfall and not shortfall["ok"]:
            actions.append(
                f"free {shortfall['missing_free_disk_gb']} GB on "
                f"{shortfall['path']} before installing {name}"
            )
    for check in paths["checks"]:
        if not check["passed"]:
            actions.append(f"fix the model root: {check['detail']}")
    if not tesseract["available"]:
        actions.append(
            "install Tesseract or keep the compatibility fallback unavailable: "
            "no OCR engine will run"
        )
    elif tesseract["missing_languages"]:
        actions.append(
            "install the Tesseract language packs "
            f"{tesseract['missing_languages']} for the fallback to work"
        )
    for entry in verified:
        if entry["status"] != "verified":
            actions.append(f"repair the bundle for {entry['pack']}: {entry['detail']}")

    blocking = bool(
        [check for check in paths["checks"] if not check["passed"]]
        or [entry for entry in verified if entry["status"] != "verified"]
        or [
            shortfall
            for shortfall in disk["packs"].values()
            if not shortfall["ok"]
        ]
    )
    return {
        "status": "blocked" if blocking else "ready",
        "profile": {
            "recommended": hardware.recommended,
            "supported": list(HARDWARE_PROFILES),
            "hardware": hardware.as_payload(),
        },
        "model_root": str(root),
        "paths": paths,
        "disk": disk,
        "tesseract": tesseract,
        "packs": packed,
        "bundle": verified,
        "actions": actions,
        "policy": {
            "downloads": "never; a bundle is prepared elsewhere and copied in",
            "network_access": "none during install, verification or indexing",
        },
        "boundary": (
            "Acceptance records come from a Windows 10/11 machine: another "
            "platform's run is evidence about that platform only."
        ),
    }


def disk_report(root: Path, packs: Sequence[str]) -> dict[str, Any]:
    """Free space around the model root against every pack's declared need."""

    target = _existing_ancestor(Path(root))
    try:
        usage = shutil.disk_usage(target)
    except OSError as error:
        return {
            "path": str(target),
            "available": False,
            "error": str(error),
            "free_gb": None,
            "total_gb": None,
            "packs": {},
        }
    free_gb = round(usage.free / (1024**3), 2)
    entries: dict[str, Any] = {}
    for name in packs:
        spec = PACKS_BY_NAME[name]
        ok = free_gb >= spec.min_free_disk_gb
        entries[name] = {
            "ok": ok,
            "reason": "" if ok else DISK_TOO_SMALL,
            "path": str(target),
            "free_gb": free_gb,
            "required_free_disk_gb": spec.min_free_disk_gb,
            "installed_size_mb": spec.installed_size_mb,
            "missing_free_disk_gb": (
                0.0 if ok else round(spec.min_free_disk_gb - free_gb, 2)
            ),
        }
    return {
        "path": str(target),
        "available": True,
        "error": "",
        "free_gb": free_gb,
        "total_gb": round(usage.total / (1024**3), 2),
        "packs": entries,
    }


def path_report(root: Path) -> dict[str, Any]:
    """Whether the model root is a path Windows can actually install into."""

    raw = str(root)
    target = Path(root)
    absolute = target.is_absolute()
    ascii_only = raw.isascii()
    writable = os.access(_existing_ancestor(target), os.W_OK)
    short_enough = len(raw) <= 200
    drive = target.drive or ""
    checks = [
        _check(
            "absolute",
            absolute,
            f"{raw} is absolute" if absolute else f"{raw} is not an absolute path",
            reason=PATH_NOT_ABSOLUTE,
        ),
        _check(
            "writable",
            writable,
            f"{_existing_ancestor(target)} is writable"
            if writable
            else f"{_existing_ancestor(target)} is not writable",
            reason=PATH_NOT_WRITABLE,
        ),
        _check(
            "short_enough",
            short_enough,
            f"{len(raw)} characters"
            + ("" if short_enough else "; Windows long paths need enabling"),
            reason=PATH_LONG,
        ),
        {
            "check": "ascii",
            "passed": True,
            "informational": True,
            "reason": "",
            "detail": (
                "ASCII path"
                if ascii_only
                else "non-ASCII characters present; supported, and covered by "
                "the Windows native tests"
            ),
        },
    ]
    return {
        "path": raw,
        "drive": drive,
        "checks": checks,
        "blocking": [
            check["check"] for check in checks if not check["passed"]
        ],
    }


def tesseract_report(
    *,
    executable: str | None = None,
    available_languages: Sequence[str] | None = None,
    run: Callable[[Sequence[str]], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Whether the compatibility fallback can run, and with which languages.

    Tesseract is a fallback, not an equivalent default: it is reported with the
    languages it actually carries so a machine missing ``chi_sim`` says so
    rather than transcribing Chinese badly.
    """

    required = list(_required_languages())
    runner = run or _run_command
    path = executable or shutil.which("tesseract")
    if not path and available_languages is None:
        return {
            "available": False,
            "reason": TESSERACT_MISSING,
            "detail": "no tesseract executable on PATH",
            "path": "",
            "version": "",
            "languages": [],
            "required_languages": required,
            "missing_languages": required,
            "compatibility_fallback": "unavailable",
        }
    if available_languages is not None:
        available = [str(language) for language in available_languages]
        version = ""
    else:
        code, output = runner([str(path), "--version"])
        version = (
            output.strip().splitlines()[0] if code == 0 and output.strip() else ""
        )
        _, listing = runner([str(path), "--list-langs"])
        available = [
            line.strip()
            for line in listing.splitlines()
            if line.strip() and not line.lower().startswith("list of available")
        ]
    missing = [language for language in required if language not in available]
    return {
        "available": True,
        "reason": TESSERACT_LANGUAGE_PACK_MISSING if missing else "",
        "detail": (
            f"missing language packs {missing}"
            if missing
            else "the compatibility fallback can run with the required languages"
        ),
        "path": str(path or ""),
        "version": version,
        "languages": sorted(available),
        "required_languages": required,
        "missing_languages": missing,
        "compatibility_fallback": "ready" if not missing else "degraded",
    }


def capability_manifests() -> dict[str, dict[str, Any]]:
    """The pinned capability manifests this build ships, by pack name."""

    return {pack.name: pack.as_payload() for pack in PACKS}


def manifest_bytes(pack: CapabilityPack | str) -> bytes:
    """The exact bytes of one committed capability manifest."""

    spec = PACKS_BY_NAME[pack] if isinstance(pack, str) else pack
    return (
        json.dumps(spec.as_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_capability_manifests(directory: Path) -> dict[str, Any]:
    """Write the pinned manifests this build ships, one file per pack."""

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for pack in PACKS:
        path = target / f"{pack.name}.json"
        path.write_bytes(manifest_bytes(pack))
        written.append(str(path))
    return {
        "directory": str(target),
        "written": written,
        "sha256": {
            pack.name: file_sha256(target / f"{pack.name}.json") for pack in PACKS
        },
    }


def _required_languages() -> tuple[str, ...]:
    configured = os.environ.get(OCR_LANGUAGE_ENV)
    if not configured:
        return DEFAULT_TESSERACT_LANGUAGES
    languages = tuple(
        part.strip() for part in configured.split("+") if part.strip()
    )
    return languages or DEFAULT_TESSERACT_LANGUAGES


def _run_command(command: Sequence[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return (1, str(error))
    return (completed.returncode, f"{completed.stdout}\n{completed.stderr}")


def _verify_file(path: Path, expected: str) -> tuple[str, str]:
    if not expected:
        return ("", f"{path.name} carries no pinned checksum in the bundle manifest")
    if not path.is_file():
        return ("", f"{path} does not exist")
    digest = file_sha256(path)
    if digest != expected:
        return (
            digest,
            f"{path.name} sha256 {digest} does not match the pinned {expected}",
        )
    return (digest, "")


def _check(
    name: str, passed: bool, detail: str, *, reason: str = ""
) -> dict[str, Any]:
    return {
        "check": name,
        "passed": bool(passed),
        "detail": detail,
        "reason": "" if passed else reason,
    }


def _first_reason(checks: Sequence[Mapping[str, Any]]) -> str:
    for check in checks:
        if not check["passed"] and check.get("reason"):
            return str(check["reason"])
    return BUNDLE_CHECKSUM_MISMATCH if checks else BUNDLE_PACK_ABSENT


def _first_detail(checks: Sequence[Mapping[str, Any]]) -> str:
    for check in checks:
        if not check["passed"]:
            return str(check["detail"])
    return ""


def _existing_ancestor(path: Path) -> Path:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    return target


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(str(text or "").strip().splitlines()[-lines:])


__all__ = [
    "BUNDLE_ARTIFACT_MISSING",
    "BUNDLE_CHECKSUM_MISMATCH",
    "BUNDLE_MANIFEST_FILENAME",
    "BUNDLE_MANIFEST_INVALID",
    "BUNDLE_MISSING",
    "BUNDLE_PACK_ABSENT",
    "BUNDLE_PIN_MISMATCH",
    "BUNDLE_PLATFORM_MISMATCH",
    "BUNDLE_PYTHON_MISMATCH",
    "BUNDLE_REASONS",
    "BUNDLE_UNKNOWN_PACK",
    "BUNDLE_VERSION",
    "BundleError",
    "BundlePlatform",
    "DEFAULT_TESSERACT_LANGUAGES",
    "DISK_TOO_SMALL",
    "MODELS_DIRECTORY",
    "OCR_LANGUAGE_ENV",
    "OfflineBundle",
    "REMOVAL_FAILED",
    "REQUIREMENTS_FILENAME",
    "TESSERACT_LANGUAGE_PACK_MISSING",
    "TESSERACT_MISSING",
    "WHEELS_DIRECTORY",
    "capability_doctor",
    "capability_manifests",
    "disk_report",
    "host_platform",
    "manifest_bytes",
    "path_report",
    "platform_compatible",
    "tesseract_report",
    "uninstall_capability",
    "write_capability_manifests",
]
