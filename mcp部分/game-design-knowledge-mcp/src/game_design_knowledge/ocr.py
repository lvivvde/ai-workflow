"""Local OCR engines, the degradation chain that selects one, and the
region-level transcription each of them returns.

The core tier is RapidOCR on ONNX Runtime; PaddleOCR is the explicitly
installed enhancement; Tesseract stays a *compatibility* fallback rather than
an equivalent default. This module owns engine identity, availability, the
degradation record, and the observation an engine produces, so a stage attempt
can always say which engine was asked for, which one answered, which regions
carried which confidence, and why anything was skipped.

Two rules shape the engines here:

* **No silent downloads.** Loading a model only ever reads files that are
  already on disk. When the local model files are missing the engine reports
  ``models_missing`` and stops, instead of letting a library fetch them.
* **Raw text is the product.** An engine returns regions; the flat
  transcription is a join over them in reading order, and no engine edits its
  own output. Normalization suggestions live in ``ocr_normalization``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence

from .ocr_regions import (
    DEFAULT_QUALITY_GATE,
    BoundingBox,
    OcrOutcome,
    OcrRegion,
    PROVIDER_STATUSES,
    QualityGate,
    RegionObservation,
)


ENGINE_TIERS = ("core", "enhanced", "compatibility")
Transcription = tuple[str, str, str | None]
RegionProvider = Callable[[Path], "RegionObservation | Mapping[str, Any]"]

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_LANGUAGE = "chi_sim+eng"
LANGUAGE_ENVIRONMENT_VARIABLE = "GAME_DESIGN_OCR_LANG"
MODEL_DIRECTORY_ENVIRONMENT_VARIABLE = "GAME_DESIGN_OCR_MODEL_DIR"
TIMEOUT_ENVIRONMENT_VARIABLE = "GAME_DESIGN_OCR_TIMEOUT"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".jpe"})
#: Outcomes where asking the next engine down the chain is worth the work. A
#: corrupt image or an unsupported container is the file's problem, not the
#: engine's, so those stop the walk.
_RETRY_WITH_NEXT_ENGINE = frozenset(
    {"unavailable", "failed", "missing_language", "models_missing"}
)

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
    notes: str = ""

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
        """The flat V1 transcription: status, text, error."""

        path = Path(asset_path)
        if self.name == "tesseract":
            # The V1 behaviour, kept verbatim for clients that only read
            # ``images.ocr_text``.
            return _tesseract_transcribe(path)
        observation = self.observe(path)
        if observation.provider_status != "succeeded":
            return (
                observation.outcome().v1_status,
                observation.text(),
                observation.detail or f"{self.name} did not transcribe {path.name}",
            )
        return "succeeded", observation.text(), None

    def observe(
        self,
        asset_path: Path,
        *,
        provider: RegionProvider | None = None,
        language: str | None = None,
        timeout: float | None = None,
        requested_engine: str = "",
        reason_chain: Sequence[Mapping[str, Any]] = (),
        fallback_used: bool = False,
    ) -> RegionObservation:
        """Region-level transcription for one image.

        ``provider`` is the seam tests use to exercise the region, confidence,
        and state handling without installing an OCR engine: it replaces only
        the engine call, never the preflight, the timing, or the grading.
        """

        path = Path(asset_path)
        language = language if language is not None else _language()
        timeout = timeout if timeout is not None else _timeout()
        context: dict[str, Any] = {
            "engine_version": self.version() if self.installed() else "",
            "tier": self.tier,
            "language": language,
            "requested_engine": requested_engine or self.name,
            "reason_chain": tuple(reason_chain),
            "fallback_used": fallback_used,
        }
        if not self.implemented:
            # "This build cannot do this at all" is a more useful answer than
            # "the image is unreadable", so it comes first.
            return RegionObservation(
                engine=self.name,
                provider_status="unavailable",
                detail=(
                    f"the {self.name} transcription handler is not implemented in "
                    f"this build ({self.owner_ticket or 'later ticket'})"
                ),
                **context,
            )
        blocked = image_preflight(path)
        if blocked is not None:
            status, detail = blocked
            return RegionObservation(
                engine=self.name, provider_status=status, detail=detail, **context
            )
        if provider is not None:
            return _coerce_observation(
                _call_with_timeout(provider, path, timeout), self, context
            )
        if self.name == "rapidocr":
            return _rapidocr_observation(path, self, context, timeout)
        if self.name == "paddleocr":
            return _paddleocr_observation(path, self, context, timeout)
        if self.name == "tesseract":
            return _tesseract_observation(path, self, context, timeout)
        return RegionObservation(
            engine=self.name,
            provider_status="unavailable",
            detail=f"the {self.name} transcription handler is not available",
            **context,
        )


TESSERACT_ENGINE = OcrEngine(
    name="tesseract",
    tier="compatibility",
    pack="core",
    ruleset_version="tesseract-cli-v1",
    executable="tesseract",
    owner_ticket="V2-04",
    notes=(
        "Compatibility fallback. Coordinates and per-line confidence come from "
        "the CLI's TSV mode; when that mode is unavailable the engine keeps the "
        "V1 plain-text invocation."
    ),
)
RAPIDOCR_ENGINE = OcrEngine(
    name="rapidocr",
    tier="core",
    pack="core",
    ruleset_version="rapidocr-onnx-v1",
    module="rapidocr_onnxruntime",
    owner_ticket="V2-04",
    notes=(
        "Default core engine. Its ONNX model files must already be on disk; "
        "this build never triggers the library's model download."
    ),
)
PADDLEOCR_ENGINE = OcrEngine(
    name="paddleocr",
    tier="enhanced",
    pack="enhanced_ocr",
    ruleset_version="paddleocr-v1",
    module="paddleocr",
    owner_ticket="V2-11",
    notes=(
        "Enhancement tier. The handler ships here so the degradation chain can "
        "actually use it; offline installation, checksums, and resource "
        "acceptance belong to V2-11."
    ),
)

# Requested tier first, then progressively lower capability. Documented as the
# degradation chain in docs/ocr.md.
DEFAULT_CHAIN: tuple[OcrEngine, ...] = (RAPIDOCR_ENGINE, PADDLEOCR_ENGINE, TESSERACT_ENGINE)


def image_preflight(asset_path: Path) -> tuple[str, str] | None:
    """Reject an unreadable or unsupported asset before any engine runs.

    This is deterministic and engine-independent, so a corrupt image is
    reported as a corrupt image rather than as three engines failing.
    """

    path = Path(asset_path)
    if not path.is_file():
        return ("failed", f"the image asset is missing: {path}")
    if path.stat().st_size == 0:
        return ("corrupt_image", f"the image asset is empty: {path.name}")
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError as error:
        return ("failed", f"the image asset could not be read: {error}")
    if header.startswith(_PNG_SIGNATURE) or header.startswith(_JPEG_SIGNATURE):
        return None
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return (
            "corrupt_image",
            (
                f"{path.name} has an image extension but its file signature does "
                "not match PNG or JPEG"
            ),
        )
    return (
        "unsupported_format",
        f"{path.name} is not a PNG or JPEG image this build can transcribe",
    )


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

    ``allow_compatibility_fallback`` is the explicit opt-out: with it off, a
    machine must not quietly use a lower-capability engine.
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


@dataclass(frozen=True)
class OcrResult:
    """One image's transcription plus the graded outcome and the chain walk."""

    observation: RegionObservation
    outcome: OcrOutcome
    attempts: tuple[dict[str, Any], ...]
    requested_engine: str

    @property
    def text(self) -> str:
        return self.observation.text()

    @property
    def v1_status(self) -> str:
        return self.outcome.v1_status

    @property
    def error(self) -> str | None:
        """The V1 ``ocr_error`` value: only failures and unavailability."""

        if self.v1_status in {"failed", "unavailable"}:
            return self.outcome.detail or self.outcome.reason_code
        return None

    def as_payload(self) -> dict[str, Any]:
        return {
            "requested_engine": self.requested_engine,
            "observation": self.observation.as_payload(),
            "outcome": self.outcome.as_payload(),
            "attempts": [dict(entry) for entry in self.attempts],
        }


