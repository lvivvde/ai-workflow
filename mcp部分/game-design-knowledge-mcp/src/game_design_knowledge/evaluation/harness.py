"""Component and end-to-end evaluation harness.

One run materializes a corpus, builds a disposable index, calls the frozen V1
tools both in-process (component) and through the MCP client (end-to-end),
then reports layers, strata, and response states separately while enforcing the
release-blocking invariants.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Callable, Mapping, Sequence

from ..index_build import build_index_atomically
from ..shared_index import SharedIndexRead
from ..evidence_package import image_layout, image_transcription
from .claims import (
    FOUND_FAMILY,
    claim_is_traceable,
    claims_for,
    jsonable,
)
from .corpus import EvaluationCorpus
from .fixtures import materialize_corpus_samples
from .invariants import (
    InvariantResult,
    SampleExecution,
    evaluate_invariants,
    violation_summary,
)
from .metrics import (
    character_error_rate,
    coverage_by_contains,
    mean_ignoring_none,
    precision_at_k,
    prf1_by_contains,
    rate_with_interval,
    recall_at_k,
    reciprocal_rank,
    region_completeness,
    response_state_confusion,
    state_precision_recall,
    word_error_rate,
)
from .network_guard import block_network
from .reporting import (
    CapabilityResult,
    LayerResult,
    StratumResult,
    render_markdown,
)
from .schema import (
    EVALUATION_LAYERS,
    EVALUATION_MODES,
    EvaluationRunManifest,
    EnvironmentRecord,
    EvidenceExpectation,
    NotationExpectation,
    Sample,
    canonical_fingerprint,
)


STRATUM_GROUP_KEYS = ("source_type", "content_type", "language", "difficulty")


@dataclass(frozen=True)
class ItemResult:
    sample: Sample
    mode: str
    tool: str
    arguments: Mapping[str, Any]
    expected_state: str
    observed_state: str
    latency_seconds: float
    error: str | None = None
    response: Mapping[str, Any] | None = None

    @property
    def sample_id(self) -> str:
        return self.sample.sample_id

    @property
    def state_satisfied(self) -> bool:
        if self.expected_state == self.observed_state:
            return True
        return self.expected_state == "found" and self.observed_state in FOUND_FAMILY

    def as_payload(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "mode": self.mode,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "expected_state": self.expected_state,
            "observed_state": self.observed_state,
            "state_satisfied": self.state_satisfied,
            "latency_seconds": round(self.latency_seconds, 6),
            "error": self.error,
            "response": jsonable(self.response),
        }


@dataclass
class EvaluationRun:
    run_id: str
    manifest: EvaluationRunManifest
    corpus_descriptions: tuple[Mapping[str, Any], ...]
    items: tuple[ItemResult, ...]
    invariant_results: tuple[InvariantResult, ...]
    layer_results: tuple[LayerResult, ...]
    stratum_results: tuple[StratumResult, ...]
    response_states: Mapping[str, Mapping[str, Mapping[str, int]]]
    capability_results: tuple[CapabilityResult, ...] = ()
    network_violations: tuple[str, ...] = ()
    build_error: str | None = None
    notes: tuple[str, ...] = ()
    artifacts: Mapping[str, Path] = field(default_factory=dict)

    @property
    def invariant_ok(self) -> bool:
        return all(result.ok for result in self.invariant_results)

    @property
    def ok(self) -> bool:
        """A run is green only when it measured something and broke nothing.

        An empty run is a failed gate, not a pass: a mistyped mode list would
        otherwise report "all invariants passed" without executing a sample.
        """

        return (
            self.invariant_ok
            and not self.network_violations
            and self.build_error is None
            and bool(self.items)
        )

    def failure_summary(self) -> list[str]:
        lines: list[str] = []
        if self.build_error is not None:
            lines.append(f"[index_build] {self.build_error}")
        if not self.items:
            lines.append(
                "[harness] the run executed no sample; a typo'd mode list or an "
                "empty selection cannot be reported as a pass"
            )
        lines.extend(violation_summary(self.invariant_results))
        for violation in self.network_violations:
            lines.append(f"[network] {violation}")
        return lines

    def as_payload(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.as_payload(),
            "ok": self.ok,
            "failures": self.failure_summary(),
            "invariants": [item.as_payload() for item in self.invariant_results],
            "layers": [item.as_payload() for item in self.layer_results],
            "strata": [item.as_payload() for item in self.stratum_results],
            "capability_packs": [
                item.as_payload() for item in self.capability_results
            ],
            "response_states": jsonable(self.response_states),
            "network_violations": list(self.network_violations),
            "notes": list(self.notes),
        }


def run_evaluation(
    corpora: Sequence[EvaluationCorpus],
    *,
    workspace: Path | None = None,
    runs_directory: Path | None = None,
    hardware_profile: str = "baseline",
    modes: Sequence[str] = ("component", "e2e"),
    cold_start: bool = True,
    run_id: str | None = None,
) -> EvaluationRun:
    """Run one evaluation and optionally publish its artifacts."""

    if not corpora:
        raise ValueError("At least one corpus is required")
    unknown_modes = sorted(set(modes) - EVALUATION_MODES)
    if unknown_modes:
        raise ValueError(
            f"Unknown evaluation modes {unknown_modes}; expected a subset of "
            f"{sorted(EVALUATION_MODES)}"
        )

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="gdk-eval-")
        workspace = Path(temporary.name)
    else:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

    started_at = _now()
    run_id = run_id or _default_run_id(corpora, started_at)
    notes: list[str] = []
    network_violations: list[str] = []
    items: list[ItemResult] = []
    executions: list[SampleExecution] = []
    build_error: str | None = None
    build_report: Mapping[str, Any] = {}
    build_seconds = 0.0
    index_status: Mapping[str, Any] = {}
    index_document_paths: frozenset[str] = frozenset()
    index_asset_paths: frozenset[str] = frozenset()
    recorded_degradation: dict[str, int] = {}
    peak_memory_bytes = 0
    disk_bytes = 0
    layer_observations: dict[str, Any] = {}
    review_seeds: list[dict[str, Any]] = []

    source_directory = workspace / "documents"
    index_directory = workspace / "index"
    samples = [sample for corpus in corpora for sample in corpus.samples]
    document_paths = {
        document.path: str((source_directory / document.path).resolve())
        for sample in samples
        for document in sample.documents
    }

    previous_environment = {
        key: os.environ.get(key)
        for key in ("GAME_DESIGN_INDEX_DIR", "GAME_DESIGN_PROJECT_ROOT")
    }
    tracemalloc.start()
    try:
        with block_network(network_violations):
            materialize_corpus_samples(samples, source_directory)
            sources_before = _hash_tree(source_directory)

            started = time.perf_counter()
            try:
                build_report = build_index_atomically(source_directory, index_directory)
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                build_error = f"{type(error).__name__}: {error}"
            build_seconds = time.perf_counter() - started

            sources_after = _hash_tree(source_directory)

            if build_error is None:
                database_path = index_directory / "knowledge.sqlite"
                with SharedIndexRead(database_path) as index:
                    index_status = dict(index.status())
                    index_document_paths = frozenset(
                        str(index.resolve_source_path(row["path"]))
                        for row in index.fetchall("SELECT path FROM documents")
                    )
                    index_asset_paths = frozenset(
                        str(index.resolve_source_path(row["asset_path"]))
                        for row in index.fetchall(
                            "SELECT DISTINCT asset_path FROM images"
                        )
                    )
                    layer_observations = _layer_observations(
                        index, document_paths
                    )
                    review_seeds = _seed_review_actions(workspace, corpora, index)
                recorded_degradation = {
                    "ocr_failed": int(index_status.get("ocr_failed") or 0),
                    "ocr_unavailable": int(index_status.get("ocr_unavailable") or 0),
                    "stale_documents": int(index_status.get("stale_documents") or 0),
                }
                os.environ["GAME_DESIGN_INDEX_DIR"] = str(index_directory)
                os.environ["GAME_DESIGN_PROJECT_ROOT"] = str(workspace)

                for sample in samples:
                    for mode in _selected_modes(sample, modes):
                        item, execution = _execute_sample(
                            sample,
                            mode,
                            document_paths=document_paths,
                            sources_before=sources_before,
                            sources_after=sources_after,
                            index_document_paths=index_document_paths,
                            index_asset_paths=index_asset_paths,
                            recorded_degradation=recorded_degradation,
                        )
                        items.append(item)
                        executions.append(execution)
            else:
                notes.append("The index build failed; no sample was executed.")

            peak_memory_bytes = tracemalloc.get_traced_memory()[1]
            disk_bytes = _directory_size(workspace)
    finally:
        tracemalloc.stop()
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if temporary is not None:
            temporary.cleanup()

    invariant_results = evaluate_invariants(executions)
    layer_results, stratum_results, response_states = _summarize(
        items=items,
        index_status=index_status,
        build_report=build_report,
        build_seconds=build_seconds,
        peak_memory_bytes=peak_memory_bytes,
        disk_bytes=disk_bytes,
        build_error=build_error,
        recorded_degradation=recorded_degradation,
        layer_observations=layer_observations,
    )

    manifest = EvaluationRunManifest(
        run_id=run_id,
        started_at=started_at,
        finished_at=_now(),
        corpus={
            "corpora": [corpus.describe() for corpus in corpora],
            "combined_fingerprint": canonical_fingerprint(
                [corpus.describe() for corpus in corpora]
            ),
        },
        environment=_environment_record(
            hardware_profile, cold_start
        ).as_payload(),
        schema_versions={
            "public_schema_version": "1.0",
            "database_schema_version": index_status.get("schema_version"),
            "state_schema_version": None,
            "cache_schema_version": None,
        },
        capabilities={
            "hardware_profile": hardware_profile,
            "capability_packs": sorted(
                {pack for sample in samples for pack in sample.capabilities}
            ),
            "degradation": dict(recorded_degradation),
        },
        config={
            "modes": list(modes),
            "random_seed": 0,
            "cold_start": cold_start,
            "network_access": "blocked",
            "review_seeds": review_seeds,
        },
        artifacts={},
    )

    run = EvaluationRun(
        run_id=run_id,
        manifest=manifest,
        corpus_descriptions=tuple(corpus.describe() for corpus in corpora),
        items=tuple(items),
        invariant_results=invariant_results,
        layer_results=layer_results,
        stratum_results=stratum_results,
        response_states=response_states,
        capability_results=_capability_results(items),
        network_violations=tuple(network_violations),
        build_error=build_error,
        notes=tuple(notes),
    )

    if runs_directory is not None:
        run.artifacts = _write_artifacts(run, Path(runs_directory))
    return run


def _execute_sample(
    sample: Sample,
    mode: str,
    *,
    document_paths: Mapping[str, str],
    sources_before: Mapping[str, str],
    sources_after: Mapping[str, str],
    index_document_paths: frozenset[str],
    index_asset_paths: frozenset[str],
    recorded_degradation: Mapping[str, int],
) -> tuple[ItemResult, SampleExecution]:
    arguments = _resolve_arguments(dict(sample.arguments), document_paths)
    started = time.perf_counter()
    error: str | None = None
    response: Mapping[str, Any] | None = None
    try:
        if mode == "component":
            response = _call_component(sample.tool, arguments)
        else:
            response = asyncio.run(_call_e2e(sample.tool, arguments))
    except Exception as failure:  # noqa: BLE001 - recorded as a result
        error = f"{type(failure).__name__}: {failure}"
    latency = time.perf_counter() - started

    observed = "failed"
    if isinstance(response, Mapping):
        observed = str(response.get("status") or "failed")

    item = ItemResult(
        sample=sample,
        mode=mode,
        tool=sample.tool,
        arguments=arguments,
        expected_state=sample.expected.response_state,
        observed_state=observed,
        latency_seconds=latency,
        error=error,
        response=response,
    )
    execution = SampleExecution(
        sample=sample,
        mode=mode,
        tool=sample.tool,
        response=response,
        error=error,
        latency_seconds=latency,
        sources_before=sources_before,
        sources_after=sources_after,
        index_document_paths=index_document_paths,
        index_asset_paths=index_asset_paths,
        recorded_degradation=recorded_degradation,
    )
    return item, execution


def _call_component(tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    from .. import server

    callable_tool: Callable[..., Mapping[str, Any]] = getattr(server, tool)
    return callable_tool(**arguments)


async def _call_e2e(tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    from mcp import Client

    from ..server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(tool, dict(arguments))
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, Mapping):
        raise RuntimeError(f"{tool} returned no structured content")
    return structured


def _selected_modes(sample: Sample, modes: Sequence[str]) -> tuple[str, ...]:
    return tuple(mode for mode in modes if mode in sample.modes)


def _resolve_arguments(
    arguments: Mapping[str, Any], document_paths: Mapping[str, str]
) -> dict[str, Any]:
    """Expand ``{{path:<corpus relative path>}}`` into a run-local absolute path."""

    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            if value.startswith("{{path:") and value.endswith("}}"):
                relative = value[len("{{path:") : -2]
                if relative not in document_paths:
                    raise KeyError(
                        f"Sample refers to an unknown corpus document: {relative}"
                    )
                return document_paths[relative]
            return value
        if isinstance(value, Mapping):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return {key: resolve(value) for key, value in arguments.items()}


def _summarize(
    *,
    items: Sequence[ItemResult],
    index_status: Mapping[str, Any],
    build_report: Mapping[str, Any],
    build_seconds: float,
    peak_memory_bytes: int,
    disk_bytes: int,
    build_error: str | None,
    recorded_degradation: Mapping[str, int],
    layer_observations: Mapping[str, Any] | None = None,
) -> tuple[
    tuple[LayerResult, ...],
    tuple[StratumResult, ...],
    dict[str, dict[str, dict[str, int]]],
]:
    modes = sorted({item.mode for item in items})
    layer_results: list[LayerResult] = []
    response_states: dict[str, dict[str, dict[str, int]]] = {}
    observations = dict(layer_observations or {})

    if not modes:
        # Report nothing rather than inventing a mode: an empty selection is
        # not the same thing as a component-mode measurement of nothing.
        return (), (), {}

    for mode in modes:
        mode_items = [item for item in items if item.mode == mode]
        response_states[mode] = response_state_confusion(
            (item.expected_state, item.observed_state) for item in mode_items
        )
        layer_results.extend(
            _mode_layers(
                mode=mode,
                items=mode_items,
                index_status=index_status,
                build_report=build_report,
                build_seconds=build_seconds,
                peak_memory_bytes=peak_memory_bytes,
                disk_bytes=disk_bytes,
                build_error=build_error,
                recorded_degradation=recorded_degradation,
                layer_observations=observations,
            )
        )

    return tuple(layer_results), _stratum_results(items), response_states


def _mode_layers(
    *,
    mode: str,
    items: Sequence[ItemResult],
    index_status: Mapping[str, Any],
    build_report: Mapping[str, Any],
    build_seconds: float,
    peak_memory_bytes: int,
    disk_bytes: int,
    build_error: str | None,
    recorded_degradation: Mapping[str, int],
    layer_observations: Mapping[str, Any] | None = None,
) -> list[LayerResult]:
    sample_ids = tuple(item.sample_id for item in items)
    observations = dict(layer_observations or {})
    if build_error is not None:
        results = [
            LayerResult(
                layer="source_import",
                mode=mode,
                status="failed",
                metrics={"build_error": build_error},
                sample_ids=sample_ids,
                notes=("The index build failed, so no sample could be served.",),
            )
        ]
        results.extend(
            LayerResult(
                layer=layer,
                mode=mode,
                status="unavailable",
                notes=("Skipped because the index build failed.",),
            )
            for layer in EVALUATION_LAYERS
            if layer != "source_import"
        )
        return results

    return [
        _source_import_layer(mode, build_report, build_seconds, index_status, sample_ids),
        _ocr_layer(
            mode, index_status, sample_ids, items=items, observations=observations
        ),
        *_layout_layers(
            mode,
            index_status,
            sample_ids,
            items=items,
            observations=observations,
        ),
        _notation_layer(mode, items),
        _statement_layer(mode, items),
        _retrieval_layer(mode, items),
        _conflict_layer(mode, items),
        _explanation_layer(mode, items),
        _performance_layer(
            mode,
            items,
            build_seconds=build_seconds,
            peak_memory_bytes=peak_memory_bytes,
            disk_bytes=disk_bytes,
            recorded_degradation=recorded_degradation,
            sample_ids=sample_ids,
        ),
    ]


def _source_import_layer(
    mode: str,
    build_report: Mapping[str, Any],
    build_seconds: float,
    index_status: Mapping[str, Any],
    sample_ids: Sequence[str],
) -> LayerResult:
    documents_indexed = int(build_report.get("documents_indexed") or 0)
    notes = []
    status = "measured"
    if documents_indexed == 0:
        status = "failed"
        notes.append("No document was indexed.")
    if index_status.get("is_stale") is True:
        notes.append("The freshly built index reports itself as stale.")
    return LayerResult(
        layer="source_import",
        mode=mode,
        status=status,
        metrics={
            "build_seconds": round(build_seconds, 4),
            "documents_indexed": documents_indexed,
            "documents_added": int(build_report.get("documents_added") or 0),
            "documents_updated": int(build_report.get("documents_updated") or 0),
            "images_indexed": int(build_report.get("images_indexed") or 0),
            "features_indexed": int(build_report.get("features_indexed") or 0),
            "schema_version": index_status.get("schema_version"),
            "is_stale": index_status.get("is_stale"),
            "samples_requested": len(sample_ids),
        },
        sample_ids=tuple(sample_ids),
        notes=tuple(notes),
    )


def _ocr_layer(
    mode: str,
    index_status: Mapping[str, Any],
    sample_ids: Sequence[str],
    *,
    items: Sequence[ItemResult] = (),
    observations: Mapping[str, Any] | None = None,
) -> LayerResult:
    annotated = [
        item
        for item in items
        if item.sample.expected.transcription is not None
    ]
    if annotated:
        return _annotated_ocr_layer(
            mode, index_status, annotated, observations or {}
        )

    images = int(index_status.get("images_indexed") or 0)
    if images == 0:
        return LayerResult(
            layer="ocr_transcription",
            mode=mode,
            status="unavailable",
            notes=("This corpus contains no images, so OCR was not exercised.",),
        )
    succeeded = int(index_status.get("ocr_succeeded") or 0)
    regions = int(index_status.get("ocr_regions") or 0)
    return LayerResult(
        layer="ocr_transcription",
        mode=mode,
        status="measured",
        metrics={
            "images_indexed": images,
            "ocr_succeeded": succeeded,
            "ocr_failed": int(index_status.get("ocr_failed") or 0),
            "ocr_unavailable": int(index_status.get("ocr_unavailable") or 0),
            "ocr_success_rate": succeeded / images,
            "ocr_regions": regions,
            "ocr_machine_supported": int(
                index_status.get("ocr_machine_supported") or 0
            ),
            "ocr_low_quality": int(index_status.get("ocr_low_quality") or 0),
            "ocr_fallbacks": int(index_status.get("ocr_fallbacks") or 0),
        },
        sample_ids=tuple(sample_ids),
        notes=(
            "Region-level transcription, per-region confidences, and critical "
            "tokens are recorded by this build; this corpus does not annotate a "
            "reference transcription, so CER/WER, critical-token scoring and "
            "region completeness are not reported for it.",
        ),
    )


def _annotated_ocr_layer(
    mode: str,
    index_status: Mapping[str, Any],
    annotated: Sequence[ItemResult],
    observations: Mapping[str, Any],
) -> LayerResult:
    """Score annotated transcriptions, or say exactly why they could not be scored."""

    sample_ids = tuple(item.sample_id for item in annotated)
    cer: list[float | None] = []
    wer: list[float | None] = []
    expected_tokens: list[str] = []
    observed_tokens: list[str] = []
    expected_regions: list[str] = []
    observed_regions: list[str] = []
    scored: list[str] = []
    for item in annotated:
        expectation = item.sample.expected.transcription
        assert expectation is not None  # filtered by the caller
        texts = _observed_region_texts(item, observations)
        if not texts:
            continue
        scored.append(item.sample_id)
        observed = " ".join(texts)
        cer.append(character_error_rate(expectation.text, observed))
        wer.append(word_error_rate(expectation.text, observed))
        expected_tokens.extend(expectation.critical_tokens)
        observed_tokens.extend(texts)
        expected_regions.extend(expectation.regions)
        observed_regions.extend(texts)

    if not scored:
        return LayerResult(
            layer="ocr_transcription",
            mode=mode,
            status="unavailable",
            sample_ids=sample_ids,
            notes=(
                f"{len(annotated)} sample(s) annotate a reference transcription, "
                "but this run produced no OCR region for their images (no OCR "
                "engine was available, or the images never reached the OCR "
                "stage), so CER/WER, critical tokens and region completeness "
                "cannot be scored.",
            ),
        )

    transcribed_regions = int(index_status.get("ocr_regions") or 0)
    return LayerResult(
        layer="ocr_transcription",
        mode=mode,
        status="measured",
        metrics={
            "annotated_samples": len(annotated),
            "scored_samples": len(scored),
            "cer": mean_ignoring_none(cer),
            "wer": mean_ignoring_none(wer),
            "critical_token_coverage": coverage_by_contains(
                expected_tokens, observed_tokens
            ),
            "region_completeness": region_completeness(
                expected_regions, observed_regions
            ),
            "ocr_regions_indexed": transcribed_regions,
            "ocr_fallbacks": int(index_status.get("ocr_fallbacks") or 0),
            "ocr_machine_supported": int(
                index_status.get("ocr_machine_supported") or 0
            ),
        },
        sample_ids=tuple(scored),
        notes=(
            "CER/WER compare the annotated transcription with the recognised "
            "regions of the same images; critical tokens and annotated regions "
            "count as found when they appear inside a recognised region.",
        ),
    )


def _seed_review_actions(
    workspace: Path,
    corpora: Sequence[EvaluationCorpus],
    index: SharedIndexRead,
) -> list[dict[str, Any]]:
    """Replay the review decisions a corpus ships, before any sample runs.

    A corpus that annotates a *confirmed* notation meaning has to be able to
    hand the run the same confirmed dictionary a person would have. The seeds go
    through the ordinary plan/apply path, so the run's own journal records them
    and no sample can tell a seeded run from a reviewed one.
    """

    from ..review import apply_review_action, plan_review_action
    from ..state import DurableState

    seeds = [seed for corpus in corpora for seed in corpus.manifest.review_seeds]
    if not seeds:
        return []
    state = DurableState(workspace)
    applied: list[dict[str, Any]] = []
    for seed in seeds:
        intent = dict(seed)
        plan = plan_review_action(state, index, **intent)
        result = apply_review_action(
            state, index, plan_token=str(plan["plan_token"]), **intent
        )
        applied.append(
            {
                "action": str(intent.get("action") or ""),
                "notation_token": str(intent.get("notation_token") or ""),
                "status": str(result.get("status") or ""),
            }
        )
    return applied


def _layer_observations(
    index: SharedIndexRead, document_paths: Mapping[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """OCR and layout output, keyed by the corpus document that produced it.

    Annotated OCR, relation, and notation scoring all need the transcription and
    the relations the build actually produced. Reading them once per run keeps
    those layers honest, and keeps the per-sample execution path unaware of how
    a corpus lays its documents out on disk.
    """

    by_source = {
        os.path.normcase(str(Path(resolved))): path
        for path, resolved in document_paths.items()
    }
    observations: dict[str, list[dict[str, Any]]] = {
        path: [] for path in document_paths
    }
    try:
        rows = index.fetchall(
            """
            SELECT i.id AS image_id, d.path AS document_path
            FROM images AS i
            JOIN documents AS d ON d.id = i.document_id
            ORDER BY i.id
            """
        )
    except sqlite3.OperationalError:
        # An index without the V2 image tables simply observed nothing, which is
        # the same answer as an index whose images never reached OCR.
        return observations
    for row in rows:
        resolved = str(index.resolve_source_path(str(row["document_path"])))
        path = by_source.get(os.path.normcase(resolved))
        if path is None:
            continue
        image_id = int(row["image_id"])
        observations[path].append(
            {
                "image_id": image_id,
                "transcription": image_transcription(index, image_id),
                "layout": image_layout(index, image_id),
            }
        )
    return observations


def _observed_region_texts(
    item: ItemResult, observations: Mapping[str, Any]
) -> list[str]:
    """Every recognised region text this run produced for a sample's documents."""

    texts: list[str] = []
    for document in item.sample.documents:
        for image in observations.get(document.path) or ():
            transcription = image.get("transcription") if isinstance(image, Mapping) else None
            if not isinstance(transcription, Mapping):
                continue
            for region in transcription.get("regions") or ():
                if not isinstance(region, Mapping):
                    continue
                text = str(region.get("text") or "").strip()
                if text:
                    texts.append(text)
    return texts


