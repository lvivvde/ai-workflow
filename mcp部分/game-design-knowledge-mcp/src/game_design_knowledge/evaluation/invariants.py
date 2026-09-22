"""Release-blocking invariants.

These are not averaged into any score. A single violation fails the run and
names the offending sample, because the protocol treats source corruption,
unsupported facts, silent conflict resolution, broken source chains, and
hidden degradation as release blockers rather than accuracy regressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .claims import claim_label, claim_position, claims_for
from .schema import Sample


INVARIANT_SOURCE_EVIDENCE_UNCHANGED = "source_evidence_unchanged"
INVARIANT_NO_UNSUPPORTED_PROJECT_FACT = "no_unsupported_project_fact"
INVARIANT_NO_SILENT_CONFLICT_RESOLUTION = "no_silent_conflict_resolution"
INVARIANT_SOURCE_REFERENCES_TRACEABLE = "source_references_traceable"
INVARIANT_DEGRADATION_NOT_HIDDEN = "degradation_not_hidden"

RELEASE_BLOCKING_INVARIANTS: tuple[str, ...] = (
    INVARIANT_SOURCE_EVIDENCE_UNCHANGED,
    INVARIANT_NO_UNSUPPORTED_PROJECT_FACT,
    INVARIANT_NO_SILENT_CONFLICT_RESOLUTION,
    INVARIANT_SOURCE_REFERENCES_TRACEABLE,
    INVARIANT_DEGRADATION_NOT_HIDDEN,
)

WINNER_SELECTION_FIELDS = (
    "preferred_statement_ref",
    "preferred_evidence_id",
    "selected_evidence_id",
    "winner",
    "winning_statement_ref",
    "canonical_value",
)

CONTENT_LAYERS = frozenset(
    {
        "source",
        "transcription",
        "visual_interpretation",
        "notation_interpretation",
        "statement",
        "explanation",
    }
)

EVIDENCE_STATUSES = frozenset(
    {"explicit", "machine-supported", "verified", "candidate", "conflict"}
)


@dataclass(frozen=True)
class InvariantViolation:
    sample_id: str
    mode: str
    detail: str

    def as_payload(self) -> dict[str, str]:
        return {
            "sample_id": self.sample_id,
            "mode": self.mode,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InvariantResult:
    name: str
    violations: tuple[InvariantViolation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "violation_count": len(self.violations),
            "violations": [item.as_payload() for item in self.violations],
        }


@dataclass(frozen=True)
class SampleExecution:
    """Everything the invariants need about one tool call on one sample."""

    sample: Sample
    mode: str
    tool: str
    response: Mapping[str, Any] | None
    error: str | None
    latency_seconds: float
    sources_before: Mapping[str, str] = field(default_factory=dict)
    sources_after: Mapping[str, str] = field(default_factory=dict)
    index_document_paths: frozenset[str] = frozenset()
    index_asset_paths: frozenset[str] = frozenset()
    recorded_degradation: Mapping[str, int] = field(default_factory=dict)

    @property
    def sample_id(self) -> str:
        return self.sample.sample_id


def evaluate_invariants(
    executions: Iterable[SampleExecution],
    checks: Sequence[tuple[str, Callable[[SampleExecution], list[str]]]] | None = None,
) -> tuple[InvariantResult, ...]:
    """Run every release-blocking check over every sample execution."""

    executions = tuple(executions)
    selected = checks if checks is not None else _default_checks()
    results = []
    for name, check in selected:
        violations: list[InvariantViolation] = []
        for execution in executions:
            for detail in check(execution):
                violations.append(
                    InvariantViolation(
                        sample_id=execution.sample_id,
                        mode=execution.mode,
                        detail=detail,
                    )
                )
        results.append(InvariantResult(name=name, violations=tuple(violations)))
    return tuple(results)


def violation_summary(results: Iterable[InvariantResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        if result.ok:
            continue
        for violation in result.violations:
            lines.append(
                f"[{result.name}] {violation.sample_id} ({violation.mode}): "
                f"{violation.detail}"
            )
    return lines


def _default_checks() -> tuple[
    tuple[str, Callable[[SampleExecution], list[str]]], ...
]:
    return (
        (INVARIANT_SOURCE_EVIDENCE_UNCHANGED, _check_source_evidence_unchanged),
        (INVARIANT_NO_UNSUPPORTED_PROJECT_FACT, _check_no_unsupported_project_fact),
        (
            INVARIANT_NO_SILENT_CONFLICT_RESOLUTION,
            _check_no_silent_conflict_resolution,
        ),
        (INVARIANT_SOURCE_REFERENCES_TRACEABLE, _check_source_references_traceable),
        (INVARIANT_DEGRADATION_NOT_HIDDEN, _check_degradation_not_hidden),
    )


def _check_source_evidence_unchanged(execution: SampleExecution) -> list[str]:
    details: list[str] = []
    before = dict(execution.sources_before)
    after = dict(execution.sources_after)
    for path, digest in sorted(before.items()):
        if path not in after:
            details.append(f"source file disappeared during the run: {path}")
        elif after[path] != digest:
            details.append(f"source file content changed during the run: {path}")
    for path in sorted(set(after) - set(before)):
        details.append(f"source file appeared during the run: {path}")

    response = execution.response or {}
    status = response.get("status")
    if status in {"found", "partial", "degraded"} and execution.error:
        details.append(
            f"response claims {status!r} although the call failed: {execution.error}"
        )
    return details


def _check_no_unsupported_project_fact(execution: SampleExecution) -> list[str]:
    response = execution.response
    if response is None:
        return []
    status = response.get("status")
    details: list[str] = []
    claims = claims_for(execution.tool, response)
    asserted = status in {"found", "partial", "degraded", "stale"} and bool(claims)

    if executing_annotated_no_answer(execution) and asserted:
        details.append(
            "response asserts project facts although the annotation records no "
            f"answer for this sample ({len(claims)} claims)"
        )
        return details

    available_ids = {claim_label(claim) for claim in claims}
    for claim in claims:
        supported_by = claim.get("supported_by")
        if isinstance(supported_by, (list, tuple, set)) and not supported_by:
            details.append(
                f"claim {claim_label(claim)} declares an empty supported_by list"
            )
        elif supported_by is not None:
            for reference in supported_by:
                if reference not in available_ids:
                    details.append(
                        f"claim {claim_label(claim)} is supported by {reference!r}, "
                        "which the same response never returns"
                    )

        # Content-layer checks apply to every claim: a response that never
        # declares ``supported_by`` can still promote a machine transcription,
        # so skipping these when the list is absent would hide exactly the
        # promotion the invariant exists to catch.
        content_layer = claim.get("content_layer")
        if content_layer is not None and content_layer not in CONTENT_LAYERS:
            details.append(
                f"claim {claim_label(claim)} uses unknown content_layer "
                f"{content_layer!r}"
            )
        evidence_status = claim.get("evidence_status")
        if evidence_status is not None and evidence_status not in EVIDENCE_STATUSES:
            details.append(
                f"claim {claim_label(claim)} uses unknown evidence_status "
                f"{evidence_status!r}"
            )
        if (
            content_layer == "transcription"
            and evidence_status in {"verified", "explicit"}
            and claim.get("review_status") != "confirmed"
        ):
            details.append(
                f"claim {claim_label(claim)} promotes machine transcription to "
                f"{evidence_status!r} without a confirmed review event"
            )
    return details


def _check_no_silent_conflict_resolution(execution: SampleExecution) -> list[str]:
    response = execution.response or {}
    expects_conflict = (
        execution.sample.expected.conflict_group_expected
        or execution.sample.stratum.values["conflict_state"] == "present"
    )
    details: list[str] = []
    for key in WINNER_SELECTION_FIELDS:
        value = response.get(key)
        if value not in (None, [], {}, ""):
            details.append(
                f"response selects a conflict winner through {key!r}={value!r}"
            )
    if not expects_conflict:
        return details

    status = response.get("status")
    if status not in {"found", "partial", "degraded", "stale"}:
        return details
    conflicts = response.get("conflicts")
    if not isinstance(conflicts, list) or not conflicts:
        conflicts = response.get("conflict_groups")
    if not isinstance(conflicts, list) or not conflicts:
        details.append(
            "sample is annotated as a conflict but the response exposes no "
            "conflict group"
        )
    return details


def _check_source_references_traceable(execution: SampleExecution) -> list[str]:
    response = execution.response
    if response is None:
        return []
    if response.get("status") not in {"found", "partial", "degraded", "stale"}:
        return []

    details: list[str] = []
    known_documents = set(execution.index_document_paths)
    known_assets = set(execution.index_asset_paths)
    for claim in claims_for(execution.tool, response):
        label = claim_label(claim)
        document = claim.get("source_document")
        asset = claim.get("asset_path")
        source_ref = claim.get("source_ref")
        if document is None and asset is None and source_ref is None:
            details.append(f"claim {label} carries no source reference")
        if isinstance(document, str) and known_documents and document not in known_documents:
            details.append(
                f"claim {label} points at a document outside the index: {document}"
            )
        if isinstance(asset, str) and known_assets and asset not in known_assets:
            details.append(f"claim {label} points at an unregistered asset: {asset}")

        if claim_position(claim) is None:
            details.append(f"claim {label} carries no locator")

        for derived in claim.get("derived_from") or []:
            if not isinstance(derived, str) or not derived.strip():
                details.append(f"claim {label} has an empty derived_from entry")
    return details


def executing_annotated_no_answer(execution: SampleExecution) -> bool:
    """True when the annotation says this question has no supported answer."""

    return execution.sample.expected.response_state in {
        "not_found",
        "failed",
        "ambiguous",
    }


def _check_degradation_not_hidden(execution: SampleExecution) -> list[str]:
    response = execution.response
    recorded = dict(execution.recorded_degradation)
    if not recorded:
        return []
    if response is None:
        if execution.error is None:
            return [
                "run recorded degradation and the sample produced neither a "
                "response nor a reported failure"
            ]
        return []

    observed = dict(response.get("index_status") or {})
    response_meta = dict(response.get("response_meta") or {})
    events = response_meta.get("degradation_events")
    uncertainties = response.get("uncertainties")
    surfaced = response_meta or uncertainties

    details: list[str] = []
    for key, value in sorted(recorded.items()):
        if value <= 0:
            continue
        if key in observed:
            if observed[key] != value and not surfaced:
                details.append(
                    f"degradation {key}={value} is reported as {observed[key]!r} "
                    "with no degradation metadata"
                )
            continue
        if events or uncertainties or response_meta.get("actual_capabilities"):
            continue
        details.append(
            f"run recorded {key}={value} but the response hides it"
        )
    return details
