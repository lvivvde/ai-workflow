"""Capability packs, hardware profiles, and the runtime that loads them.

Everything heavy in this project is optional and installed explicitly: OCR
engines, layout analysis, and local visual understanding. Model artifacts are
installed once, verified against a pinned checksum, then loaded on demand and
released on idle timeout, low-memory mode, explicit unload, or process exit.

Nothing here downloads anything. Detection only looks at what is already on
this machine, and every answer carries the checks that produced it, so a
degraded run can explain itself instead of pretending the fallback is the
capability that was requested.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from .revisions import file_sha256
from .run_records import profile_budget


PACK_STATUSES = (
    "available",
    "degraded",
    "not_installed",
    "insufficient_resources",
    "self_check_failed",
)
HARDWARE_PROFILES = ("baseline", "recommended", "visual")

# One pack install status maps to exactly one stage execution status, so
# degradation reporting cannot drift away from the capability check.
PACK_TO_EXECUTION_STATUS = {
    "available": "succeeded",
    "degraded": "partial",
    "not_installed": "unavailable",
    "insufficient_resources": "unavailable",
    "self_check_failed": "failed",
}

MODEL_STORAGE_ENV = "GAME_DESIGN_MODEL_DIR"


@dataclass(frozen=True)
class PythonArtifact:
    """One pinned Python distribution a pack requires."""

    distribution: str
    module: str
    version: str
    purpose: str


@dataclass(frozen=True)
class PythonDependency:
    """One pinned distribution that rides along with a pack's artifacts.

    These are the transitive distributions the pack's own artifacts need. They
    are pinned for one reason: an offline install runs pip in
    ``--require-hashes`` mode, so every distribution pip installs has to come
    from the requirements file with a hash. A bundle that carried only the
    artifacts could never satisfy its own dependencies, and a bundle that
    carried unpinned extras would be refused by the pin check.

    They are deliberately *not* artifacts: the pack does not claim to own them,
    they may already be installed for another reason, and unloading a pack must
    not remove them.
    """

    distribution: str
    version: str
    purpose: str


@dataclass(frozen=True)
class ModelArtifact:
    """One pinned model file, stored outside the project's SQLite data."""

    relative_path: str
    sha256: str
    purpose: str


@dataclass(frozen=True)
class CapabilityPack:
    """A separately installable, versioned capability."""

    name: str
    version: str
    purpose: str
    license: str
    tier: str
    min_cpu_cores: int
    min_ram_gb: float
    min_free_disk_gb: float
    download_size_mb: float
    installed_size_mb: float
    python_artifacts: tuple[PythonArtifact, ...] = ()
    python_dependencies: tuple[PythonDependency, ...] = ()
    model_artifacts: tuple[ModelArtifact, ...] = ()
    builtin_components: tuple[str, ...] = ()
    optional: bool = True
    idle_timeout_seconds: float = 300.0
    notes: tuple[str, ...] = ()

    def pinned_python(self) -> dict[str, str]:
        """Every Python distribution this pack's bundle has to carry.

        Artifacts come first, and win where a distribution is both: the
        artifact entry is the one that carries a module name and an ownership
        claim.
        """

        pinned = {
            artifact.distribution: artifact.version
            for artifact in self.python_artifacts
        }
        for dependency in self.python_dependencies:
            pinned.setdefault(dependency.distribution, dependency.version)
        return pinned

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "tier": self.tier,
            "purpose": self.purpose,
            "license": self.license,
            "optional": self.optional,
            "requirements": {
                "cpu_cores": self.min_cpu_cores,
                "ram_gb": self.min_ram_gb,
                "free_disk_gb": self.min_free_disk_gb,
            },
            "sizes_mb": {
                "download": self.download_size_mb,
                "installed": self.installed_size_mb,
            },
            "python_artifacts": [
                {
                    "distribution": artifact.distribution,
                    "module": artifact.module,
                    "version": artifact.version,
                    "purpose": artifact.purpose,
                }
                for artifact in self.python_artifacts
            ],
            "python_dependencies": [
                {
                    "distribution": dependency.distribution,
                    "version": dependency.version,
                    "purpose": dependency.purpose,
                }
                for dependency in self.python_dependencies
            ],
            "model_artifacts": [
                {
                    "relative_path": artifact.relative_path,
                    "sha256": artifact.sha256,
                    "purpose": artifact.purpose,
                }
                for artifact in self.model_artifacts
            ],
            "builtin_components": list(self.builtin_components),
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "notes": list(self.notes),
        }