def _layout_layers(
    mode: str,
    index_status: Mapping[str, Any],
    sample_ids: Sequence[str],
    *,
    items: Sequence[ItemResult] = (),
    observations: Mapping[str, Any] | None = None,
) -> list[LayerResult]:
    """Reading order and relations, reported as two layers that can fail apart.

    Both are counted from the index the build actually published, so a corpus
    whose images never reached the ruleset is reported as *unavailable* with the
    reason, instead of being folded into a passing number.
    """

    images = int(index_status.get("images_indexed") or 0)
    elements = int(index_status.get("layout_elements") or 0)
    annotated = [item for item in items if item.sample.expected.relations]
    if images == 0:
        reason = "This corpus contains no images, so no reading order was exercised."
    elif elements == 0:
        reason = (
            "No OCR region reached the layout ruleset, so this corpus exercises "
            "neither the reading order nor the arrow rules."
        )
        if annotated:
            reason += (
                f" {len(annotated)} sample(s) annotate relations, so relation "
                "precision/recall/F1 cannot be scored on this run."
            )
    else:
        reason = ""
    if reason:
        return [
            LayerResult(
                layer=layer, mode=mode, status="unavailable", notes=(reason,)
            )
            for layer in ("layout_regions", "reading_order_relations")
        ]
    return [
        LayerResult(
            layer="layout_regions",
            mode=mode,
            status="measured",
            metrics={
                "images_indexed": images,
                "layout_runs": int(index_status.get("layout_runs") or 0),
                "layout_images": int(index_status.get("layout_images") or 0),
                "layout_elements": elements,
                "layout_elements_per_image": round(elements / images, 4),
                "layout_uncertain_images": sum(
                    count
                    for code, count in (
                        index_status.get("layout_uncertainty") or {}
                    ).items()
                    if code
                ),
                "layout_rulesets": index_status.get("layout_rulesets") or {},
            },
            sample_ids=tuple(sample_ids),
            notes=(
                "Each element keeps its region, row, column, depth hint, and the "
                "geometry confidence of the order it sits in; per-image ordering "
                "accuracy needs a labelled corpus and lands with V2-12.",
            ),
        ),
        LayerResult(
            layer="reading_order_relations",
            mode=mode,
            status="measured",
            metrics=_relation_metrics(index_status, annotated, observations or {}),
            sample_ids=(
                tuple(item.sample_id for item in annotated)
                if annotated
                else tuple(sample_ids)
            ),
            notes=(
                "A confirmed next_step keeps its arrow, both endpoints, the "
                "geometry basis, and the geometry and OCR confidences apart; "
                "indentation never creates a relation.",
            ),
        ),
    ]


