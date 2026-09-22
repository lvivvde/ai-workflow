"""Region-level OCR results, the state policy that grades them, and the
transcription evidence boundary.

Three things live here and nowhere else:

* the region model -- bounding box, raw text, *separated* confidences,
  language, and the reading-order candidate a region participates in;
* the one mapping from "what the provider did" to execution status, quality
  status, reason code, evidence state, and the coarse V1 status;
* the guards that keep a transcription from being upgraded into source
  evidence and that keep the three confidences from collapsing into a single
  aggregate score.

The evidence rule (docs/ocr.md; spec #11 section 3.4) is that an OCR result
stays a *derived* transcription. It may reach ``machine-supported`` when it
passes the configured quality gate and keeps reviewable regions, and it never
becomes ``explicit`` source evidence by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .ocr_normalization import extract_critical_tokens


#: The three layers that must be reported separately. A single blended number
#: would hide which layer is actually uncertain.
CONFIDENCE_KINDS = ("text_confidence", "region_confidence", "key_mark_confidence")

AGGREGATE_CONFIDENCE_KEYS = frozenset(
    {
        "confidence",
        "score",
        "certainty",
        "overall_confidence",
        "aggregate_confidence",
        "combined_confidence",
        "total_score",
    }
)

#: Provider outcomes this build understands. ``succeeded`` is the only one that
#: can reach ``machine-supported``.
PROVIDER_STATUSES = (
    "succeeded",
    "timeout",
    "failed",
    "corrupt_image",
    "unavailable",
    "models_missing",
    "missing_language",
    "unsupported_format",
)

TRANSCRIPTION_EVIDENCE_STATE = "transcription"
MACHINE_SUPPORTED_EVIDENCE_STATE = "machine-supported"
OCR_EVIDENCE_STATES = (TRANSCRIPTION_EVIDENCE_STATE, MACHINE_SUPPORTED_EVIDENCE_STATE)
#: Only source text itself or a human confirmation may produce these, so an OCR
#: payload that claims them is a bug rather than a preference.
FORBIDDEN_OCR_EVIDENCE_STATES = frozenset({"explicit", "verified"})

#: "Low quality output" is one specific thing: the engine ran and produced text
#: that did not clear the gate. A missing engine, a corrupt image, or an image
#: the engine could not see text in are their own categories, and counting them
#: here would make the counter answer a question nobody asked.
LOW_QUALITY_REASON_CODES = ("below_quality_threshold",)


class AggregateConfidenceError(ValueError):
    """Raised when a payload collapses the three confidences into one score."""


class EvidenceBoundaryError(ValueError):
    """Raised when OCR output claims a source-evidence state it cannot hold."""


def _iter_keys(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _iter_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_keys(item)


def assert_separate_confidence(
    payload: Any, *, where: str = "OCR payload"
) -> None:
    """Reject any payload that reports one aggregate OCR confidence."""

    offending = sorted(AGGREGATE_CONFIDENCE_KEYS.intersection(_iter_keys(payload)))
    if offending:
        raise AggregateConfidenceError(
            f"{where} must report {', '.join(CONFIDENCE_KINDS)} separately; it "
            f"also carries aggregate score field(s): {', '.join(offending)}"
        )


def assert_transcription_boundary(state: str, *, where: str = "OCR payload") -> str:
    """Keep an OCR result inside the transcription boundary."""

    if state in FORBIDDEN_OCR_EVIDENCE_STATES:
        raise EvidenceBoundaryError(
            f"{where} claims evidence state {state!r}; OCR output stays "
            f"{TRANSCRIPTION_EVIDENCE_STATE} or {MACHINE_SUPPORTED_EVIDENCE_STATE} "
            "and is only upgraded by a human confirmation or a source text layer"
        )
    if state not in OCR_EVIDENCE_STATES:
        raise EvidenceBoundaryError(
            f"{where} reports unknown evidence state {state!r}; expected one of "
            f"{', '.join(OCR_EVIDENCE_STATES)}"
        )
    return state


@dataclass(frozen=True)
class BoundingBox:
    """An axis-aligned box in the source image's own pixel coordinates."""

    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def union(self, other: "BoundingBox") -> "BoundingBox":
        left = min(self.x, other.x)
        top = min(self.y, other.y)
        right = max(self.x + self.width, other.x + other.width)
        bottom = max(self.y + self.height, other.y + other.height)
        return BoundingBox(left, top, max(0.0, right - left), max(0.0, bottom - top))

    def as_payload(self) -> dict[str, float]:
        return {
            "x": float(self.x),
            "y": float(self.y),
            "width": float(self.width),
            "height": float(self.height),
        }

    @classmethod
    def from_polygon(cls, points: Sequence[Sequence[float]]) -> "BoundingBox | None":
        """Box around a provider's polygon; ``None`` when it has no geometry."""

        xs: list[float] = []
        ys: list[float] = []
        for point in points:
            if len(point) < 2:
                continue
            xs.append(float(point[0]))
            ys.append(float(point[1]))
        if not xs or not ys:
            return None
        return cls(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BoundingBox | None":
        try:
            return cls(
                float(payload["x"]),
                float(payload["y"]),
                float(payload["width"]),
                float(payload["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class OcrRegion:
    """One located piece of raw transcription.

    ``text`` is the *raw* transcription: nothing in this project normalizes it
    in place. Suggestions live beside it (``ocr_normalization``) so the raw
    value survives every later correction.
    """

    index: int
    text: str
    bbox: BoundingBox | None = None
    reading_order: int = 0
    language: str = ""
    text_confidence: float | None = None
    region_confidence: float | None = None
    key_mark_confidence: float | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "reading_order": self.reading_order,
            "text": self.text,
            "text_confidence": _round(self.text_confidence),
            "region_confidence": _round(self.region_confidence),
            "key_mark_confidence": _round(self.key_mark_confidence),
            "language": self.language,
            "bbox": self.bbox.as_payload() if self.bbox is not None else None,
        }
        assert_separate_confidence(payload, where=f"OCR region {self.index}")
        return payload


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError(f"confidence must be a finite number, got {value!r}")
    return round(float(value), 6)


def key_mark_confidence(regions: Sequence[OcrRegion]) -> float | None:
    """Confidence of the regions that carry a critical transcription token.

    This is deliberately *not* an average over the image: a single shaky region
    holding ``-3.5`` or ``→`` is exactly the case the separate key-mark number
    exists to expose. ``None`` means "no critical token was transcribed", which
    is different from "the critical tokens were confident".
    """

    confidences = [
        confidence
        for confidence in (region_key_mark_confidence(region) for region in regions)
        if confidence is not None
    ]
    if not confidences:
        return None
    return min(confidences)


def region_key_mark_confidence(region: OcrRegion) -> float | None:
    """One region's key-mark layer, from the provider if it scored one.

    An engine that reports its own key-mark score keeps it; otherwise the text
    confidence of a region that actually carries a critical token stands in for
    it, because that is the number whose shakiness matters. A region holding no
    critical token contributes nothing, so it cannot lift the layer either.
    """

    if region.key_mark_confidence is not None:
        return region.key_mark_confidence
    if region.text_confidence is None:
        return None
    if not extract_critical_tokens(region.text):
        return None
    return region.text_confidence


@dataclass(frozen=True)
class QualityGate:
    """The configured threshold a transcription has to clear."""

    min_text_confidence: float = 0.5
    min_key_mark_confidence: float = 0.5
    min_characters: int = 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "min_text_confidence": self.min_text_confidence,
            "min_key_mark_confidence": self.min_key_mark_confidence,
            "min_characters": self.min_characters,
        }


DEFAULT_QUALITY_GATE = QualityGate()


def gate_failures(
    regions: Sequence[OcrRegion], gate: QualityGate = DEFAULT_QUALITY_GATE
) -> tuple[str, ...]:
    """Every reason the transcription does not clear the gate.

    A confidence the engine cannot report is not a failure: Tesseract, for
    instance, returns text without scores on some builds, and inventing a
    number for it would be worse than leaving the layer empty.
    """

    retained = [region for region in regions if region.has_text]
    failures: list[str] = []
    if not retained:
        failures.append("no_text_detected")
    characters = sum(len(region.text.strip()) for region in retained)
    if retained and characters < gate.min_characters:
        failures.append("below_minimum_length")
    scored = [
        region.text_confidence
        for region in retained
        if region.text_confidence is not None
    ]
    if scored and max(scored) < gate.min_text_confidence:
        failures.append("text_confidence_below_threshold")
    marks = key_mark_confidence(retained)
    if marks is not None and marks < gate.min_key_mark_confidence:
        failures.append("key_mark_confidence_below_threshold")
    return tuple(failures)


@dataclass(frozen=True)
class OcrOutcome:
    """The graded result of one image transcription."""

    provider_status: str
    execution_status: str
    quality_status: str
    reason_code: str
    detail: str
    evidence_state: str
    v1_status: str
    retryable: bool
    corrective_action: str = ""
    gate_failures: tuple[str, ...] = ()
    #: True when the engine that answered is not the one that was requested.
    fallback_used: bool = False

    def __post_init__(self) -> None:
        assert_transcription_boundary(self.evidence_state)

    @property
    def machine_supported(self) -> bool:
        return self.evidence_state == MACHINE_SUPPORTED_EVIDENCE_STATE

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "provider_status": self.provider_status,
            "execution_status": self.execution_status,
            "quality_status": self.quality_status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "evidence_state": self.evidence_state,
            "v1_status": self.v1_status,
            "retryable": self.retryable,
            "corrective_action": self.corrective_action,
            "gate_failures": list(self.gate_failures),
            "fallback_used": self.fallback_used,
        }
        assert_separate_confidence(payload, where="OCR outcome")
        return payload


_CORRECTIVE_ACTIONS = {
    "ocr_timeout": "raise the OCR timeout or lower the image resolution",
    "corrupt_image": "re-export the image from the source document",
    "unsupported_image_format": "convert the image to PNG or JPEG",
    "missing_language_pack": "install the OCR language data for this document",
    "no_usable_engine": "install the core OCR capability pack",
    "models_not_installed": (
        "install the core OCR capability pack from a local directory; models are "
        "never downloaded during a build"
    ),
    "engine_error": "re-run the stage; if it repeats, capture the engine log",
    "no_text_detected": "confirm the image really contains text",
    "below_quality_threshold": "re-crop or enlarge the region and re-run OCR",
    "below_minimum_length": "re-crop or enlarge the region and re-run OCR",
}


def _v1_status(execution_status: str) -> str:
    """The coarse status V1 clients already read.

    ``partial`` is a V2 distinction, so it projects onto V1's ``succeeded``:
    the V1 field only says whether usable text came out at all. The precise
    state is kept in the OCR run record.
    """

    if execution_status in {"succeeded", "partial"}:
        return "succeeded"
    return execution_status


def _outcome(
    provider_status: str,
    execution_status: str,
    quality_status: str,
    reason_code: str,
    detail: str,
    *,
    evidence_state: str = TRANSCRIPTION_EVIDENCE_STATE,
    retryable: bool = False,
    failures: tuple[str, ...] = (),
    fallback_used: bool = False,
) -> OcrOutcome:
    return OcrOutcome(
        provider_status=provider_status,
        execution_status=execution_status,
        quality_status=quality_status,
        reason_code=reason_code,
        detail=detail,
        evidence_state=evidence_state,
        v1_status=_v1_status(execution_status),
        retryable=retryable,
        corrective_action=_CORRECTIVE_ACTIONS.get(reason_code, ""),
        gate_failures=failures,
        fallback_used=fallback_used,
    )


def classify_outcome(
    provider_status: str,
    regions: Sequence[OcrRegion] = (),
    *,
    gate: QualityGate = DEFAULT_QUALITY_GATE,
    detail: str = "",
    fallback_used: bool = False,
) -> OcrOutcome:
    """Grade one provider outcome. The only place this table is written.

    ================  ================  ================  ===================
    provider status   execution         quality           evidence state
    ================  ================  ================  ===================
    succeeded         succeeded         accepted          machine-supported
    succeeded         succeeded         rejected          transcription
    timeout           partial / failed  uncertain/rej.    transcription
    failed            partial / failed  uncertain/rej.    transcription
    corrupt image     failed            rejected          transcription
    missing language  unavailable       rejected          transcription
    models missing    unavailable       rejected          transcription
    unsupported fmt   failed            rejected          transcription
    unavailable       unavailable       rejected          transcription
    ================  ================  ================  ===================

    Timeouts and engine failures keep the regions they produced (``partial``);
    a run that produced nothing is ``failed``. Partial output is never graded
    ``accepted``, because "some of a page" is not the page.
    """

    if provider_status not in PROVIDER_STATUSES:
        raise ValueError(
            f"unknown OCR provider status: {provider_status!r}; expected one of "
            f"{', '.join(PROVIDER_STATUSES)}"
        )
    retained = tuple(region for region in regions if region.has_text)
    failures = gate_failures(retained, gate)

    if provider_status == "succeeded":
        if "no_text_detected" in failures:
            return _outcome(
                provider_status,
                "succeeded",
                "rejected",
                "no_text_detected",
                detail
                or "the engine ran but reported no text in this image",
                failures=failures,
                fallback_used=fallback_used,
            )
        if failures:
            return _outcome(
                provider_status,
                "succeeded",
                "rejected",
                "below_quality_threshold",
                detail
                or ("transcription did not clear the quality gate: " + ", ".join(failures)),
                failures=failures,
                fallback_used=fallback_used,
            )
        return _outcome(
            provider_status,
            "succeeded",
            "accepted",
            "",
            detail or f"{len(retained)} region(s) cleared the quality gate",
            evidence_state=MACHINE_SUPPORTED_EVIDENCE_STATE,
            failures=failures,
            fallback_used=fallback_used,
        )

    if provider_status == "timeout":
        retained_any = bool(retained)
        return _outcome(
            provider_status,
            "partial" if retained_any else "failed",
            "uncertain" if retained_any else "rejected",
            "ocr_timeout",
            detail or "the engine exceeded the configured OCR timeout",
            retryable=True,
            failures=failures,
            fallback_used=fallback_used,
        )

    if provider_status in {"failed", "corrupt_image"}:
        retained_any = bool(retained)
        reason_code = (
            "corrupt_image"
            if not retained_any and provider_status == "corrupt_image"
            else "engine_error"
        )
        return _outcome(
            provider_status,
            "partial" if retained_any else "failed",
            "uncertain" if retained_any else "rejected",
            reason_code,
            detail or "the OCR engine did not complete",
            retryable=reason_code == "engine_error",
            failures=failures,
            fallback_used=fallback_used,
        )

    if provider_status == "missing_language":
        return _outcome(
            provider_status,
            "unavailable",
            "rejected",
            "missing_language_pack",
            detail or "the OCR language data for this document is not installed",
            fallback_used=fallback_used,
        )

    if provider_status == "models_missing":
        return _outcome(
            provider_status,
            "unavailable",
            "rejected",
            "models_not_installed",
            detail
            or (
                "the engine is installed but its local model files are missing; "
                "this build never downloads them"
            ),
            fallback_used=fallback_used,
        )

    if provider_status == "unsupported_format":
        return _outcome(
            provider_status,
            "failed",
            "rejected",
            "unsupported_image_format",
            detail or "this image format is not supported",
            fallback_used=fallback_used,
        )

    return _outcome(
        provider_status,
        "unavailable",
        "rejected",
        "no_usable_engine",
        detail or "no usable OCR engine was selected for this image",
        fallback_used=fallback_used,
    )


@dataclass(frozen=True)
class RegionObservation:
    """What one engine reported for one image, before any grading."""

    engine: str
    engine_version: str = ""
    tier: str = ""
    provider_status: str = "succeeded"
    regions: tuple[OcrRegion, ...] = ()
    language: str = ""
    reading_order_source: str = "provider"
    detail: str = ""
    duration_ms: int = 0
    requested_engine: str = ""
    fallback_used: bool = False
    reason_chain: tuple[Mapping[str, Any], ...] = ()
    #: The V1 flat transcription when the engine produced one; otherwise the
    #: reading-order join of ``regions`` is used.
    flat_text: str = ""

    def ordered(self) -> tuple[OcrRegion, ...]:
        return tuple(sorted(self.regions, key=lambda region: region.reading_order))

    def text(self) -> str:
        if self.flat_text:
            return self.flat_text
        return "\n".join(region.text for region in self.ordered() if region.has_text)

    def outcome(self, gate: QualityGate = DEFAULT_QUALITY_GATE) -> OcrOutcome:
        return classify_outcome(
            self.provider_status,
            self.regions,
            gate=gate,
            detail=self.detail,
            fallback_used=self.fallback_used,
        )

    def as_payload(self) -> dict[str, Any]:
        outcome = self.outcome()
        payload: dict[str, Any] = {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "tier": self.tier,
            "provider_status": self.provider_status,
            "language": self.language,
            "reading_order_source": self.reading_order_source,
            "region_count": len(self.regions),
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "requested_engine": self.requested_engine or self.engine,
            "fallback_used": self.fallback_used,
            "reason_chain": [dict(entry) for entry in self.reason_chain],
            "regions": [region.as_payload() for region in self.ordered()],
            "key_mark_confidence": _round(key_mark_confidence(self.regions)),
            "outcome": outcome.as_payload(),
        }
        assert_separate_confidence(payload, where=f"OCR observation for {self.engine}")
        return payload


def summarize_outcomes(outcomes: Iterable[OcrOutcome]) -> dict[str, int]:
    """Image counters for the build report; the three V1 numbers keep meaning.

    The per-engine and per-status breakdowns are derived from the OCR tables at
    read time (``index_status``), so this stays a flat set of counters.

    ``low_quality_images`` counts only *low quality output*: text came out and
    the gate rejected it. Images that never reached the gate are already
    accounted for by ``v1_unavailable`` / ``v1_failed``.
    """

    summary: dict[str, int] = {
        "images": 0,
        "v1_succeeded": 0,
        "v1_failed": 0,
        "v1_unavailable": 0,
        "fallback_images": 0,
        "low_quality_images": 0,
        "machine_supported_images": 0,
        "partial_images": 0,
    }
    for outcome in outcomes:
        summary["images"] += 1
        key = f"v1_{outcome.v1_status}"
        summary[key] = summary.get(key, 0) + 1
        summary["partial_images"] += outcome.execution_status == "partial"
        summary["fallback_images"] += outcome.fallback_used
        summary["low_quality_images"] += outcome.reason_code in LOW_QUALITY_REASON_CODES
        summary["machine_supported_images"] += outcome.machine_supported
    return summary


__all__ = [
    "AGGREGATE_CONFIDENCE_KEYS",
    "AggregateConfidenceError",
    "BoundingBox",
    "CONFIDENCE_KINDS",
    "DEFAULT_QUALITY_GATE",
    "EvidenceBoundaryError",
    "FORBIDDEN_OCR_EVIDENCE_STATES",
    "LOW_QUALITY_REASON_CODES",
    "MACHINE_SUPPORTED_EVIDENCE_STATE",
    "OCR_EVIDENCE_STATES",
    "OcrOutcome",
    "OcrRegion",
    "PROVIDER_STATUSES",
    "QualityGate",
    "RegionObservation",
    "TRANSCRIPTION_EVIDENCE_STATE",
    "assert_separate_confidence",
    "assert_transcription_boundary",
    "classify_outcome",
    "gate_failures",
    "key_mark_confidence",
    "region_key_mark_confidence",
    "summarize_outcomes",
]
