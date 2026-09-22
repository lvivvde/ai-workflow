"""Corpus, annotation, and evaluation-run schemas.

These dataclasses are the executable form of the accuracy protocol: they fix
which strata, annotation fields, and run-manifest fields an evaluation run has
to carry so two runs can be compared without re-reading prose specs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


EVALUATION_LAYERS: tuple[str, ...] = (
    "source_import",
    "ocr_transcription",
    "layout_regions",
    "reading_order_relations",
    "notation_resolution",
    "statement_fidelity",
    "retrieval",
    "conflict_and_response_state",
    "explanation",
    "performance_and_degradation",
)

STRATUM_FIELDS: tuple[str, ...] = (
    "source_type",
    "content_type",
    "language",
    "visual_quality",
    "structure_complexity",
    "notation_complexity",
    "conflict_state",
    "capability_pack",
    "difficulty",
)

STRATUM_VALUES: dict[str, frozenset[str]] = {
    "source_type": frozenset(
        {"docx", "xlsx", "embedded_image", "standalone_image"}
    ),
    "content_type": frozenset(
        {
            "rule_text",
            "config_value",
            "image_text",
            "flow_notation",
            "conflict",
            "unknown",
        }
    ),
    "language": frozenset({"zh", "en", "mixed"}),
    "visual_quality": frozenset({"clean", "degraded", "n/a"}),
    "structure_complexity": frozenset({"simple", "nested"}),
    "notation_complexity": frozenset({"none", "arrow", "unknown_symbol"}),
    "conflict_state": frozenset({"none", "present", "expected_absent"}),
    "capability_pack": frozenset({"core", "enhanced_ocr", "visual"}),
    "difficulty": frozenset({"basic", "hard", "boundary"}),
}

RESPONSE_STATES: frozenset[str] = frozenset(
    {"found", "not_found", "partial", "ambiguous", "degraded", "failed", "stale"}
)

CORPUS_SPLITS: frozenset[str] = frozenset(
    {"development", "golden", "v1_compatibility"}
)

EVALUATION_MODES: frozenset[str] = frozenset({"component", "e2e"})


class SchemaError(ValueError):
    """Raised when a corpus, sample, or run manifest violates the protocol."""


def canonical_fingerprint(payload: object) -> str:
    """Stable content fingerprint; the harness compares these, not file bytes."""

    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Stratum:
    values: Mapping[str, str]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], where: str) -> Stratum:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: stratum must be an object")
        missing = [key for key in STRATUM_FIELDS if key not in payload]
        if missing:
            raise SchemaError(f"{where}: stratum is missing {missing}")
        unknown = [key for key in payload if key not in STRATUM_FIELDS]
        if unknown:
            raise SchemaError(f"{where}: stratum has unknown keys {unknown}")
        for key, value in payload.items():
            allowed = STRATUM_VALUES[key]
            if value not in allowed:
                raise SchemaError(
                    f"{where}: stratum.{key} must be one of {sorted(allowed)}, "
                    f"got {value!r}"
                )
        return cls(values=dict(payload))

    def label(self, *keys: str) -> str:
        selected = keys or STRATUM_FIELDS
        return "|".join(f"{key}={self.values[key]}" for key in selected)

    def as_payload(self) -> dict[str, str]:
        return dict(self.values)


@dataclass(frozen=True)
class EvidenceExpectation:
    """One annotated reference the response has to be able to point back to."""

    source_document: str
    locator_contains: Mapping[str, Any] = field(default_factory=dict)
    text: str | None = None

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], where: str
    ) -> EvidenceExpectation:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: required_evidence entries must be objects")
        source_document = payload.get("source_document")
        if not isinstance(source_document, str) or not source_document.strip():
            raise SchemaError(f"{where}: required_evidence needs source_document")
        locator = payload.get("locator_contains") or {}
        if not isinstance(locator, Mapping):
            raise SchemaError(f"{where}: locator_contains must be an object")
        text = payload.get("text")
        if text is not None and not isinstance(text, str):
            raise SchemaError(f"{where}: required_evidence text must be a string")
        return cls(
            source_document=source_document,
            locator_contains=dict(locator),
            text=text,
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_document": self.source_document,
            "locator_contains": dict(self.locator_contains),
            "text": self.text,
        }


@dataclass(frozen=True)
class SampleAnnotation:
    annotators: tuple[str, ...]
    adjudicated: bool
    guide_version: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], where: str) -> SampleAnnotation:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: annotation must be an object")
        annotators = payload.get("annotators")
        if (
            not isinstance(annotators, list)
            or not annotators
            or not all(isinstance(item, str) and item.strip() for item in annotators)
        ):
            raise SchemaError(f"{where}: annotation.annotators must be a non-empty list")
        guide_version = payload.get("guide_version")
        if not isinstance(guide_version, str) or not guide_version.strip():
            raise SchemaError(f"{where}: annotation.guide_version is required")
        return cls(
            annotators=tuple(annotators),
            adjudicated=bool(payload.get("adjudicated", False)),
            guide_version=guide_version,
        )

    @property
    def needs_adjudication(self) -> bool:
        """High-risk samples follow double annotation plus third-party review."""

        return len(self.annotators) < 2 or not self.adjudicated

    def as_payload(self) -> dict[str, Any]:
        return {
            "annotators": list(self.annotators),
            "adjudicated": self.adjudicated,
            "guide_version": self.guide_version,
        }


@dataclass(frozen=True)
class ExpectedOutcome:
    response_state: str
    allowed_answer_set: tuple[str, ...] = ()
    required_evidence: tuple[EvidenceExpectation, ...] = ()
    required_gaps: tuple[str, ...] = ()
    conflict_group_expected: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], where: str) -> ExpectedOutcome:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: expected must be an object")
        response_state = payload.get("response_state")
        if response_state not in RESPONSE_STATES:
            raise SchemaError(
                f"{where}: expected.response_state must be one of "
                f"{sorted(RESPONSE_STATES)}, got {response_state!r}"
            )
        allowed = payload.get("allowed_answer_set") or []
        if not isinstance(allowed, list) or not all(
            isinstance(item, str) for item in allowed
        ):
            raise SchemaError(f"{where}: allowed_answer_set must be a string list")
        evidence = payload.get("required_evidence") or []
        gaps = payload.get("required_gaps") or []
        if not isinstance(gaps, list) or not all(isinstance(item, str) for item in gaps):
            raise SchemaError(f"{where}: required_gaps must be a string list")
        return cls(
            response_state=str(response_state),
            allowed_answer_set=tuple(allowed),
            required_evidence=tuple(
                EvidenceExpectation.from_payload(item, where) for item in evidence
            ),
            required_gaps=tuple(gaps),
            conflict_group_expected=bool(payload.get("conflict_group_expected", False)),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "response_state": self.response_state,
            "allowed_answer_set": list(self.allowed_answer_set),
            "required_evidence": [
                item.as_payload() for item in self.required_evidence
            ],
            "required_gaps": list(self.required_gaps),
            "conflict_group_expected": self.conflict_group_expected,
        }


@dataclass(frozen=True)
class DocumentSpec:
    """A synthetic or desensitized document, stored as a generator spec.

    Samples ship generator specs instead of binary OOXML so the public repo
    never carries real design material, and so the corpus stays diffable.
    """

    path: str
    generator: Mapping[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        where: str,
        registry: Mapping[str, Any] | None = None,
    ) -> DocumentSpec:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: documents entries must be objects")
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise SchemaError(f"{where}: document path is required")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise SchemaError(f"{where}: document path must stay inside the corpus")
        generator = payload.get("generator")
        if generator is None and registry is not None:
            generator = registry.get(path)
        if not isinstance(generator, Mapping):
            raise SchemaError(
                f"{where}: document generator must be an object or defined in "
                f"the corpus manifest: {path}"
            )
        kind = generator.get("kind")
        if kind not in {"docx", "xlsx", "catalog"}:
            raise SchemaError(
                f"{where}: generator.kind must be docx, xlsx, or catalog"
            )
        return cls(path=path, generator=dict(generator))

    def as_payload(self) -> dict[str, Any]:
        return {"path": self.path, "generator": dict(self.generator)}


@dataclass(frozen=True)
class Sample:
    sample_id: str
    stratum: Stratum
    documents: tuple[DocumentSpec, ...]
    tool: str
    arguments: Mapping[str, Any]
    expected: ExpectedOutcome
    annotation: SampleAnnotation
    modes: tuple[str, ...] = ("component", "e2e")
    capabilities: tuple[str, ...] = ("core",)
    notes: str = ""

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        where: str,
        registry: Mapping[str, Any] | None = None,
    ) -> Sample:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: sample must be an object")
        sample_id = payload.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip():
            raise SchemaError(f"{where}: sample_id is required")
        where = f"{where}[{sample_id}]"

        documents = payload.get("documents")
        if not isinstance(documents, list) or not documents:
            raise SchemaError(f"{where}: documents must be a non-empty list")
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise SchemaError(f"{where}: tool is required")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise SchemaError(f"{where}: arguments must be an object")
        modes = payload.get("modes") or ["component", "e2e"]
        if not isinstance(modes, list) or not modes:
            raise SchemaError(f"{where}: modes must be a non-empty list")
        invalid_modes = [item for item in modes if item not in EVALUATION_MODES]
        if invalid_modes:
            raise SchemaError(f"{where}: unknown modes {invalid_modes}")
        capabilities = payload.get("capabilities") or ["core"]
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise SchemaError(f"{where}: capabilities must be a string list")

        return cls(
            sample_id=sample_id,
            stratum=Stratum.from_payload(payload.get("stratum"), where),
            documents=tuple(
                DocumentSpec.from_payload(item, where, registry) for item in documents
            ),
            tool=tool,
            arguments=dict(arguments),
            expected=ExpectedOutcome.from_payload(payload.get("expected"), where),
            annotation=SampleAnnotation.from_payload(payload.get("annotation"), where),
            modes=tuple(modes),
            capabilities=tuple(capabilities),
            notes=str(payload.get("notes") or ""),
        )

    @property
    def high_risk(self) -> bool:
        """Facts, relations, conflicts, no-answer, and explanation need review."""

        return (
            self.stratum.values["conflict_state"] != "none"
            or self.expected.conflict_group_expected
            or self.expected.response_state != "found"
            or self.stratum.values["notation_complexity"] != "none"
            or self.stratum.values["content_type"]
            in {"flow_notation", "image_text", "conflict", "unknown"}
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "stratum": self.stratum.as_payload(),
            "documents": [item.as_payload() for item in self.documents],
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "expected": self.expected.as_payload(),
            "annotation": self.annotation.as_payload(),
            "modes": list(self.modes),
            "capabilities": list(self.capabilities),
            "notes": self.notes,
        }

    def fingerprint(self) -> str:
        return canonical_fingerprint(self.as_payload())


@dataclass(frozen=True)
class CorpusManifest:
    name: str
    version: str
    split: str
    description: str
    frozen: bool
    sample_fingerprints: Mapping[str, str]
    guide_version: str
    documents: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], where: str
    ) -> CorpusManifest:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: manifest must be an object")
        name = payload.get("name")
        version = payload.get("version")
        split = payload.get("split")
        if not isinstance(name, str) or not name.strip():
            raise SchemaError(f"{where}: manifest.name is required")
        if not isinstance(version, str) or not version.strip():
            raise SchemaError(f"{where}: manifest.version is required")
        if split not in CORPUS_SPLITS:
            raise SchemaError(
                f"{where}: manifest.split must be one of {sorted(CORPUS_SPLITS)}"
            )
        fingerprints = payload.get("sample_fingerprints")
        if not isinstance(fingerprints, Mapping) or not fingerprints:
            raise SchemaError(f"{where}: manifest.sample_fingerprints is required")
        for sample_id, value in fingerprints.items():
            if not isinstance(value, str) or len(value) != 64:
                raise SchemaError(
                    f"{where}: fingerprint for {sample_id!r} must be a sha256 hex"
                )
        guide_version = payload.get("guide_version")
        if not isinstance(guide_version, str) or not guide_version.strip():
            raise SchemaError(f"{where}: manifest.guide_version is required")
        return cls(
            name=name,
            version=version,
            split=str(split),
            description=str(payload.get("description") or ""),
            frozen=bool(payload.get("frozen", split == "golden")),
            sample_fingerprints=dict(fingerprints),
            guide_version=guide_version,
            documents=dict(payload.get("documents") or {}),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "split": self.split,
            "description": self.description,
            "frozen": self.frozen,
            "sample_fingerprints": dict(self.sample_fingerprints),
            "guide_version": self.guide_version,
            "documents": dict(self.documents),
        }


@dataclass
class EnvironmentRecord:
    platform: str
    python_version: str
    cpu_count: int
    memory_bytes: int | None
    hardware_profile: str
    cold_start: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "python_version": self.python_version,
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "hardware_profile": self.hardware_profile,
            "cold_start": self.cold_start,
        }


@dataclass
class EvaluationRunManifest:
    run_id: str
    started_at: str
    finished_at: str
    corpus: Mapping[str, Any]
    environment: Mapping[str, Any]
    schema_versions: Mapping[str, Any]
    capabilities: Mapping[str, Any]
    config: Mapping[str, Any]
    artifacts: Mapping[str, str]

    def as_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "corpus": dict(self.corpus),
            "environment": dict(self.environment),
            "schema_versions": dict(self.schema_versions),
            "capabilities": dict(self.capabilities),
            "config": dict(self.config),
            "artifacts": dict(self.artifacts),
        }