CORE_PACK = CapabilityPack(
    name="core",
    version="1.0.0",
    tier="core",
    purpose=(
        "Deterministic OOXML parsing, SQLite/FTS storage, OpenCV geometry, and "
        "RapidOCR on ONNX Runtime for ordinary image text."
    ),
    license="Apache-2.0 (OpenCV, ONNX Runtime, RapidOCR); MIT (this project)",
    min_cpu_cores=4,
    min_ram_gb=8.0,
    min_free_disk_gb=4.0,
    download_size_mb=420.0,
    installed_size_mb=1150.0,
    python_artifacts=(
        PythonArtifact("opencv-python", "cv2", "4.10.0.84", "deterministic image geometry"),
        PythonArtifact(
            "rapidocr-onnxruntime",
            "rapidocr_onnxruntime",
            "1.3.24",
            "core OCR engine",
        ),
        PythonArtifact("onnxruntime", "onnxruntime", "1.19.2", "CPU inference runtime"),
    ),
    # The pinned closure of the three artifacts above, resolved for Windows
    # x64 / CPython 3.12. Keep it complete: an offline pip run in hash-checking
    # mode installs the closure from the bundle, and a missing member turns a
    # valid bundle into an install that fails halfway.
    python_dependencies=(
        PythonDependency("numpy", "1.26.4", "array backend for RapidOCR and OpenCV"),
        PythonDependency("shapely", "2.1.2", "RapidOCR geometry"),
        PythonDependency("pyclipper", "1.4.0", "RapidOCR polygon clipping"),
        PythonDependency("pillow", "12.3.0", "image decoding"),
        PythonDependency("pyyaml", "6.0.3", "RapidOCR configuration"),
        PythonDependency("six", "1.17.0", "RapidOCR compatibility shim"),
        PythonDependency("packaging", "26.3", "version parsing"),
        PythonDependency("coloredlogs", "15.0.1", "onnxruntime console output"),
        PythonDependency("humanfriendly", "10.0", "coloredlogs formatting"),
        PythonDependency("pyreadline3", "3.5.6", "humanfriendly console support"),
        PythonDependency("flatbuffers", "25.12.19", "onnxruntime serialization"),
        PythonDependency("protobuf", "7.36.2", "onnxruntime serialization"),
        PythonDependency("sympy", "1.14.0", "onnxruntime symbolic shapes"),
        PythonDependency("mpmath", "1.3.0", "sympy arbitrary precision"),
    ),
    builtin_components=(
        "ooxml-parser",
        "sqlite-fts5-store",
        "atomic-snapshot-publisher",
    ),
    optional=False,
    notes=(
        "The builtin components ship with the project itself; only the OCR "
        "artifacts have to be installed.",
    ),
)