def _relation_metrics(
    index_status: Mapping[str, Any],
    annotated: Sequence[ItemResult],
    observations: Mapping[str, Any],
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "layout_relations": int(index_status.get("layout_relations") or 0),
        "layout_confirmed_relations": int(
            index_status.get("layout_confirmed_relations") or 0
        ),
        "layout_candidate_relations": int(
            index_status.get("layout_candidate_relations") or 0
        ),
        "layout_uncertain_relations": int(
            index_status.get("layout_uncertain_relations") or 0
        ),
        "relation_rulesets": index_status.get("relation_rulesets") or {},
    }
    if not annotated:
        return metrics
    expected_labels: list[str] = []
    observed_labels: list[str] = []
    for item in annotated:
        expected_labels.extend(
            expectation.label() for expectation in item.sample.expected.relations
        )
        observed_labels.extend(_observed_relation_labels(item, observations))
    metrics["annotated_relations"] = len(expected_labels)
    metrics["observed_relations"] = len(observed_labels)
    if observed_labels:
        metrics["annotated_relation_scores"] = prf1_by_contains(
            expected_labels, observed_labels
        )
    return metrics


def _observed_relation_labels(
    item: ItemResult, observations: Mapping[str, Any]
) -> list[str]:
    """Relation labels built from the region text each endpoint was read as."""

    labels: list[str] = []
    for document in item.sample.documents:
        for image in observations.get(document.path) or ():
            if not isinstance(image, Mapping):
                continue
            transcription = image.get("transcription") or {}
            region_text: dict[str, str] = {}
            for region in transcription.get("regions") or ():
                if isinstance(region, Mapping):
                    region_text[str(region.get("region_index"))] = str(
                        region.get("text") or ""
                    )
            layout = image.get("layout") or {}
            for relation in layout.get("relations") or ():
                if not isinstance(relation, Mapping):
                    continue
                source = region_text.get(str(relation.get("source_region")), "")
                target = region_text.get(str(relation.get("target_region")), "")
                if source or target:
                    labels.append(f"{source}->{target}")
    return labels


