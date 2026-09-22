"""Load and validate evaluation corpora."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    CorpusManifest,
    Sample,
    SchemaError,
    canonical_fingerprint,
)


MANIFEST_NAME = "manifest.json"
SAMPLE_DIRECTORY = "samples"


@dataclass(frozen=True)
class EvaluationCorpus:
    root: Path
    manifest: CorpusManifest
    samples: tuple[Sample, ...]

    def sample(self, sample_id: str) -> Sample:
        for sample in self.samples:
            if sample.sample_id == sample_id:
                return sample
        raise KeyError(sample_id)

    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "manifest": self.manifest.as_payload(),
                "samples": [sample.as_payload() for sample in self.samples],
            }
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.manifest.name,
            "version": self.manifest.version,
            "split": self.manifest.split,
            "frozen": self.manifest.frozen,
            "guide_version": self.manifest.guide_version,
            "sample_count": len(self.samples),
            "sample_ids": [sample.sample_id for sample in self.samples],
            "fingerprint": self.fingerprint(),
        }


def load_corpus(
    root: Path, *, allow_unadjudicated_high_risk: bool = False
) -> EvaluationCorpus:
    """Read a corpus directory and enforce the annotation/freeze protocol."""

    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SchemaError(f"Corpus manifest is missing: {manifest_path}")
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = CorpusManifest.from_payload(manifest_payload, str(root))

    sample_directory = root / SAMPLE_DIRECTORY
    if not sample_directory.is_dir():
        raise SchemaError(f"Corpus has no {SAMPLE_DIRECTORY}/ directory: {root}")
    sample_paths = sorted(sample_directory.glob("*.json"))
    if not sample_paths:
        raise SchemaError(f"Corpus has no samples: {sample_directory}")

    samples = []
    seen: set[str] = set()
    for sample_path in sample_paths:
        payload = json.loads(sample_path.read_text(encoding="utf-8"))
        sample = Sample.from_payload(payload, sample_path.name, manifest.documents)
        if sample.sample_id in seen:
            raise SchemaError(f"Duplicate sample_id: {sample.sample_id}")
        seen.add(sample.sample_id)
        samples.append(sample)

    samples = tuple(samples)
    _verify_fingerprints(manifest, samples, root)
    _verify_annotation_protocol(samples, allow_unadjudicated_high_risk)
    return EvaluationCorpus(root=root, manifest=manifest, samples=samples)


def refresh_manifest(root: Path, *, force: bool = False) -> Path:
    """Recompute sample fingerprints after an intentional corpus edit.

    Frozen corpora refuse to refresh unless ``force`` is set, and forcing is
    reported by the caller: rewriting frozen labels invalidates every earlier
    quality report that used them.
    """

    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SchemaError(f"Corpus manifest is missing: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("frozen") and not force:
        raise SchemaError(
            f"Corpus {payload.get('name')} is frozen; refresh it only with "
            "force=True after deciding to invalidate earlier reports"
        )

    registry = payload.get("documents") or {}
    sample_directory = root / SAMPLE_DIRECTORY
    fingerprints: dict[str, str] = {}
    for sample_path in sorted(sample_directory.glob("*.json")):
        sample = Sample.from_payload(
            json.loads(sample_path.read_text(encoding="utf-8")),
            sample_path.name,
            registry,
        )
        if sample.sample_id in fingerprints:
            raise SchemaError(f"Duplicate sample_id: {sample.sample_id}")
        fingerprints[sample.sample_id] = sample.fingerprint()

    if not fingerprints:
        raise SchemaError(f"Corpus has no samples: {sample_directory}")
    payload["sample_fingerprints"] = dict(sorted(fingerprints.items()))
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def _verify_fingerprints(
    manifest: CorpusManifest, samples: tuple[Sample, ...], root: Path
) -> None:
    recorded = dict(manifest.sample_fingerprints)
    actual = {sample.sample_id: sample.fingerprint() for sample in samples}

    missing = sorted(set(actual) - set(recorded))
    unlisted = sorted(set(recorded) - set(actual))
    changed = sorted(
        sample_id
        for sample_id, fingerprint in actual.items()
        if sample_id in recorded and recorded[sample_id] != fingerprint
    )
    if not (missing or unlisted or changed):
        return

    detail = []
    if missing:
        detail.append(f"samples without a recorded fingerprint: {missing}")
    if unlisted:
        detail.append(f"recorded fingerprints without a sample: {unlisted}")
    if changed:
        detail.append(f"modified samples: {changed}")

    if manifest.frozen:
        raise SchemaError(
            f"Frozen corpus {manifest.name}@{manifest.version} was modified "
            f"({root}); frozen labels must not change. " + "; ".join(detail)
        )
    raise SchemaError(
        f"Corpus {manifest.name}@{manifest.version} manifest is out of date ({root}); "
        "refresh manifest.json with the new fingerprints. " + "; ".join(detail)
    )


def _verify_annotation_protocol(
    samples: tuple[Sample, ...], allow_unadjudicated_high_risk: bool
) -> None:
    if allow_unadjudicated_high_risk:
        return
    offenders = [
        sample.sample_id
        for sample in samples
        if sample.high_risk and sample.annotation.needs_adjudication
    ]
    if offenders:
        raise SchemaError(
            "High-risk samples need two independent annotators plus adjudication: "
            f"{offenders}"
        )