ENHANCED_OCR_PACK = CapabilityPack(
    name="enhanced_ocr",
    version="1.0.0",
    tier="enhanced",
    purpose="PaddleOCR and PP-StructureV3 for difficult OCR and richer layout analysis.",
    license="Apache-2.0 (PaddleOCR, PaddleX)",
    min_cpu_cores=8,
    min_ram_gb=16.0,
    min_free_disk_gb=4.0,
    download_size_mb=400.0,
    installed_size_mb=1030.0,
    python_artifacts=(
        PythonArtifact("paddleocr", "paddleocr", "3.7.0", "enhanced OCR"),
        PythonArtifact("paddlex", "paddlex", "3.7.2", "layout analysis"),
        PythonArtifact("paddlepaddle", "paddle", "3.3.1", "enhanced OCR runtime"),
    ),
    # The pinned closure of the three artifacts above, resolved for Windows
    # x64 / CPython 3.12 and recorded wheel by wheel in
    # ``capabilities/locks/enhanced_ocr-win_amd64-cp312.json``. The pins that
    # matter to a resolver are the ones that overlap core (numpy, protobuf,
    # pillow, packaging, httpx), and they are the same versions core pins, so
    # the two packs differ only where PaddleX leaves no choice.
    python_dependencies=(
        PythonDependency("aiohappyeyeballs", "2.7.1", "aiohttp connection helpers"),
        PythonDependency("aiohttp", "3.14.3", "PaddleOCR client transport"),
        PythonDependency("aiosignal", "1.4.0", "aiohttp signal plumbing"),
        PythonDependency("aistudio-sdk", "0.3.9", "PaddleX model source"),
        PythonDependency("annotated-types", "0.8.0", "pydantic annotations"),
        PythonDependency("anyio", "4.15.1", "httpx async backend"),
        PythonDependency("attrs", "26.1.0", "ModelScope record types"),
        PythonDependency("bce-python-sdk", "0.9.79", "ModelScope object storage"),
        PythonDependency("certifi", "2026.7.22", "TLS root certificates"),
        PythonDependency("cffi", "2.1.1", "cryptography bindings"),
        PythonDependency("chardet", "7.6.0", "PaddleX text encoding detection"),
        PythonDependency("charset-normalizer", "3.5.1", "requests encoding detection"),
        PythonDependency("click", "8.5.0", "PaddleX console entry points"),
        PythonDependency("colorama", "0.4.6", "click console colours"),
        PythonDependency("colorlog", "6.12.0", "PaddleX console logging"),
        PythonDependency("crc32c", "2.9.post0", "ModelScope transfer checksums"),
        PythonDependency("cryptography", "50.0.1", "TLS for the model hosters"),
        PythonDependency("filelock", "4.0.1", "PaddleX model cache locking"),
        PythonDependency("frozenlist", "1.8.0", "aiohttp registry state"),
        PythonDependency("fsspec", "2026.9.0", "huggingface-hub filesystem layer"),
        PythonDependency("future", "1.0.0", "bce-python-sdk compatibility"),
        PythonDependency("h11", "0.16.0", "HTTP/1.1 protocol"),
        PythonDependency("hf-xet", "1.6.0", "huggingface-hub transfers"),
        PythonDependency("httpcore", "1.0.9", "httpx transport"),
        PythonDependency("httpx", "0.27.2", "PaddlePaddle HTTP client"),
        PythonDependency("huggingface-hub", "1.32.0", "PaddleX model source"),
        PythonDependency("idna", "3.20", "internationalized domain names"),
        PythonDependency("imagesize", "2.0.1", "PaddleX image metadata"),
        PythonDependency("modelscope", "1.40.1", "PaddleX model source"),
        PythonDependency("modelscope-hub", "0.4.5", "ModelScope hub transfers"),
        PythonDependency("multidict", "6.9.1", "aiohttp headers"),
        PythonDependency("networkx", "3.7", "PaddlePaddle graph utilities"),
        PythonDependency("numpy", "1.26.4", "array backend shared with core"),
        PythonDependency("opencv-contrib-python", "4.10.0.84", "PaddleX image operators"),
        PythonDependency("opt-einsum", "3.3.0", "PaddlePaddle tensor contraction"),
        PythonDependency("packaging", "26.3", "version parsing"),
        PythonDependency("pandas", "3.0.6", "PaddleX tabular results"),
        PythonDependency("pillow", "12.3.0", "image decoding"),
        PythonDependency("prettytable", "3.18.0", "PaddleX console tables"),
        PythonDependency("propcache", "0.5.4", "aiohttp property cache"),
        PythonDependency("protobuf", "7.36.2", "PaddlePaddle serialization"),
        PythonDependency("psutil", "7.2.2", "PaddleX resource reporting"),
        PythonDependency("py-cpuinfo", "9.0.0", "PaddleX CPU capability report"),
        PythonDependency("pyclipper", "1.4.0", "PaddleX polygon clipping"),
        PythonDependency("pycparser", "3.0", "cffi parser"),
        PythonDependency("pycryptodome", "3.23.0", "bce-python-sdk signing"),
        PythonDependency("pydantic", "2.13.5", "PaddleX configuration models"),
        PythonDependency("pydantic-core", "2.46.5", "pydantic core"),
        PythonDependency("pypdfium2", "5.13.0", "PaddleX PDF input"),
        PythonDependency("python-bidi", "0.6.11", "PaddleX text ordering"),
        PythonDependency("python-dateutil", "2.9.0.post0", "pandas timestamps"),
        PythonDependency("pyyaml", "6.0.2", "PaddleX pipeline configuration"),
        PythonDependency("requests", "2.34.2", "PaddleX HTTP"),
        PythonDependency("ruamel.yaml", "0.19.1", "PaddleX round-trip YAML"),
        PythonDependency("safetensors", "0.8.0", "PaddlePaddle weight loading"),
        PythonDependency("setuptools", "84.0.0", "PaddlePaddle on Python 3.12"),
        PythonDependency("shapely", "2.1.2", "PaddleX geometry"),
        PythonDependency("six", "1.17.0", "python-dateutil compatibility"),
        PythonDependency("sniffio", "1.3.1", "async backend detection"),
        PythonDependency("tqdm", "4.70.1", "ModelScope progress"),
        PythonDependency("typing-extensions", "4.16.0", "typed public API"),
        PythonDependency("typing-inspection", "0.4.4", "pydantic introspection"),
        PythonDependency("tzdata", "2026.4", "pandas timezone data"),
        PythonDependency("ujson", "6.0.0", "PaddleX JSON"),
        PythonDependency("urllib3", "2.8.0", "requests transport"),
        PythonDependency("wcwidth", "0.9.0", "prettytable column widths"),
        PythonDependency("yarl", "1.25.1", "aiohttp URL handling"),
    ),
    notes=(
        "Targets the PaddleOCR 3.x line, because PP-StructureV3 only exists "
        "there and paddlepaddle 3.3.1 is the runtime paddlex 3.7.2 was built "
        "against. The handler's API call is generation-neutral: it prefers "
        "``predict()`` and keeps the 2.x ``ocr()`` path.",
        "paddlex hard-pins PyYAML==6.0.2 while core pins 6.0.3, and "
        "paddlex[ocr-core] brings opencv-contrib-python where core brings "
        "opencv-python. Two packs installed into one interpreter therefore end "
        "with only the later pack's literal pins satisfied; the closure check "
        "reads bundles, never an interpreter's installed set.",
        "Declared sizes split the two halves: the bundle carries 211 MB of "
        "wheels, and the engine's own model files are not in it. PaddleX "
        "fetches those the first time a pipeline is built (192 MB, seven "
        "models, measured on Windows x64), so an air-gapped machine has to "
        "pre-place that cache; this pack does not pin those files yet.",
        "The pinned paddlepaddle cannot lower these models through its oneDNN "
        "path: every PP-OCR version fails with "
        "``ConvertPirAttribute2RuntimeAttribute not support "
        "[pir::ArrayAttribute<pir::DoubleAttribute>]``. The handler therefore "
        "constructs PaddleOCR with ``enable_mkldnn=False``, which trades CPU "
        "speed for an engine that runs.",
    ),
)