def run_image_ocr(
    asset_path: Path,
    *,
    engine: OcrEngine | None = None,
    chain: tuple[OcrEngine, ...] = DEFAULT_CHAIN,
    providers: Mapping[str, RegionProvider] | None = None,
    gate: QualityGate | None = None,
    language: str | None = None,
    timeout: float | None = None,
    allow_compatibility_fallback: bool = True,
) -> OcrResult:
    """Transcribe one image, walking the chain when an engine cannot answer.

    The requested engine is tried first; a runtime failure then falls through to
    the next usable engine, and every step is recorded so the result can say
    "RapidOCR was asked for and failed, Tesseract answered" instead of quietly
    reporting a compatibility result as a core one.
    """

    providers = dict(providers or {})
    gate = gate if gate is not None else DEFAULT_QUALITY_GATE
    candidates = _candidates(engine, chain)
    requested = candidates[0].name if candidates else ""
    language = language if language is not None else _language()
    blocked = image_preflight(Path(asset_path))
    if blocked is not None:
        status, detail = blocked
        observation = RegionObservation(
            engine=requested,
            provider_status=status,
            detail=detail,
            language=language,
            requested_engine=requested,
        )
        return OcrResult(
            observation=observation,
            outcome=observation.outcome(gate),
            attempts=(),
            requested_engine=requested,
        )

    entries: list[dict[str, Any]] = []
    last_observation: RegionObservation | None = None
    for position, candidate in enumerate(candidates):
        provider = providers.get(candidate.name)
        usable, detail = candidate.usable()
        available = candidate.installed()
        if provider is not None:
            usable, available, detail = True, True, "injected test provider"
        if (
            candidate.tier == "compatibility"
            and candidate.name != requested
            and not allow_compatibility_fallback
        ):
            usable = False
            detail = (
                "compatibility fallback is disabled by configuration; this "
                "engine was not selected"
            )
        if not usable:
            entries.append(
                _attempt_entry(candidate, detail, available, provider_status="")
            )
            continue
        observation = candidate.observe(
            Path(asset_path),
            provider=provider,
            language=language,
            timeout=timeout,
            requested_engine=requested,
        )
        last_observation = observation
        entries.append(
            _attempt_entry(
                candidate, observation.detail, available, observation.provider_status
            )
        )
        has_next = position + 1 < len(candidates)
        if observation.provider_status in _RETRY_WITH_NEXT_ENGINE and has_next:
            continue
        final = replace(
            observation,
            reason_chain=tuple(entries),
            fallback_used=candidate.name != requested,
            requested_engine=requested,
        )
        return OcrResult(
            observation=final,
            outcome=final.outcome(gate),
            attempts=tuple(entries),
            requested_engine=requested,
        )

    if last_observation is not None:
        # An engine answered and only lower tiers were unavailable: keep the
        # engine's own reason ("missing language data", "models not installed")
        # instead of flattening it into "no engine at all".
        final = replace(
            last_observation,
            reason_chain=tuple(entries),
            fallback_used=last_observation.engine != requested,
            requested_engine=requested,
        )
        return OcrResult(
            observation=final,
            outcome=final.outcome(gate),
            attempts=tuple(entries),
            requested_engine=requested,
        )

    observation = RegionObservation(
        engine=requested,
        provider_status="unavailable",
        detail=(
            "no usable OCR engine: "
            + "; ".join(f"{entry['engine']}: {entry['detail']}" for entry in entries)
        ),
        language=language,
        requested_engine=requested,
        reason_chain=tuple(entries),
    )
    return OcrResult(
        observation=observation,
        outcome=observation.outcome(gate),
        attempts=tuple(entries),
        requested_engine=requested,
    )