def _notation_layer(mode: str, items: Sequence[ItemResult]) -> LayerResult:
    """How often an annotated notation reading came back the way it was labelled."""

    annotated = [item for item in items if item.sample.expected.notation]
    if not annotated:
        return LayerResult(
            layer="notation_resolution",
            mode=mode,
            status="unavailable",
            notes=(
                "Designer notation is resolved (designer-notation-v1, confirmed "
                "entries only); this corpus annotates no notation token, so this "
                "layer is not measured.",
            ),
        )
    expectations = [
        (item, expectation)
        for item in annotated
        for expectation in item.sample.expected.notation
    ]
    satisfied = 0
    notes: list[str] = []
    for item, expectation in expectations:
        ok, why = _notation_satisfied(item.response, expectation)
        if ok:
            satisfied += 1
        else:
            notes.append(f"{item.sample_id}:{expectation.symbol} {why}")
    return LayerResult(
        layer="notation_resolution",
        mode=mode,
        status="measured",
        metrics={
            "annotated_tokens": len(expectations),
            "samples": len(annotated),
            "annotated_reading_matches": rate_with_interval(
                satisfied, len(expectations)
            ),
        },
        sample_ids=tuple(item.sample_id for item in annotated),
        notes=tuple(notes),
    )


def _notation_satisfied(
    response: Mapping[str, Any] | None, expectation: NotationExpectation
) -> tuple[bool, str]:
    """Whether one annotated notation reading came back as it was labelled.

    A confirmed entry has to resolve to the annotated meaning; a rejected
    reading may only be reported as decoration; an unknown symbol must stay
    unknown instead of being invented or quarantined into a fact.
    """

    if not isinstance(response, Mapping):
        return (False, "the token was never resolved")
    status = str(response.get("status") or "")
    resolved = response.get("resolved")
    meaning = (
        str(resolved.get("meaning") or "") if isinstance(resolved, Mapping) else ""
    )
    if expectation.status == "confirmed":
        if status != "resolved":
            return (False, f"expected a confirmed reading, got status {status!r}")
        if expectation.meaning and expectation.meaning not in meaning:
            return (False, f"expected meaning {expectation.meaning!r}, got {meaning!r}")
        return (True, "")
    if expectation.status == "rejected":
        if status == "resolved":
            return (False, "a rejected reading was served as a project fact")
        candidates = [
            candidate
            for candidate in (response.get("candidates") or [])
            if isinstance(candidate, Mapping)
        ]
        if not any(candidate.get("rejected") for candidate in candidates):
            return (False, "the rejected reading is not reported as decoration")
        return (True, "")
    if status not in {"not_found", "candidate_only"}:
        return (False, f"expected an unknown symbol, got status {status!r}")
    return (True, "")