# The pin file is the pack's own model identity: which runtime, which model, and
# which quantization the pack was built for. The tensor files themselves stay
# under the runtime's storage and are pinned by this descriptor.
VISUAL_MODEL_PIN = {
    "model": "qwen2.5-vl:3b",
    "runtime": "ollama",
    "quantization": "q4_K_M",
    "authority": "coarse image explanation only; never project evidence",
}
VISUAL_MODEL_PIN_PATH = "qwen2.5-vl-3b/pin.json"


def visual_model_pin_bytes() -> bytes:
    """The exact bytes a visual pack ships as its model pin."""

    return (
        json.dumps(
            VISUAL_MODEL_PIN, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


# The pin's checksum is taken over the bytes the pack actually ships. Hashing a
# different serialization would let an installation verify a file nobody wrote.
VISUAL_MODEL_PIN_SHA256 = hashlib.sha256(visual_model_pin_bytes()).hexdigest()


VISUAL_PACK = CapabilityPack(
    name="visual",
    version="1.0.0",
    tier="visual",
    purpose=(
        "Ollama with a small Qwen vision model for coarse image explanation. "
        "Disabled by default and never authoritative project evidence."
    ),
    license="MIT (Ollama); Apache-2.0 (Qwen2.5-VL)",
    min_cpu_cores=8,
    min_ram_gb=16.0,
    min_free_disk_gb=20.0,
    download_size_mb=3200.0,
    installed_size_mb=3400.0,
    python_artifacts=(
        PythonArtifact("ollama", "ollama", "0.3.3", "local model runtime"),
    ),
    python_dependencies=(
        PythonDependency("httpx", "0.27.2", "ollama client transport"),
        PythonDependency("httpcore", "1.0.9", "httpx transport"),
        PythonDependency("h11", "0.16.0", "HTTP/1.1 protocol"),
        PythonDependency("anyio", "4.15.1", "httpx async backend"),
        PythonDependency("sniffio", "1.3.1", "async backend detection"),
        PythonDependency("certifi", "2026.7.22", "TLS root certificates"),
        PythonDependency("idna", "3.20", "internationalized domain names"),
        PythonDependency("typing-extensions", "4.16.0", "typed public API"),
    ),
    model_artifacts=(
        ModelArtifact(
            VISUAL_MODEL_PIN_PATH,
            VISUAL_MODEL_PIN_SHA256,
            "pins the runtime, model, and quantization this pack was built for",
        ),
    ),
    idle_timeout_seconds=300.0,
    notes=(
        "Low-memory mode unloads the vision model after every batch.",
        "The model loads on demand and is never kept as an unexplained "
        "background process.",
    ),
)

PACKS: tuple[CapabilityPack, ...] = (CORE_PACK, ENHANCED_OCR_PACK, VISUAL_PACK)
PACKS_BY_NAME = {pack.name: pack for pack in PACKS}


@dataclass(frozen=True)
class HardwareProfile:
    """The machine as detected, plus the profile it recommends."""

    os_name: str
    os_version: str
    architecture: str
    cpu_cores: int
    ram_gb: float | None
    free_disk_gb: float | None
    gpu: str
    recommended: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "os": self.os_name,
            "os_version": self.os_version,
            "architecture": self.architecture,
            "cpu_cores": self.cpu_cores,
            "ram_gb": self.ram_gb,
            "free_disk_gb": self.free_disk_gb,
            "gpu": self.gpu,
            "recommended_profile": self.recommended,
        }

    def within(self, pack: CapabilityPack) -> tuple[bool, list[str]]:
        """Whether this machine meets a pack's declared minimums."""

        shortfalls: list[str] = []
        if self.cpu_cores < pack.min_cpu_cores:
            shortfalls.append(f"{pack.min_cpu_cores} CPU cores (found {self.cpu_cores})")
        if self.ram_gb is not None and self.ram_gb < pack.min_ram_gb:
            shortfalls.append(f"{pack.min_ram_gb} GB RAM (found {self.ram_gb} GB)")
        if self.free_disk_gb is not None and self.free_disk_gb < pack.min_free_disk_gb:
            shortfalls.append(
                f"{pack.min_free_disk_gb} GB free disk (found {self.free_disk_gb} GB)"
            )
        if pack.name == "visual" and self.gpu == "none":
            shortfalls.append("a discrete GPU (none detected)")
        return not shortfalls, shortfalls


def detect_hardware(reference: Path | None = None) -> HardwareProfile:
    """Detect this machine and recommend one of the three declared profiles."""

    cores = os.cpu_count() or 1
    ram_gb = _total_ram_gb()
    disk_gb = _free_disk_gb(reference)
    gpu = _detect_gpu()
    if gpu != "none" and cores >= 8 and (ram_gb or 0) >= 16:
        recommended = "visual"
    elif cores >= 8 and (ram_gb or 0) >= 16:
        recommended = "recommended"
    else:
        recommended = "baseline"
    return HardwareProfile(
        os_name=platform.system(),
        os_version=platform.release(),
        architecture=platform.machine(),
        cpu_cores=cores,
        ram_gb=ram_gb,
        free_disk_gb=disk_gb,
        gpu=gpu,
        recommended=recommended,
    )


@dataclass(frozen=True)
class PackStatus:
    """Detection result for one pack, with every check that produced it."""

    name: str
    version: str
    tier: str
    status: str
    purpose: str
    license: str
    optional: bool
    execution_status: str
    installed_version: str
    declared_installed_size_mb: float
    missing: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]
    reason: str

    @property
    def usable(self) -> bool:
        return self.status in ("available", "degraded")

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "tier": self.tier,
            "status": self.status,
            "execution_status": self.execution_status,
            "purpose": self.purpose,
            "license": self.license,
            "optional": self.optional,
            "installed_version": self.installed_version,
            "declared_installed_size_mb": self.declared_installed_size_mb,
            "missing": list(self.missing),
            "reason": self.reason,
            "checks": [dict(check) for check in self.checks],
        }