def _candidates(
    engine: OcrEngine | None, chain: tuple[OcrEngine, ...]
) -> tuple[OcrEngine, ...]:
    if engine is None:
        return tuple(chain)
    return (engine, *(candidate for candidate in chain if candidate.name != engine.name))


def _attempt_entry(
    engine: OcrEngine, detail: str, available: bool, provider_status: str
) -> dict[str, Any]:
    return {
        "engine": engine.name,
        "tier": engine.tier,
        "pack": engine.pack,
        "available": available,
        "usable": provider_status != "",
        "version": engine.version() if available else "",
        "detail": detail,
        "provider_status": provider_status,
    }


def _language() -> str:
    return os.environ.get(LANGUAGE_ENVIRONMENT_VARIABLE, DEFAULT_LANGUAGE)


def _timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENVIRONMENT_VARIABLE)
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _call_with_timeout(
    provider: RegionProvider, asset_path: Path, timeout: float | None
) -> RegionObservation | Mapping[str, Any] | None:
    """Run one provider call under the configured budget.

    ``None`` means the call did not finish in time. The worker thread is left to
    unwind on its own; the point of the budget is to stop a bad image from
    holding the whole build.
    """

    if timeout is None or timeout <= 0:
        return provider(asset_path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider, asset_path)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            return None