def _explanation_layer(mode: str, items: Sequence[ItemResult]) -> LayerResult:
    """Atom coverage, citation resolvability, gaps, and unsupported atoms."""

    annotated = [item for item in items if item.sample.expected.required_atoms]
    if not annotated:
        return LayerResult(
            layer="explanation",
            mode=mode,
            status="unavailable",
            notes=(
                "Brief/Standard/Full explanations are served (explanation-v1); "
                "this corpus annotates no required atom, so this layer is not "
                "measured.",
            ),
        )
    expected_atoms: list[str] = []
    observed_atoms: list[str] = []
    resolvable = 0
    total_atoms = 0
    isolated = 0
    expected_gaps: list[str] = []
    observed_gaps: list[str] = []
    conflict_items = 0
    conflicts_preserved = 0
    for item in annotated:
        explanation = _explanation_payload(item.response)
        atoms = [
            atom
            for atom in (explanation.get("atoms") or [])
            if isinstance(atom, Mapping)
        ]
        total_atoms += len(atoms)
        resolvable += sum(1 for atom in atoms if _atom_citation_resolvable(atom))
        observed_atoms.extend(str(atom.get("text") or "") for atom in atoms)
        # The validator reports the atoms it had to isolate on the explanation
        # itself; nothing here may quietly drop them back into the count.
        isolated += len(
            [
                atom
                for atom in (explanation.get("isolated_atoms") or ())
                if isinstance(atom, Mapping)
            ]
        )
        expected_atoms.extend(item.sample.expected.required_atoms)
        expected_gaps.extend(item.sample.expected.required_gaps)
        observed_gaps.extend(_observed_gap_labels(explanation))
        if item.sample.expected.conflicts or item.sample.expected.conflict_group_expected:
            conflict_items += 1
            conflicts_preserved += 1 if _exposes_conflict(item) else 0
    return LayerResult(
        layer="explanation",
        mode=mode,
        status="measured",
        metrics={
            "annotated_samples": len(annotated),
            "required_atom_coverage": coverage_by_contains(
                expected_atoms, observed_atoms
            ),
            "citations_resolvable": rate_with_interval(resolvable, total_atoms),
            "unsupported_atom_rate": rate_with_interval(isolated, total_atoms),
            "gap_disclosure": (
                coverage_by_contains(expected_gaps, observed_gaps)
                if expected_gaps
                else None
            ),
            "conflict_preservation": (
                rate_with_interval(conflicts_preserved, conflict_items)
                if conflict_items
                else None
            ),
        },
        sample_ids=tuple(item.sample_id for item in annotated),
        notes=(
            "A required atom counts as covered when the response carries its "
            "text; a citation is resolvable when the atom keeps a revision-bound "
            "source reference and a locator; an atom the validator had to "
            "isolate is reported as an unsupported-atom rate.",
        ),
    )