def detect_pack(
    pack: CapabilityPack | str,
    *,
    model_root: Path | None = None,
    hardware: HardwareProfile | None = None,
) -> PackStatus:
    """Report whether a pack is installed, complete, and affordable here."""

    pack = _resolve_pack(pack)
    root = Path(model_root) if model_root is not None else default_model_root()
    hardware = hardware or detect_hardware(root)
    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    versions: list[str] = []
    broken: list[str] = []

    for artifact in pack.python_artifacts:
        found = importlib.util.find_spec(artifact.module) is not None
        checks.append(
            _check(
                f"python:{artifact.distribution}",
                found,
                f"{artifact.distribution}=={artifact.version}"
                + ("" if found else " is not installed"),
            )
        )
        if not found:
            missing.append(f"{artifact.distribution}=={artifact.version}")
            continue
        installed = _installed_version(artifact.distribution)
        versions.append(installed)
        if installed and installed != artifact.version:
            checks.append(
                _check(
                    f"version:{artifact.distribution}",
                    False,
                    f"installed {installed}, pinned {artifact.version}",
                )
            )
            broken.append(f"{artifact.distribution} {installed} != {artifact.version}")

    for component in pack.builtin_components:
        checks.append(_check(f"builtin:{component}", True, "ships with this project"))

    for artifact in pack.model_artifacts:
        path = root / pack.name / artifact.relative_path
        if not path.is_file():
            checks.append(
                _check(
                    f"model:{artifact.relative_path}",
                    False,
                    f"missing model artifact {path}",
                )
            )
            missing.append(artifact.relative_path)
            continue
        digest = file_sha256(path)
        verified = digest == artifact.sha256
        checks.append(
            _check(
                f"model:{artifact.relative_path}",
                verified,
                f"sha256 {digest}" if verified else f"sha256 {digest} does not match the pin",
            )
        )
        if not verified:
            broken.append(artifact.relative_path)

    fits, shortfalls = hardware.within(pack)
    checks.append(
        _check(
            "hardware",
            fits,
            "meets declared minimums" if fits else "; ".join(shortfalls),
        )
    )

    external = len(pack.python_artifacts) + len(pack.model_artifacts)
    installed_external = external - len(missing)
    if broken:
        status = "self_check_failed"
    elif missing and installed_external == 0 and not pack.builtin_components:
        status = "not_installed"
    elif missing:
        status = "degraded" if pack.builtin_components else "not_installed"
    elif not fits:
        status = "insufficient_resources"
    else:
        status = "available"

    if status == "available":
        reason = f"{pack.name} {pack.version} is installed and verified"
    elif status == "degraded":
        reason = (
            f"{pack.name} is partially installed ({installed_external}/{external} "
            f"artifacts present); missing {missing}"
        )
    elif status == "not_installed":
        reason = f"{pack.name} is not installed; missing {missing}"
    elif status == "insufficient_resources":
        reason = (
            f"this machine does not meet {pack.name} minimums: {'; '.join(shortfalls)}"
        )
    else:
        reason = f"{pack.name} failed its self check: {broken}"

    return PackStatus(
        name=pack.name,
        version=pack.version,
        tier=pack.tier,
        status=status,
        purpose=pack.purpose,
        license=pack.license,
        optional=pack.optional,
        execution_status=PACK_TO_EXECUTION_STATUS[status],
        installed_version=", ".join(version for version in versions if version),
        declared_installed_size_mb=pack.installed_size_mb,
        missing=tuple(missing),
        checks=tuple(checks),
        reason=reason,
    )