def _coerce_observation(
    value: RegionObservation | Mapping[str, Any] | None,
    engine: OcrEngine,
    context: Mapping[str, Any],
) -> RegionObservation:
    if value is None:
        return RegionObservation(
            engine=engine.name,
            provider_status="timeout",
            detail=f"{engine.name} exceeded the configured OCR timeout",
            **context,
        )
    if isinstance(value, RegionObservation):
        # Engine identity is the registry's, not the payload's: a provider says
        # what it read, the chain says which engine that was.
        merged = dict(context)
        merged["tier"] = engine.tier
        if value.engine_version:
            merged["engine_version"] = value.engine_version
        if value.language:
            merged["language"] = value.language
        return replace(value, engine=engine.name, **merged)
    if not isinstance(value, Mapping):
        return RegionObservation(
            engine=engine.name,
            provider_status="failed",
            detail=(
                f"an OCR provider returned {type(value).__name__} instead of an "
                "observation"
            ),
            **context,
        )
    try:
        regions = tuple(
            OcrRegion(
                index=int(region.get("index", position)),
                text=str(region.get("text", "")),
                bbox=(
                    BoundingBox.from_payload(region["bbox"])
                    if isinstance(region.get("bbox"), Mapping)
                    else None
                ),
                reading_order=int(region.get("reading_order", position)),
                language=str(region.get("language", "")),
                text_confidence=region.get("text_confidence"),
                region_confidence=region.get("region_confidence"),
                key_mark_confidence=region.get("key_mark_confidence"),
            )
            for position, region in enumerate(value.get("regions", ()))
        )
    except (TypeError, ValueError) as error:
        return RegionObservation(
            engine=engine.name,
            provider_status="failed",
            detail=f"an OCR provider returned unusable regions: {error}",
            **context,
        )
    merged = dict(context)
    reported_version = str(value.get("engine_version", "") or "")
    if reported_version:
        merged["engine_version"] = reported_version
    reported_language = str(value.get("language", "") or "")
    if reported_language:
        merged["language"] = reported_language
    reported_status = str(value.get("provider_status", "succeeded"))
    if reported_status not in PROVIDER_STATUSES:
        # An unrecognised status would otherwise blow up the whole build when
        # the outcome is graded; name the offender instead.
        return RegionObservation(
            engine=engine.name,
            provider_status="failed",
            detail=(
                f"an OCR provider reported the unknown status "
                f"{reported_status!r}; expected one of "
                f"{', '.join(PROVIDER_STATUSES)}"
            ),
            **merged,
        )
    return RegionObservation(
        engine=engine.name,
        tier=engine.tier,
        provider_status=reported_status,
        regions=regions,
        reading_order_source=str(value.get("reading_order_source", "provider")),
        detail=str(value.get("detail", "")),
        duration_ms=int(value.get("duration_ms", 0)),
        flat_text=str(value.get("flat_text", "")),
        **merged,
    )


