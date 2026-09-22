"""Local OCR engines and the explicit degradation chain that selects one.

The core tier is RapidOCR on ONNX Runtime; Tesseract stays a *compatibility*
fallback rather than an equivalent default. This module owns engine identity,
availability, and the fallback record, so a stage attempt can always say which
engine was asked for, which one ran, and why anything was skipped.

V2-04 delivers the real RapidOCR handler (regions, confidence, normalization).
Until then the engine is declared but not usable, which is exactly the case the
degradation chain has to report honestly instead of silently pretending the
compatibility fallback is the same capability.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


ENGINE_TIERS = ("core", "compatibility")
Transcription = tuple[str, str, str | None]
_VERSION_CACHE: dict[str, str] = {}


@dataclass(frozen=True)
class OcrEngine:
    """One OCR engine as this build understands it."""

    name: str
    tier: str
    pack: str
    ruleset_version: str
    module: str = ""
    executable: str = ""
    implemented: bool = True
    owner_ticket: str = ""

    def installed(self) -> bool:
        if self.module:
            return importlib.util.find_spec(self.module) is not None
        if self.executable:
            return shutil.which(self.executable) is not None
        return False

    def version(self) -> str:
        cached = _VERSION_CACHE.get(self.name)
        if cached is not None:
            return cached
        _VERSION_CACHE[self.name] = self._resolve_version()
        return _VERSION_CACHE[self.name]

    def _resolve_version(self) -> str:
        if self.module:
            return _module_version(self.module)
        executable = shutil.which(self.executable) if self.executable else None
        if executable is None:
            return ""
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""

    def usable(self) -> tuple[bool, str]:
        """Whether this engine can actually produce a transcription right now."""

        if not self.implemented:
            return False, (
                f"the {self.name} handler is declared by this build but not "
                f"implemented yet ({self.owner_ticket or 'later ticket'})"
            )
        if not self.installed():
            target = self.module or self.executable
            return False, f"{target} is not installed"
        return True, "ready"

    def transcribe(self, asset_path: Path) -> Transcription:
        if self.name != "tesseract":
            return (
                "unavailable",
                "",
                f"the {self.name} transcription handler is not implemented in this "
                f"build ({self.owner_ticket or 'later ticket'})",
            )
        return _tesseract_transcribe(Path(asset_path))


TESSERACT_ENGINE = OcrEngine(
    name="tesseract",
    tier="compatibility",
    pack="core",
    ruleset_version="tesseract-cli-v1",
    executable="tesseract",
    owner_ticket="V2-04",
)
RAPIDOCR_ENGINE = OcrEngine(
    name="rapidocr",
    tier="core",
    pack="core",
    ruleset_version="rapidocr-onnx-v1",
    module="rapidocr_onnxruntime",
    implemented=False,
    owner_ticket="V2-04",
)
PADDLEOCR_ENGINE = OcrEngine(
    name="paddleocr",
    tier="enhanced",
    pack="enhanced_ocr",
    ruleset_version="paddleocr-v1",
    module="paddleocr",
    implemented=False,
    owner_ticket="V2-04",
)

# Requested tier first, then progressively lower capability. Documented as the
# degradation chain in docs/processing.md.
DEFAULT_CHAIN: tuple[OcrEngine, ...] = (RAPIDOCR_ENGINE, PADDLEOCR_ENGINE, TESSERACT_ENGINE)


@dataclass(frozen=True)
class OcrSelection:
    """Which engine the pipeline selected, and the chain it walked to get there."""

    engine: OcrEngine | None
    chain: tuple[dict[str, Any], ...]
    execution_status: str
    reason_code: str
    reason: str

    @property
    def fallback_used(self) -> bool:
        return self.engine is not None and self.chain[0]["engine"] != self.engine.name

    def as_payload(self) -> dict[str, Any]:
        return {
            "selected": self.engine.name if self.engine else None,
            "tier": self.engine.tier if self.engine else None,
            "execution_status": self.execution_status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "fallback_used": self.fallback_used,
            "chain": [dict(entry) for entry in self.chain],
        }


def select_engine(
    chain: tuple[OcrEngine, ...] = DEFAULT_CHAIN,
    *,
    prefer: str | None = None,
    allow_compatibility_fallback: bool = True,
    runtime: Any = None,
) -> OcrSelection:
    """Walk the degradation chain and report exactly what was skipped.

    ``allow_compatibility_fallback`` is the explicit opt-out that V2-04/V2-11
    need when a machine must not quietly use a lower-capability engine.
    """

    engines = chain if prefer is None else tuple(
        sorted(chain, key=lambda engine: engine.name != prefer)
    )
    entries: list[dict[str, Any]] = []
    for engine in engines:
        usable, detail = engine.usable()
        entry = {
            "engine": engine.name,
            "tier": engine.tier,
            "pack": engine.pack,
            "available": engine.installed(),
            "usable": usable,
            "version": engine.version() if engine.installed() else "",
            "detail": detail,
        }
        entries.append(entry)
        if not usable:
            continue
        if engine.tier == "compatibility" and not allow_compatibility_fallback:
            entry["detail"] = (
                "compatibility fallback is disabled by configuration; this engine "
                "was not selected"
            )
            entry["usable"] = False
            continue
        if runtime is not None:
            runtime.acquire(engine.pack)
        return OcrSelection(
            engine=engine,
            chain=tuple(entries),
            execution_status="succeeded",
            reason_code="",
            reason=f"{engine.name} is ready",
        )

    return OcrSelection(
        engine=None,
        chain=tuple(entries),
        execution_status="unavailable",
        reason_code="no_usable_engine",
        reason=(
            "no usable OCR engine: "
            + "; ".join(f"{entry['engine']}: {entry['detail']}" for entry in entries)
        ),
    )


def _tesseract_transcribe(asset_path: Path) -> Transcription:
    executable = shutil.which(TESSERACT_ENGINE.executable)
    if executable is None:
        return "unavailable", "", "Tesseract executable was not found on PATH"

    language = os.environ.get("GAME_DESIGN_OCR_LANG", "chi_sim+eng")
    try:
        completed = subprocess.run(
            [executable, str(asset_path), "stdout", "-l", language],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return "failed", "", str(error)

    if completed.returncode != 0:
        error = completed.stderr.strip() or f"Tesseract exited with {completed.returncode}"
        return "failed", "", error
    return "succeeded", completed.stdout.strip(), None


def _module_version(module: str) -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.10+
        return ""
    for candidate in {module, module.replace("_", "-")}:
        try:
            return version(candidate)
        except PackageNotFoundError:
            continue
    return ""


__all__ = [
    "DEFAULT_CHAIN",
    "ENGINE_TIERS",
    "OcrEngine",
    "OcrSelection",
    "PADDLEOCR_ENGINE",
    "RAPIDOCR_ENGINE",
    "TESSERACT_ENGINE",
    "Transcription",
    "select_engine",
]