def detect_all_packs(
    *, model_root: Path | None = None, hardware: HardwareProfile | None = None
) -> list[PackStatus]:
    hardware = hardware or detect_hardware(model_root)
    return [detect_pack(pack, model_root=model_root, hardware=hardware) for pack in PACKS]


def default_model_root() -> Path:
    """Where model artifacts live: outside the project's SQLite data."""

    override = os.environ.get(MODEL_STORAGE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "game-design-knowledge" / "models"
    return Path.home() / ".local" / "share" / "game-design-knowledge" / "models"


class ModelStore:
    """Install, verify, inspect, and remove model artifacts explicitly.

    Installation reads a local directory only. There is deliberately no
    download path in this class, so "no silent model download" is a property of
    the code rather than a promise in the documentation.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_model_root()

    def path_for(self, pack: CapabilityPack | str, relative_path: str) -> Path:
        pack = _resolve_pack(pack)
        return self.root / pack.name / relative_path

    def inspect(self) -> dict[str, Any]:
        packs = []
        for pack in PACKS:
            if not pack.model_artifacts:
                continue
            packs.append(
                {
                    "pack": pack.name,
                    "directory": str(self.root / pack.name),
                    "declared": [a.relative_path for a in pack.model_artifacts],
                    "present": [
                        artifact.relative_path
                        for artifact in pack.model_artifacts
                        if (self.root / pack.name / artifact.relative_path).is_file()
                    ],
                }
            )
        return {
            "model_root": str(self.root),
            "packs": packs,
            "note": (
                "Model storage is separate from project data; removing it never "
                "deletes documents, facts, evidence, or parse revisions."
            ),
        }

    def verify(self, pack: CapabilityPack | str) -> dict[str, Any]:
        pack = _resolve_pack(pack)
        checks: list[dict[str, Any]] = []
        for artifact in pack.model_artifacts:
            path = self.root / pack.name / artifact.relative_path
            if not path.is_file():
                checks.append(_check(artifact.relative_path, False, "not installed"))
                continue
            digest = file_sha256(path)
            checks.append(
                _check(artifact.relative_path, digest == artifact.sha256, f"sha256 {digest}")
            )
        return {
            "pack": pack.name,
            "ok": all(check["passed"] for check in checks),
            "checks": checks,
        }

    def install(
        self,
        pack: CapabilityPack | str,
        source_directory: Path,
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Copy declared artifacts from a local directory after checksum checks."""

        pack = _resolve_pack(pack)
        if not confirmed:
            return {
                "status": "confirmation_required",
                "pack": pack.name,
                "source": str(Path(source_directory)),
                "artifacts": [a.relative_path for a in pack.model_artifacts],
                "declared_download_size_mb": pack.download_size_mb,
                "model_root": str(self.root),
                "message": (
                    "Installing a capability pack copies model files into "
                    f"{self.root}; confirm before applying."
                ),
            }
        if not pack.model_artifacts:
            return {
                "status": "no_model_artifacts",
                "pack": pack.name,
                "message": (
                    f"{pack.name} is installed through its Python distributions; "
                    "there is nothing to copy into the model store."
                ),
            }

        source_directory = Path(source_directory).resolve()
        installed: list[dict[str, Any]] = []
        for artifact in pack.model_artifacts:
            source = source_directory / artifact.relative_path
            if not source.is_file():
                raise FileNotFoundError(
                    f"{pack.name} cannot be installed from {source_directory}: "
                    f"{artifact.relative_path} is missing"
                )
            digest = file_sha256(source)
            if digest != artifact.sha256:
                raise ValueError(
                    f"{artifact.relative_path} does not match the pinned checksum "
                    f"(expected {artifact.sha256}, found {digest})"
                )
            destination = self.root / pack.name / artifact.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            installed.append({"relative_path": artifact.relative_path, "sha256": digest})
        return {
            "status": "installed",
            "pack": pack.name,
            "model_root": str(self.root),
            "installed": installed,
        }

    def remove(self, pack: CapabilityPack | str, *, confirmed: bool) -> dict[str, Any]:
        pack = _resolve_pack(pack)
        directory = self.root / pack.name
        if not confirmed:
            return {
                "status": "confirmation_required",
                "pack": pack.name,
                "directory": str(directory),
                "message": (
                    "Removing model storage only removes model files; documents, "
                    "facts, evidence, and parse revisions stay intact."
                ),
            }
        if not directory.is_dir():
            return {
                "status": "not_installed",
                "pack": pack.name,
                "directory": str(directory),
            }
        shutil.rmtree(directory)
        return {"status": "removed", "pack": pack.name, "directory": str(directory)}


class CapabilityRuntime:
    """Load capability packs on demand and release them predictably."""

    def __init__(
        self,
        *,
        model_root: Path | None = None,
        idle_timeout: float | None = None,
        low_memory: bool = False,
        profile: str | None = None,
        hardware: HardwareProfile | None = None,
        clock: Callable[[], float] = time.monotonic,
        exit_hook: bool = True,
    ) -> None:
        self.hardware = hardware or detect_hardware(model_root)
        self.model_root = (
            Path(model_root) if model_root is not None else default_model_root()
        )
        self.low_memory = low_memory
        self.profile = profile or self.hardware.recommended
        self._clock = clock
        self._lock = threading.RLock()
        self._residency: dict[str, dict[str, Any]] = {}
        self._idle_timeout = idle_timeout
        self._batch_depth = 0
        self.events: list[dict[str, Any]] = []
        self.exit_hook_registered = False
        self.loader: Callable[[CapabilityPack], Any] = self._default_loader
        if exit_hook:
            self.attach_exit_hook()

    # -- lifecycle ---------------------------------------------------------

    def attach_exit_hook(self) -> None:
        """Release residency when the process exits, including on Windows."""

        atexit.register(self.release_all)
        self.exit_hook_registered = True

    def idle_timeout_for(self, pack: CapabilityPack) -> float:
        if self._idle_timeout is not None:
            return max(float(self._idle_timeout), 0.0)
        if self.low_memory:
            return 0.0
        return pack.idle_timeout_seconds

    def acquire(self, pack: CapabilityPack | str) -> dict[str, Any]:
        """Load a pack on first use and reuse it for the rest of the batch."""

        resolved = _resolve_pack(pack)
        with self._lock:
            self._sweep_locked()
            status = detect_pack(
                resolved, model_root=self.model_root, hardware=self.hardware
            )
            resident = self._residency.get(resolved.name)
            reusable = (
                resident is not None
                and resident["status"] == status.status
                and status.usable
            )
            if reusable:
                resident["last_used"] = self._clock()
                return dict(resident)
            if resident is not None:
                self._release_locked(resolved.name, "capability status changed")
            handle = self.loader(resolved)
            self._residency[resolved.name] = {
                "pack": resolved.name,
                "status": status.status,
                "execution_status": status.execution_status,
                "loaded_at": _now(),
                "last_used": self._clock(),
                "handle": handle,
                "idle_timeout_seconds": self.idle_timeout_for(resolved),
            }
            self.events.append(
                {
                    "event": "load",
                    "pack": resolved.name,
                    "at": _now(),
                    "status": status.status,
                }
            )
            return dict(self._residency[resolved.name])

    def release(self, pack: CapabilityPack | str) -> bool:
        resolved = _resolve_pack(pack)
        with self._lock:
            return self._release_locked(resolved.name, "explicit unload")

    def release_all(self) -> list[str]:
        with self._lock:
            return [
                name
                for name in list(self._residency)
                if self._release_locked(name, "release all")
            ]

    def sweep(self) -> list[str]:
        """Release packs idle beyond their timeout."""

        with self._lock:
            return self._sweep_locked()

    def residency(self) -> dict[str, Any]:
        with self._lock:
            return {
                "packs": [
                    {
                        "pack": entry["pack"],
                        "status": entry["status"],
                        "loaded_at": entry["loaded_at"],
                        "idle_timeout_seconds": entry["idle_timeout_seconds"],
                    }
                    for entry in self._residency.values()
                ],
                "low_memory": self.low_memory,
                "profile": self.profile,
                "exit_hook_registered": self.exit_hook_registered,
            }

    def batch(self) -> "CapabilityRuntime":
        self._batch_depth += 1
        return self

    def end_batch(self) -> list[str]:
        """Close a batch scope: low-memory mode unloads everything here."""

        self._batch_depth = max(self._batch_depth - 1, 0)
        if self.low_memory and self._batch_depth == 0:
            return self.release_all()
        return []

    def __enter__(self) -> "CapabilityRuntime":
        return self.batch()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.end_batch()

    # -- reporting ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "recommended_profile": self.hardware.recommended,
            "low_memory": self.low_memory,
            "hardware": self.hardware.as_payload(),
            "model_root": str(self.model_root),
            "packs": [
                status.as_payload()
                for status in detect_all_packs(
                    model_root=self.model_root, hardware=self.hardware
                )
            ],
            "residency": self.residency(),
            "limits": {
                # One source of truth: the profile's budget in run_records.
                **profile_budget(self.profile),
            },
            "policy": {
                "downloads": "never automatic; install explicitly from a local directory",
                "telemetry": "none",
                "background_scanning": "none",
            },
        }

    # -- internals ---------------------------------------------------------

    def _default_loader(self, pack: CapabilityPack) -> None:
        """This runtime loads nothing by itself.

        OCR engines are command-line or library calls made at the moment of
        transcription, so there is no long-running process to leak. A pack that
        does hold a resident model overrides this through ``runtime.loader``.
        """

        return None

    def _sweep_locked(self) -> list[str]:
        now = self._clock()
        released: list[str] = []
        for name, entry in list(self._residency.items()):
            timeout = entry["idle_timeout_seconds"]
            if timeout is None:
                continue
            if now - float(entry["last_used"]) >= float(timeout):
                if self._release_locked(name, "idle timeout"):
                    released.append(name)
        return released

    def _release_locked(self, name: str, reason: str) -> bool:
        entry = self._residency.pop(name, None)
        if entry is None:
            return False
        handle = entry.get("handle")
        closer = getattr(handle, "close", None)
        if callable(closer):
            closer()
        self.events.append(
            {"event": "release", "pack": name, "at": _now(), "reason": reason}
        )
        return True