def _rapidocr_observation(
    asset_path: Path,
    engine: OcrEngine,
    context: Mapping[str, Any],
    timeout: float | None,
) -> RegionObservation:
    model_root, problem = _rapidocr_model_root()
    if problem is not None:
        return RegionObservation(
            engine=engine.name,
            provider_status="models_missing",
            detail=problem,
            **context,
        )

    def provider(path: Path) -> RegionObservation:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

        reader = RapidOCR(
            det_model_path=str(model_root / "ch_PP-OCRv4_det_infer.onnx"),
            cls_model_path=str(model_root / "ch_ppocr_mobile_v2.0_cls_infer.onnx"),
            rec_model_path=str(model_root / "ch_PP-OCRv4_rec_infer.onnx"),
        )
        prediction, _elapsed = reader(str(path))
        return _observation_from_boxes(engine, prediction or (), context)

    try:
        return _coerce_observation(
            _call_with_timeout(provider, asset_path, timeout), engine, context
        )
    except Exception as error:  # noqa: BLE001 - the engine's failure is the report
        return RegionObservation(
            engine=engine.name,
            provider_status="failed",
            detail=f"RapidOCR failed: {type(error).__name__}: {error}",
            **context,
        )


def _rapidocr_model_root() -> tuple[Path, str | None]:
    """Where the ONNX models must already be. Returns ``(root, problem)``."""

    configured = os.environ.get(MODEL_DIRECTORY_ENVIRONMENT_VARIABLE)
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    spec = importlib.util.find_spec("rapidocr_onnxruntime")
    if spec is not None and spec.origin:
        candidates.append(Path(spec.origin).resolve().parent / "models")
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.onnx")):
            return candidate, None
    if not candidates:
        return Path(), (
            "rapidocr_onnxruntime is not installed; install the core OCR "
            "capability pack"
        )
    return Path(), (
        "no local RapidOCR ONNX model files were found in "
        + ", ".join(str(candidate) for candidate in candidates)
        + f"; set {MODEL_DIRECTORY_ENVIRONMENT_VARIABLE} to the directory that "
        "holds them. This build never downloads models."
    )


def _observation_from_boxes(
    engine: OcrEngine,
    boxes: Iterable[Any],
    context: Mapping[str, Any],
) -> RegionObservation:
    """Translate an engine's ``[polygon, text, score]`` triples into regions."""

    regions: list[OcrRegion] = []
    for position, item in enumerate(boxes):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        polygon, text = item[0], item[1]
        score = item[2] if len(item) > 2 else None
        if isinstance(text, (list, tuple)) and text:
            text = text[0]
        regions.append(
            OcrRegion(
                index=position,
                text=str(text),
                bbox=(
                    BoundingBox.from_polygon(polygon)
                    if isinstance(polygon, (list, tuple))
                    else None
                ),
                reading_order=position,
                language=str(context.get("language", "")),
                text_confidence=_as_confidence(score),
            )
        )
    return RegionObservation(
        engine=engine.name,
        regions=tuple(regions),
        reading_order_source="provider",
        provider_status="succeeded",
        **context,
    )


def _paddleocr_observation(
    asset_path: Path,
    engine: OcrEngine,
    context: Mapping[str, Any],
    timeout: float | None,
) -> RegionObservation:
    available, detail = engine.usable()
    if not available:
        return RegionObservation(
            engine=engine.name, provider_status="unavailable", detail=detail, **context
        )

    def provider(path: Path) -> RegionObservation:
        from paddleocr import PaddleOCR  # type: ignore[import-not-found]

        reader = PaddleOCR(use_angle_cls=True, lang=_paddle_language())
        if hasattr(reader, "predict"):
            boxes = _flatten_paddle_predict(reader.predict(str(path)))
        else:  # pragma: no cover - older PaddleOCR releases
            boxes = _flatten_paddle_classic(reader.ocr(str(path), cls=True))
        return _observation_from_boxes(engine, boxes, context)

    try:
        return _coerce_observation(
            _call_with_timeout(provider, asset_path, timeout), engine, context
        )
    except Exception as error:  # noqa: BLE001 - the engine's failure is the report
        return RegionObservation(
            engine=engine.name,
            provider_status="failed",
            detail=f"PaddleOCR failed: {type(error).__name__}: {error}",
            **context,
        )


