"""Identity and fingerprint rules for the immutable revision chain.

Document -> Source Revision -> Parse Revision -> Published Revision Bundle.

Everything here is a pure function of bytes and configuration, so the same
inputs reproduce the same identifiers on another machine and another day.
Identifiers never embed a drive letter, a user directory, or a timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


DOCUMENT_ID_PREFIX = "doc"
SOURCE_REVISION_PREFIX = "src"
PARSE_REVISION_PREFIX = "par"
BUNDLE_PREFIX = "bnd"


class RevisionError(ValueError):
    """Raised when an identity or fingerprint cannot be derived safely."""


def normalize_relative_path(path: str) -> str:
    """Normalize a project-relative path to one canonical spelling.

    Long-lived identity uses ``/`` separators, NFC Unicode, and a case-exact
    spelling; Windows path comparison is a separate, explicit check so that
    two documents differing only by case are reported instead of silently
    merged.
    """

    if not isinstance(path, str) or not path.strip():
        raise RevisionError("a document path is required")
    text = unicodedata.normalize("NFC", path.strip()).replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise RevisionError(f"document path must be project-relative: {path}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise RevisionError(f"document path must stay inside the project: {path}")
    return "/".join(parts)


def case_collisions(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Report pairs of distinct paths that differ only by case."""

    seen: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for path in paths:
        normalized = normalize_relative_path(path)
        folded = normalized.casefold()
        previous = seen.get(folded)
        if previous is not None and previous != normalized:
            collisions.append((previous, normalized))
        else:
            seen[folded] = normalized
    return collisions


def document_id(relative_path: str) -> str:
    """A logical document identity that survives moves of the project root."""

    normalized = normalize_relative_path(relative_path)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{DOCUMENT_ID_PREFIX}-{digest}"


def source_revision_id(document: str, content_sha256: str) -> str:
    """Bind one exact byte sequence to one document.

    Identical bytes reappearing later still produce a new revision event, so
    the id covers the document as well as the content.
    """

    if not _is_sha256(content_sha256):
        raise RevisionError(f"source revision needs a sha256 digest: {content_sha256!r}")
    payload = f"{document}\n{content_sha256.lower()}"
    return f"{SOURCE_REVISION_PREFIX}-{_digest(payload)}"


def processing_fingerprint(stages: Sequence["ProcessingStage"]) -> str:
    """Hash the full processing pipeline identity, stage by stage."""

    payload = [stage.as_payload() for stage in stages]
    return _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def parse_revision_id(
    source_revision: str,
    *,
    database_schema_version: int,
    processing_fingerprint_value: str,
) -> str:
    """Bind a parse result to its source bytes, schema, and pipeline."""

    if not isinstance(database_schema_version, int) or database_schema_version < 1:
        raise RevisionError("database_schema_version must be a positive integer")
    if not processing_fingerprint_value:
        raise RevisionError("parse revision needs a processing fingerprint")
    payload = (
        f"{source_revision}\n{database_schema_version}\n"
        f"{processing_fingerprint_value}"
    )
    return f"{PARSE_REVISION_PREFIX}-{_digest(payload)}"


def bundle_id(parse_revision: str, bundle_payload: Any) -> str:
    """Identify a published revision bundle by revision and content."""

    payload = f"{parse_revision}\n{_canonical(bundle_payload)}"
    return f"{BUNDLE_PREFIX}-{_digest(payload)}"


def cache_key(
    *,
    stage: str,
    ruleset_version: str,
    input_sha256: str,
    upstream_sha256: Sequence[str] = (),
    runtime_identity: str = "",
    runtime_version: str = "",
    config: Any = None,
    output_schema_version: str = "",
) -> str:
    """One stage cache key, per the protocol's cache-key contract."""

    payload = {
        "stage": stage,
        "ruleset_version": ruleset_version,
        "input_sha256": input_sha256,
        "upstream_sha256": list(upstream_sha256),
        "runtime_identity": runtime_identity,
        "runtime_version": runtime_version,
        "config": config,
        "output_schema_version": output_schema_version,
    }
    return _digest(_canonical(payload))


def file_sha256(path: Path) -> str:
    """The one way this project hashes a file: streaming SHA-256 of its bytes."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ProcessingStage:
    """One stage of the pipeline as it actually ran, not as it was configured."""

    name: str
    ruleset_version: str
    output_schema_version: str = ""
    handler: str = ""
    handler_version: str = ""
    execution_status: str = "succeeded"
    quality_status: str = "accepted"
    reason_code: str = ""
    fallback_used: bool = False
    input_sha256: str = ""
    output_sha256: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ruleset_version": self.ruleset_version,
            "output_schema_version": self.output_schema_version,
            "handler": self.handler,
            "handler_version": self.handler_version,
            "execution_status": self.execution_status,
            "quality_status": self.quality_status,
            "reason_code": self.reason_code,
            "fallback_used": self.fallback_used,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProcessingStage":
        return cls(
            name=str(payload["name"]),
            ruleset_version=str(payload["ruleset_version"]),
            output_schema_version=str(payload.get("output_schema_version") or ""),
            handler=str(payload.get("handler") or ""),
            handler_version=str(payload.get("handler_version") or ""),
            execution_status=str(payload.get("execution_status") or "succeeded"),
            quality_status=str(payload.get("quality_status") or "accepted"),
            reason_code=str(payload.get("reason_code") or ""),
            fallback_used=bool(payload.get("fallback_used", False)),
            input_sha256=str(payload.get("input_sha256") or ""),
            output_sha256=str(payload.get("output_sha256") or ""),
        )


@dataclass(frozen=True)
class ProcessingManifest:
    """The stage list a parse revision was produced with."""

    stages: tuple[ProcessingStage, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        return processing_fingerprint(self.stages)

    def as_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "stages": [stage.as_payload() for stage in self.stages],
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProcessingManifest":
        return cls(
            stages=tuple(
                ProcessingStage.from_payload(stage)
                for stage in payload.get("stages") or ()
            ),
            notes=tuple(str(note) for note in payload.get("notes") or ()),
        )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_sha256(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


__all__ = [
    "BUNDLE_PREFIX",
    "DOCUMENT_ID_PREFIX",
    "PARSE_REVISION_PREFIX",
    "ProcessingManifest",
    "ProcessingStage",
    "RevisionError",
    "SOURCE_REVISION_PREFIX",
    "bundle_id",
    "cache_key",
    "case_collisions",
    "document_id",
    "file_sha256",
    "normalize_relative_path",
    "parse_revision_id",
    "processing_fingerprint",
    "source_revision_id",
]