def _explanation_payload(response: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    explanation = response.get("explanation")
    return explanation if isinstance(explanation, Mapping) else {}


def _atom_citation_resolvable(atom: Mapping[str, Any]) -> bool:
    reference = atom.get("source_reference")
    if not isinstance(reference, Mapping):
        return False
    if not str(reference.get("source_revision_id") or ""):
        return False
    if not str(reference.get("path") or ""):
        return False
    return bool(atom.get("locator"))


def _observed_gap_labels(explanation: Mapping[str, Any]) -> list[str]:
    """Both labels a disclosed gap can be annotated by: its sentence and its code.

    The payload lists gap atom ids and keeps the wording and the machine code on
    the atoms themselves, so an annotation may quote the sentence a person read
    or name the gap code the build used.
    """

    labels: list[str] = []
    for atom in explanation.get("atoms") or ():
        if not isinstance(atom, Mapping):
            continue
        roles = atom.get("roles")
        roles = roles if isinstance(roles, Mapping) else {}
        code = str(roles.get("gap_code") or "")
        if code or str(atom.get("section") or "") == "relevant_source_gaps":
            labels.append(str(atom.get("text") or ""))
            labels.append(code)
    return [label for label in labels if label]


def _statement_layer(mode: str, items: Sequence[ItemResult]) -> LayerResult:
    supported = [(item, _item_claims(item)) for item in items]
    claims = [claim for _, item_claims in supported for claim in item_claims]
    if not claims:
        return LayerResult(
            layer="statement_fidelity",
            mode=mode,
            status="unavailable",
            notes=("No sample returned a traceable claim.",),
        )
    traceable = sum(1 for claim in claims if claim_is_traceable(claim))
    return LayerResult(
        layer="statement_fidelity",
        mode=mode,
        status="measured",
        metrics={
            "claims": len(claims),
            "traceable_claims": traceable,
            "evidence_support_rate": traceable / len(claims),
        },
        sample_ids=tuple(
            item.sample_id for item, item_claims in supported if item_claims
        ),
    )


def _retrieval_layer(mode: str, items: Sequence[ItemResult]) -> LayerResult:
    scored = [item for item in items if item.sample.expected.required_evidence]
    no_answer = [
        item for item in items if item.sample.expected.response_state == "not_found"
    ]
    if not scored and not no_answer:
        return LayerResult(
            layer="retrieval",
            mode=mode,
            status="unavailable",
            notes=("No sample annotates required evidence or a no-answer case.",),
        )

    recalls: list[float | None] = []
    recalls_1: list[float | None] = []
    recalls_5: list[float | None] = []
    precisions: list[float | None] = []
    ranks: list[float] = []
    locator_hits = 0
    locator_total = 0
    for item in scored:
        claims = _item_claims(item)
        expectations = item.sample.expected.required_evidence
        locator_hits += sum(
            1 for expectation in expectations if _matches_expectation(claims, expectation)
        )
        locator_total += len(expectations)

        limit = int(item.sample.arguments.get("limit") or 20)
        ranked = [_claim_rank_key(claim) for claim in claims]
        wanted = [
            expectation.text or expectation.source_document
            for expectation in expectations
        ]
        recalls.append(recall_at_k(ranked, wanted, limit))
        recalls_1.append(recall_at_k(ranked, wanted, 1))
        recalls_5.append(recall_at_k(ranked, wanted, 5))
        precisions.append(precision_at_k(ranked, wanted, limit))
        ranks.append(reciprocal_rank(ranked, wanted))

    false_positives = sum(1 for item in no_answer if item.observed_state in FOUND_FAMILY)
    answerable = [
        item for item in items if item.sample.expected.response_state == "found"
    ]
    false_negatives = sum(
        1 for item in answerable if item.observed_state not in FOUND_FAMILY
    )
    return LayerResult(
        layer="retrieval",
        mode=mode,
        status="measured",
        metrics={
            "samples_scored": len(scored),
            "recall_at_limit": mean_ignoring_none(recalls),
            "recall_at_1": mean_ignoring_none(recalls_1),
            "recall_at_5": mean_ignoring_none(recalls_5),
            "precision_at_limit": mean_ignoring_none(precisions),
            "mrr": mean_ignoring_none(ranks),
            "annotated_reference_hit_rate": (
                locator_hits / locator_total if locator_total else None
            ),
            "no_answer_samples": len(no_answer),
            "no_answer_false_positives": rate_with_interval(
                false_positives, len(no_answer)
            ),
            "answerable_samples": len(answerable),
            "no_answer_false_negatives": rate_with_interval(
                false_negatives, len(answerable)
            ),
        },
        sample_ids=tuple(item.sample_id for item in scored + no_answer),
    )


def _conflict_layer(mode: str, items: Sequence[ItemResult]) -> LayerResult:
    conflict_items = [
        item
        for item in items
        if item.sample.expected.conflict_group_expected
        or item.sample.stratum.values["conflict_state"] == "present"
    ]
    if not conflict_items:
        return LayerResult(
            layer="conflict_and_response_state",
            mode=mode,
            status="unavailable",
            notes=("No sample is annotated as a conflict case.",),
        )
    preserved = sum(1 for item in conflict_items if _exposes_conflict(item))
    return LayerResult(
        layer="conflict_and_response_state",
        mode=mode,
        status="measured",
        metrics={
            "conflict_samples": len(conflict_items),
            "conflicts_preserved": preserved,
            "conflict_preservation_rate": preserved / len(conflict_items),
        },
        sample_ids=tuple(item.sample_id for item in conflict_items),
    )


def _performance_layer(
    mode: str,
    items: Sequence[ItemResult],
    *,
    build_seconds: float,
    peak_memory_bytes: int,
    disk_bytes: int,
    recorded_degradation: Mapping[str, int],
    sample_ids: Sequence[str],
) -> LayerResult:
    latencies = sorted(item.latency_seconds for item in items)
    return LayerResult(
        layer="performance_and_degradation",
        mode=mode,
        status="measured",
        metrics={
            "calls": len(latencies),
            "latency_p50_seconds": _percentile(latencies, 50),
            "latency_p95_seconds": _percentile(latencies, 95),
            "index_build_seconds": round(build_seconds, 4),
            "peak_python_memory_bytes": peak_memory_bytes,
            "workspace_bytes": disk_bytes,
            "degradation_events": sum(
                1 for value in recorded_degradation.values() if value > 0
            ),
        },
        sample_ids=tuple(sample_ids),
    )


def _capability_results(items: Sequence[ItemResult]) -> tuple[CapabilityResult, ...]:
    """One result per declared capability pack, never merged with another.

    A sample can declare more than one pack, and it is counted under each of
    them: a pack that depends on a capability the run could not exercise has to
    show its own response states instead of borrowing a green one.
    """

    buckets: dict[str, dict[str, Any]] = {}
    for item in items:
        for capability in item.sample.capabilities:
            bucket = buckets.setdefault(
                capability, {"modes": set(), "states": [], "samples": set()}
            )
            bucket["modes"].add(item.mode)
            bucket["states"].append((item.expected_state, item.observed_state))
            bucket["samples"].add(item.sample_id)
    return tuple(
        CapabilityResult(
            capability=capability,
            sample_count=len(bucket["samples"]),
            modes=tuple(sorted(bucket["modes"])),
            response_states=response_state_confusion(bucket["states"]),
        )
        for capability, bucket in sorted(buckets.items())
    )


def _stratum_results(items: Sequence[ItemResult]) -> tuple[StratumResult, ...]:
    bucket_states: dict[str, list[tuple[str, str]]] = {}
    bucket_strata: dict[str, dict[str, str]] = {}
    bucket_samples: dict[str, set[str]] = {}
    for item in items:
        label = item.sample.stratum.label(*STRATUM_GROUP_KEYS)
        bucket_strata.setdefault(label, item.sample.stratum.as_payload())
        bucket_samples.setdefault(label, set()).add(item.sample_id)
        bucket_states.setdefault(label, []).append(
            (item.expected_state, item.observed_state)
        )

    results = []
    for label in sorted(bucket_states):
        matrix = response_state_confusion(bucket_states[label])
        results.append(
            StratumResult(
                label=label,
                strata=bucket_strata[label],
                sample_count=len(bucket_samples[label]),
                response_states=matrix,
                matrices={"state_precision_recall": state_precision_recall(matrix)},
            )
        )
    return tuple(results)


def _write_artifacts(run: EvaluationRun, runs_directory: Path) -> dict[str, Path]:
    run_directory = runs_directory / run.run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, Path] = {}

    payload_path = run_directory / "run.json"
    payload_path.write_text(
        json.dumps(run.as_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    artifacts["run"] = payload_path

    item_path = run_directory / "item_results.json"
    item_path.write_text(
        json.dumps(
            {"run_id": run.run_id, "items": [item.as_payload() for item in run.items]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    artifacts["items"] = item_path

    report_path = run_directory / "report.md"
    report_path.write_text(
        render_markdown(
            run_id=run.run_id,
            corpus_descriptions=run.corpus_descriptions,
            layer_results=run.layer_results,
            stratum_results=run.stratum_results,
            response_states=run.response_states,
            invariant_payloads=[
                result.as_payload() for result in run.invariant_results
            ],
            environment=dict(run.manifest.environment),
            extra_notes=run.failure_summary() or run.notes,
            capability_results=run.capability_results,
            hardware_profile=str(
                run.manifest.environment.get("hardware_profile") or ""
            ),
        ),
        encoding="utf-8",
    )
    artifacts["report"] = report_path
    return artifacts


def _item_claims(item: ItemResult) -> list[Mapping[str, Any]]:
    response = item.response
    if not isinstance(response, Mapping):
        return []
    return claims_for(item.tool, response, statuses=FOUND_FAMILY)


def _claim_rank_key(claim: Mapping[str, Any]) -> str:
    for key in ("text", "display_text", "raw_value", "ocr_text", "context_text"):
        value = claim.get(key)
        if value:
            return str(value)
    return str(claim.get("source_document") or "")


def _claim_position_map(claim: Mapping[str, Any]) -> dict[str, Any]:
    position: dict[str, Any] = {}
    locator = claim.get("locator")
    if isinstance(locator, Mapping):
        position.update(locator)
    for key in ("sheet_name", "cell_reference", "region_id", "workbook"):
        if claim.get(key) is not None:
            position[key] = claim[key]
    if claim.get("paragraph_index") is not None:
        position["paragraph_index"] = claim["paragraph_index"]
    return position


def _matches_expectation(
    claims: Sequence[Mapping[str, Any]], expectation: EvidenceExpectation
) -> bool:
    """Match a claim against an annotated reference.

    A bare file name in the annotation matches any document with that name; a
    reference that also names a directory must match the whole path, because
    two workbooks can share a name and the corpus fingerprint already tells
    them apart.
    """

    expected = Path(expectation.source_document)
    for claim in claims:
        source = str(claim.get("source_document") or "")
        if not source:
            continue
        found = Path(source)
        if len(expected.parts) > 1:
            if found.as_posix() != expected.as_posix():
                continue
        elif found.name != expected.name:
            continue
        position = _claim_position_map(claim)
        if all(
            position.get(key) == value
            for key, value in expectation.locator_contains.items()
        ):
            return True
    return False


def _exposes_conflict(item: ItemResult) -> bool:
    response = item.response
    if not isinstance(response, Mapping):
        return False
    conflicts = response.get("conflicts") or response.get("conflict_groups")
    return isinstance(conflicts, list) and bool(conflicts)


def _hash_tree(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    if not root.exists():
        return digests
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digests[str(path)] = digest.hexdigest()
    return digests


def _directory_size(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _percentile(values: Sequence[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(percentile / 100 * len(ordered)) - 1))
    return round(ordered[index], 6)


def _environment_record(
    hardware_profile: str, cold_start: bool
) -> EnvironmentRecord:
    return EnvironmentRecord(
        platform=platform.platform(),
        python_version=sys.version.split()[0],
        cpu_count=os.cpu_count() or 1,
        memory_bytes=_system_memory_bytes(),
        hardware_profile=hardware_profile,
        cold_start=cold_start,
    )


def _system_memory_bytes() -> int | None:
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
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

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        global_memory_status = getattr(
            getattr(ctypes, "windll", None), "kernel32", None
        )
        if global_memory_status is not None and global_memory_status.GlobalMemoryStatusEx(
            ctypes.byref(status)
        ):
            return int(status.ullTotalPhys)
    except Exception:  # noqa: BLE001 - memory reporting is optional
        return None
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_run_id(corpora: Sequence[EvaluationCorpus], started_at: str) -> str:
    stamp = started_at.replace(":", "").replace("-", "").split(".")[0]
    digest = canonical_fingerprint([corpus.describe() for corpus in corpora])[:8]
    return f"{stamp}-{digest}"