def _paddle_language() -> str:
    """PaddleOCR names languages without Tesseract's ``+`` combination."""

    language = _language().replace("+", "_")
    return language.split("_")[0] or "ch"


def _flatten_paddle_predict(result: Any) -> list[Any]:
    boxes: list[Any] = []
    for page in result or ():
        if isinstance(page, Mapping):
            payload: Any = page
        else:
            payload = getattr(page, "json", None)
            if callable(payload):
                payload = payload()
        boxes.extend(_paddle_json_items(payload))
    return boxes


def _paddle_json_items(payload: Any) -> list[Any]:
    if not isinstance(payload, Mapping):
        return []
    result = payload.get("res", payload)
    if not isinstance(result, Mapping):
        return []
    texts = result.get("rec_texts") or ()
    scores = result.get("rec_scores") or ()
    polygons = result.get("rec_polys") or result.get("dt_polys") or ()
    return [
        [polygon, text, score]
        for polygon, text, score in zip(polygons, texts, scores)
    ]


def _flatten_paddle_classic(result: Any) -> list[Any]:
    boxes: list[Any] = []
    for page in result or ():
        for line in page or ():
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            polygon, payload = line[0], line[1]
            if isinstance(payload, (list, tuple)) and payload:
                text = payload[0]
                score = payload[1] if len(payload) > 1 else None
            else:  # pragma: no cover - defensive against other shapes
                text, score = str(payload), None
            boxes.append([polygon, text, score])
    return boxes


def _tesseract_observation(
    asset_path: Path,
    engine: OcrEngine,
    context: Mapping[str, Any],
    timeout: float | None,
) -> RegionObservation:
    executable = shutil.which(engine.executable)
    if executable is None:
        return RegionObservation(
            engine=engine.name,
            provider_status="unavailable",
            detail="Tesseract executable was not found on PATH",
            **context,
        )
    language = str(context.get("language") or DEFAULT_LANGUAGE)
    tsv = _tesseract_tsv(executable, asset_path, language, timeout)
    if tsv is not None:
        status, payload = tsv
        if status == "succeeded":
            regions, flat_text = payload
            return RegionObservation(
                engine=engine.name,
                regions=regions,
                flat_text=flat_text,
                reading_order_source="provider",
                provider_status="succeeded",
                **context,
            )
        if status == "timeout":
            return RegionObservation(
                engine=engine.name,
                provider_status="timeout",
                detail=str(payload),
                **context,
            )
        if status == "missing_language":
            return RegionObservation(
                engine=engine.name,
                provider_status="missing_language",
                detail=str(payload),
                **context,
            )
        # Any other TSV problem falls back to the V1 plain-text invocation.
    status, text, error = _tesseract_transcribe(Path(asset_path), language=language)
    if status == "succeeded":
        region = OcrRegion(index=0, text=text, reading_order=0, language=language)
        return RegionObservation(
            engine=engine.name,
            regions=(region,),
            flat_text=text,
            reading_order_source="provider",
            provider_status="succeeded",
            detail=(
                "Tesseract reported no line geometry; the V1 text output was used"
            ),
            **context,
        )
    if status == "unavailable":
        return RegionObservation(
            engine=engine.name,
            provider_status="unavailable",
            detail=error or "Tesseract is not available",
            **context,
        )
    return RegionObservation(
        engine=engine.name,
        provider_status=(
            "missing_language" if _is_missing_language(error) else "failed"
        ),
        detail=error or "Tesseract did not complete",
        **context,
    )