def _resolve_pack(pack: CapabilityPack | str) -> CapabilityPack:
    if isinstance(pack, CapabilityPack):
        return pack
    try:
        return PACKS_BY_NAME[pack]
    except KeyError as error:
        raise KeyError(
            f"unknown capability pack {pack!r}; known packs are {sorted(PACKS_BY_NAME)}"
        ) from error


def _installed_version(distribution: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(distribution)
    except PackageNotFoundError:
        return ""


def _total_ram_gb() -> float | None:
    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / (1024**3), 1)
        except (AttributeError, OSError, ValueError):
            return None
        return None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    return round((pages * page_size) / (1024**3), 1)


def _free_disk_gb(reference: Path | None) -> float | None:
    try:
        target = Path(reference) if reference is not None else Path.cwd()
        while not target.exists() and target.parent != target:
            target = target.parent
        return round(shutil.disk_usage(target).free / (1024**3), 1)
    except OSError:
        return None


def _detect_gpu() -> str:
    if os.name != "nt":
        return "none"
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return "none"
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "none"
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip().splitlines()[0]
    return "none"


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CORE_PACK",
    "CapabilityPack",
    "CapabilityRuntime",
    "ENHANCED_OCR_PACK",
    "HARDWARE_PROFILES",
    "HardwareProfile",
    "MODEL_STORAGE_ENV",
    "ModelArtifact",
    "ModelStore",
    "PACKS",
    "PACKS_BY_NAME",
    "PACK_STATUSES",
    "PACK_TO_EXECUTION_STATUS",
    "PackStatus",
    "PythonArtifact",
    "VISUAL_MODEL_PIN",
    "VISUAL_MODEL_PIN_PATH",
    "VISUAL_PACK",
    "default_model_root",
    "detect_all_packs",
    "detect_hardware",
    "detect_pack",
    "visual_model_pin_bytes",
]