def _tesseract_tsv(
    executable: str, asset_path: Path, language: str, timeout: float | None
) -> tuple[str, Any] | None:
    """Line regions from Tesseract's TSV mode. ``None`` means "not usable here"."""

    if timeout is not None and timeout <= 0:
        timeout = None
    try:
        completed = subprocess.run(
            [executable, str(asset_path), "stdout", "tsv", "-l", language],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ("timeout", "Tesseract exceeded the configured OCR timeout")
    except OSError:
        return None
    if completed.returncode != 0:
        message = (
            completed.stderr.strip() or f"Tesseract exited with {completed.returncode}"
        )
        if _is_missing_language(message):
            return ("missing_language", message)
        return None
    parsed = _parse_tesseract_tsv(completed.stdout)
    if parsed is None:
        return None
    return ("succeeded", parsed)


def _parse_tesseract_tsv(output: str) -> tuple[tuple[OcrRegion, ...], str] | None:
    """Group Tesseract's word rows into line regions with unions of their boxes."""

    lines = output.splitlines()
    if not lines:
        return None
    header = [name.strip() for name in lines[0].split("\t")]
    required = {
        "block_num",
        "par_num",
        "line_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    }
    if not required.issubset(set(header)):
        return None
    columns = {name: position for position, name in enumerate(header)}
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) < len(header):
            continue
        text = fields[columns["text"]].strip()
        if not text:
            continue
        key = (
            fields[columns["block_num"]],
            fields[columns["par_num"]],
            fields[columns["line_num"]],
        )
        confidence = _as_confidence(fields[columns["conf"]])
        box = BoundingBox(
            _as_float(fields[columns["left"]]) or 0.0,
            _as_float(fields[columns["top"]]) or 0.0,
            _as_float(fields[columns["width"]]) or 0.0,
            _as_float(fields[columns["height"]]) or 0.0,
        )
        entry = grouped.setdefault(key, {"texts": [], "confidences": [], "bbox": None})
        entry["texts"].append(text)
        if confidence is not None:
            entry["confidences"].append(confidence)
        entry["bbox"] = box if entry["bbox"] is None else entry["bbox"].union(box)
    if not grouped:
        return None
    regions: list[OcrRegion] = []
    for position, key in enumerate(sorted(grouped, key=_tesseract_line_sort_key)):
        entry = grouped[key]
        confidences = entry["confidences"]
        regions.append(
            OcrRegion(
                index=position,
                text=" ".join(entry["texts"]),
                bbox=entry["bbox"],
                reading_order=position,
                text_confidence=(
                    sum(confidences) / len(confidences) if confidences else None
                ),
            )
        )
    return tuple(regions), "\n".join(region.text for region in regions)


def _tesseract_line_sort_key(key: tuple[str, str, str]) -> tuple[int, int, int]:
    first, second, third = key
    return (_as_int(first), _as_int(second), _as_int(third))


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:  # Tesseract reports -1 for "no confidence available"
        return None
    if number > 1.0:  # Tesseract scores on a 0-100 scale
        number = number / 100.0
    return min(1.0, max(0.0, number))


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_MISSING_LANGUAGE_MARKERS = (
    "failed loading language",
    "error opening data file",
    "could not initialize tesseract",
)


def _is_missing_language(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _MISSING_LANGUAGE_MARKERS)


def _tesseract_transcribe(
    asset_path: Path, *, language: str | None = None
) -> Transcription:
    """The V1 plain-text invocation, kept for compatibility."""

    executable = shutil.which(TESSERACT_ENGINE.executable)
    if executable is None:
        return "unavailable", "", "Tesseract executable was not found on PATH"

    language = language or _language()
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
    "DEFAULT_LANGUAGE",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENGINE_TIERS",
    "IMAGE_SUFFIXES",
    "OcrEngine",
    "OcrResult",
    "OcrSelection",
    "PADDLEOCR_ENGINE",
    "RAPIDOCR_ENGINE",
    "RegionProvider",
    "TESSERACT_ENGINE",
    "Transcription",
    "image_preflight",
    "run_image_ocr",
    "select_engine",
]
