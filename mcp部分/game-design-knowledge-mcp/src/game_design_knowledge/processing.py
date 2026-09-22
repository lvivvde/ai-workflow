"""The staged processing pipeline: definitions, attempts, cache, and status.

A published parse revision says *which* source bytes produced it. This module
adds the other half of that sentence: which stages ran, with which handler and
engine, on which inputs, and what each one decided. Stages are independently
retryable; every retry appends a new immutable attempt instead of rewriting
history, and a cache entry is only reused when the whole stage fingerprint
still matches.

Two fingerprints exist on purpose and are not interchangeable:

* the *configured* pipeline fingerprint (``ProcessingManifest``) binds a parse
  revision to the rules, handlers, and versions that were configured;
* a *stage* fingerprint binds one cache entry to one input, its upstream
  outputs, its runtime identity, and its effective configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from .revisions import (
    ProcessingManifest,
    ProcessingStage,
    cache_key,
    normalize_relative_path,
)


EXECUTION_STATUSES = ("succeeded", "partial", "failed", "unavailable", "skipped")
QUALITY_STATUSES = ("accepted", "uncertain", "rejected")
CACHEABLE_EXECUTION_STATUSES = ("succeeded", "partial")
ATTEMPT_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1


class ProcessingError(RuntimeError):
    """Raised when a stage cannot be described or recorded safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class StageDefinition:
    """One declared stage: what it needs, what it produces, who owns it."""

    name: str
    order: int
    tier: str
    owner: str
    description: str
    upstream: tuple[str, ...] = ()
    capability_pack: str = ""
    required: bool = True
    per_document: bool = True
    cacheable: bool = True
    handler: str = ""
    handler_version: str = "v1"
    ruleset_version: str = "v1"
    output_schema_version: str = "v1"

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "order": self.order,
            "tier": self.tier,
            "owner": self.owner,
            "description": self.description,
            "upstream": list(self.upstream),
            "capability_pack": self.capability_pack,
            "required": self.required,
            "per_document": self.per_document,
            "cacheable": self.cacheable,
            "handler": self.handler,
            "handler_version": self.handler_version,
            "ruleset_version": self.ruleset_version,
            "output_schema_version": self.output_schema_version,
        }


DEFAULT_PIPELINE: tuple[StageDefinition, ...] = (
    StageDefinition(
        name="source_parse",
        order=1,
        tier="core",
        owner="V2-03",
        description=(
            "Normalize source identity: project-relative path, content hash, "
            "document type, and byte size for one document."
        ),
        capability_pack="core",
        handler="pipeline.source_parse",
        ruleset_version="source-identity-v1",
    ),
    StageDefinition(
        name="ocr",
        order=2,
        tier="core",
        owner="V2-04",
        description=(
            "Select the OCR engine through the documented degradation chain and "
            "record which engine was asked for and which one answers."
        ),
        upstream=("source_parse",),
        capability_pack="core",
        required=False,
        handler="pipeline.select_ocr",
        # v2: the OCR stage now delivers regions, separated confidences, the
        # quality gate, and the normalization suggestion beside the raw text.
        ruleset_version="ocr-regions-v1",
        output_schema_version="ocr-regions-v1",
    ),
    StageDefinition(
        name="layout",
        order=3,
        tier="enhanced",
        owner="V2-05",
        description="Visual elements, reading order, and region association.",
        upstream=("ocr",),
        capability_pack="enhanced_ocr",
        required=False,
        handler="pipeline.layout",
        ruleset_version="layout-v1",
    ),
    StageDefinition(
        name="structure_relations",
        order=4,
        tier="core",
        owner="V2-05",
        description="Conservative structural relations such as vertical arrows.",
        upstream=("layout",),
        capability_pack="core",
        required=False,
        handler="pipeline.structure_relations",
        ruleset_version="relations-v1",
    ),
    StageDefinition(
        name="notation",
        order=5,
        tier="core",
        owner="V2-07",
        description="Designer notation occurrences and dictionary resolution.",
        upstream=("structure_relations",),
        capability_pack="core",
        required=False,
        handler="pipeline.notation",
        ruleset_version="notation-v1",
    ),
    StageDefinition(
        name="statements",
        order=6,
        tier="core",
        owner="V2-08",
        description="Atomic statements and their uncertainties.",
        upstream=("notation",),
        capability_pack="core",
        required=False,
        handler="pipeline.statements",
        ruleset_version="statements-v1",
    ),
    StageDefinition(
        name="explanation_cache",
        order=7,
        tier="optional",
        owner="V2-08",
        description="Deterministic Brief/Standard/Full explanations, cached by policy.",
        upstream=("statements",),
        capability_pack="",
        required=False,
        per_document=False,
        handler="pipeline.explanation_cache",
        ruleset_version="explanation-v1",
    ),
    # The projection publishes the snapshot and the run manifest travels inside
    # it, so nothing may run after this stage: its attempt would never reach the
    # index it is supposed to explain.
    StageDefinition(
        name="retrieval_projection",
        order=8,
        tier="core",
        owner="V2-03",
        description=(
            "Publish the derived index: parse every document with the selected "
            "engines and swap in a validated immutable snapshot."
        ),
        upstream=("source_parse", "ocr"),
        capability_pack="core",
        per_document=False,
        handler="index_build.build_index_atomically",
        ruleset_version="projection-v1",
    ),
)

STAGE_BY_NAME = {stage.name: stage for stage in DEFAULT_PIPELINE}
PIPELINE_STAGE_NAMES = tuple(stage.name for stage in DEFAULT_PIPELINE)


def configured_manifest(
    pipeline: Sequence[StageDefinition] = DEFAULT_PIPELINE,
) -> ProcessingManifest:
    """The *configured* pipeline identity that a parse revision binds to.

    This is the single source of truth for "which rules and handlers were
    configured". Every entry point that writes a parse revision -- the
    pipeline, the indexing entry points, and the freshness report that checks
    them -- has to use the same manifest, or a healthy index reads as stale.
    Per-run engines, fallbacks, and statuses are recorded separately in the
    run manifest.
    """

    return ProcessingManifest(
        stages=tuple(
            ProcessingStage(
                name=stage.name,
                ruleset_version=stage.ruleset_version,
                output_schema_version=stage.output_schema_version,
                handler=stage.handler,
                handler_version=stage.handler_version,
            )
            for stage in pipeline
        ),
        notes=(
            "Configured pipeline identity. Per-run engines, fallbacks, and statuses "
            "are recorded separately in the run manifest.",
        ),
    )


def downstream_stages(name: str, pipeline: Sequence[StageDefinition] = DEFAULT_PIPELINE) -> tuple[str, ...]:
    """Every stage that transitively consumes this stage's output."""

    if name not in {stage.name for stage in pipeline}:
        raise ProcessingError(f"unknown stage: {name}")
    affected = {name}
    changed = True
    while changed:
        changed = False
        for stage in pipeline:
            if stage.name in affected:
                continue
            if affected.intersection(stage.upstream):
                affected.add(stage.name)
                changed = True
    return tuple(
        stage.name for stage in sorted(pipeline, key=lambda item: item.order)
        if stage.name in affected
    )


def stage_fingerprint(
    stage: StageDefinition,
    *,
    input_sha256: str,
    upstream_sha256: Iterable[str] = (),
    runtime_identity: str = "",
    runtime_version: str = "",
    config: Any = None,
) -> str:
    """One stage's cache key, per the protocol's cache-key contract."""

    return cache_key(
        stage=stage.name,
        ruleset_version=stage.ruleset_version,
        input_sha256=input_sha256,
        upstream_sha256=tuple(upstream_sha256),
        runtime_identity=runtime_identity,
        runtime_version=runtime_version,
        config=config,
        output_schema_version=stage.output_schema_version,
    )


@dataclass(frozen=True)
class StageOutput:
    """What one stage run produced, in the terms the protocol asks for."""

    payload: Mapping[str, Any] = field(default_factory=dict)
    execution_status: str = "succeeded"
    quality_status: str = "accepted"
    reason_code: str = ""
    detail: str = ""
    fallback_used: bool = False
    reason_chain: tuple[dict[str, Any], ...] = ()
    engine: str = ""
    engine_version: str = ""
    model: str = ""
    model_version: str = ""
    coverage: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = True

    def validate(self) -> None:
        if self.execution_status not in EXECUTION_STATUSES:
            raise ProcessingError(f"unknown execution status: {self.execution_status}")
        if self.quality_status not in QUALITY_STATUSES:
            raise ProcessingError(f"unknown quality status: {self.quality_status}")


@dataclass(frozen=True)
class StageContext:
    """Everything a stage handler is allowed to look at."""

    project_root: Path
    index_directory: Path
    run_id: str
    document_path: str
    definition: StageDefinition
    config: Mapping[str, Any] = field(default_factory=dict)
    runtime: Any = None
    cache: "StageCache | None" = None
    upstream: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    prior_attempts: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class StageAttempt:
    """One immutable record of one stage execution."""

    attempt_id: str
    run_id: str
    stage: str
    attempt_number: int
    execution_status: str
    quality_status: str
    fingerprint: str
    input_sha256: str
    output_sha256: str
    document_path: str
    owner_ticket: str
    started_at: str
    finished_at: str
    duration_ms: int
    cache_hit: bool = False
    fallback_used: bool = False
    reason_code: str = ""
    detail: str = ""
    engine: str = ""
    engine_version: str = ""
    model: str = ""
    model_version: str = ""
    coverage: Mapping[str, Any] = field(default_factory=dict)
    reason_chain: tuple[dict[str, Any], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "attempt_number": self.attempt_number,
            "execution_status": self.execution_status,
            "quality_status": self.quality_status,
            "fingerprint": self.fingerprint,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "document_path": self.document_path,
            "owner_ticket": self.owner_ticket,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "cache_hit": self.cache_hit,
            "fallback_used": self.fallback_used,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "model": self.model,
            "model_version": self.model_version,
            "coverage": dict(self.coverage),
            "reason_chain": [dict(entry) for entry in self.reason_chain],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StageAttempt":
        return cls(
            attempt_id=str(payload["attempt_id"]),
            run_id=str(payload.get("run_id") or ""),
            stage=str(payload["stage"]),
            attempt_number=int(payload.get("attempt_number") or 1),
            execution_status=str(payload.get("execution_status") or "succeeded"),
            quality_status=str(payload.get("quality_status") or "accepted"),
            fingerprint=str(payload.get("fingerprint") or ""),
            input_sha256=str(payload.get("input_sha256") or ""),
            output_sha256=str(payload.get("output_sha256") or ""),
            document_path=str(payload.get("document_path") or ""),
            owner_ticket=str(payload.get("owner_ticket") or ""),
            started_at=str(payload.get("started_at") or ""),
            finished_at=str(payload.get("finished_at") or ""),
            duration_ms=int(payload.get("duration_ms") or 0),
            cache_hit=bool(payload.get("cache_hit", False)),
            fallback_used=bool(payload.get("fallback_used", False)),
            reason_code=str(payload.get("reason_code") or ""),
            detail=str(payload.get("detail") or ""),
            engine=str(payload.get("engine") or ""),
            engine_version=str(payload.get("engine_version") or ""),
            model=str(payload.get("model") or ""),
            model_version=str(payload.get("model_version") or ""),
            coverage=dict(payload.get("coverage") or {}),
            reason_chain=tuple(
                dict(entry) for entry in payload.get("reason_chain") or ()
            ),
        )


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stores: int = 0
    bypassed: int = 0
    corrupt: int = 0

    def as_payload(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "bypassed": self.bypassed,
            "corrupt": self.corrupt,
        }


class StageCache:
    """A disposable, project-local, content-addressed stage cache.

    A hit requires the whole fingerprint to match, and the stored payload is
    re-hashed before it is handed back, so a half-written or edited entry is
    reported as corrupt instead of being reused as if it were valid.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self.stats = CacheStats()

    def entry_path(self, stage: str, fingerprint: str) -> Path:
        return self.root / "stages" / stage / f"{fingerprint}.json"

    def get(self, stage: str, fingerprint: str) -> dict[str, Any] | None:
        if not self.enabled:
            self.stats.bypassed += 1
            return None
        path = self.entry_path(stage, fingerprint)
        if not path.is_file():
            self.stats.misses += 1
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.stats.corrupt += 1
            return None
        if not isinstance(entry, Mapping) or entry.get("fingerprint") != fingerprint:
            self.stats.corrupt += 1
            return None
        payload = entry.get("payload")
        if _payload_sha256(payload) != entry.get("payload_sha256"):
            self.stats.corrupt += 1
            return None
        self.stats.hits += 1
        return dict(entry)

    def put(self, stage: str, fingerprint: str, output: StageOutput) -> str:
        payload = json.loads(json.dumps(dict(output.payload), ensure_ascii=False))
        digest = _payload_sha256(payload)
        if not self.enabled:
            return digest
        path = self.entry_path(stage, fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload_sha256": digest,
            "execution_status": output.execution_status,
            "quality_status": output.quality_status,
            "reason_code": output.reason_code,
            "fallback_used": output.fallback_used,
            "engine": output.engine,
            "engine_version": output.engine_version,
            "model": output.model,
            "model_version": output.model_version,
            "payload": payload,
            "written_at": _now(),
        }
        _atomic_write_json(path, entry)
        self.stats.stores += 1
        return digest

    def dispose(self) -> dict[str, Any]:
        """Delete the whole cache; only recomputation cost is lost."""

        existed = self.root.is_dir()
        if existed:
            shutil.rmtree(self.root)
        return {"status": "disposed" if existed else "absent", "cache_root": str(self.root)}

    def describe(self) -> dict[str, Any]:
        return {
            "cache_root": str(self.root),
            "enabled": self.enabled,
            "entries": sum(1 for _ in self.root.rglob("*.json")) if self.root.is_dir() else 0,
            "stats": self.stats.as_payload(),
        }


def default_cache_root(index_directory: Path) -> Path:
    """Where the disposable stage cache for one index lives.

    Beside the index directory, never inside it. The published index keeps
    exactly the shape its readers expect, a first build that fails leaves no
    half-created index behind, and the cache sits at the same depth as the
    snapshots it is rebuilt from.
    """

    index_directory = Path(index_directory).resolve()
    return index_directory.parent / f".{index_directory.name}.cache"


def execute_stage(
    definition: StageDefinition,
    handler: Callable[[StageContext], StageOutput],
    *,
    run: "ProcessingRun",
    document_path: str,
    input_sha256: str,
    upstream: Mapping[str, Mapping[str, Any]] | None = None,
    upstream_sha256: Sequence[str] = (),
    runtime_identity: str = "",
    runtime_version: str = "",
    config: Mapping[str, Any] | None = None,
    handler_config: Mapping[str, Any] | None = None,
) -> tuple[StageAttempt, StageOutput]:
    """Run one stage once, reusing a matching cache entry when it exists.

    ``handler_config`` carries live objects a handler needs but which must not
    enter the cache key, because the fingerprint already records them in a
    stable form (``runtime_identity`` and ``runtime_version``). Only
    ``config``'s JSON-safe view is hashed.
    """

    upstream = upstream or {}
    config = config or {}
    fingerprint = stage_fingerprint(
        definition,
        input_sha256=input_sha256,
        upstream_sha256=upstream_sha256,
        runtime_identity=runtime_identity,
        runtime_version=runtime_version,
        config=config,
    )
    started_at = _now()
    started = time.monotonic()
    cache = run.cache
    cached = (
        cache.get(definition.name, fingerprint)
        if cache is not None and definition.cacheable
        else None
    )
    if cached is not None:
        output = StageOutput(
            payload=dict(cached.get("payload") or {}),
            execution_status=str(cached.get("execution_status") or "succeeded"),
            quality_status=str(cached.get("quality_status") or "accepted"),
            reason_code=str(cached.get("reason_code") or ""),
            fallback_used=bool(cached.get("fallback_used", False)),
            engine=str(cached.get("engine") or ""),
            engine_version=str(cached.get("engine_version") or ""),
            model=str(cached.get("model") or ""),
            model_version=str(cached.get("model_version") or ""),
        )
        attempt = run.next_attempt(
            definition,
            document_path=document_path,
            execution_status=output.execution_status,
            quality_status=output.quality_status,
            fingerprint=fingerprint,
            input_sha256=input_sha256,
            output_sha256=str(cached.get("payload_sha256") or ""),
            started_at=started_at,
            duration_ms=_elapsed_ms(started),
            cache_hit=True,
            reason_code=output.reason_code,
            detail="reused a matching cache entry",
            engine=output.engine,
            engine_version=output.engine_version,
            model=output.model,
            model_version=output.model_version,
        )
        run.add(attempt)
        return attempt, output

    context = StageContext(
        project_root=run.project_root,
        index_directory=run.index_directory,
        run_id=run.run_id,
        document_path=document_path,
        definition=definition,
        config={**config, **dict(handler_config or {})},
        runtime=run.runtime,
        cache=cache,
        upstream=upstream,
        prior_attempts=tuple(
            attempt.as_payload()
            for attempt in run.history.get(definition.name, ())[-3:]
        ),
    )
    try:
        output = handler(context)
    except Exception as error:  # a stage failure is data, not an abort
        run.record_error(definition.name, document_path, error)
        output = StageOutput(
            payload={"error": f"{type(error).__name__}: {error}"},
            execution_status="failed",
            quality_status="rejected",
            reason_code="handler_error",
            detail=f"{type(error).__name__}: {error}",
        )
    output.validate()
    digest = _payload_sha256(dict(output.payload))
    if cache is not None and definition.cacheable and output.execution_status in CACHEABLE_EXECUTION_STATUSES:
        digest = cache.put(definition.name, fingerprint, output)
    elif cache is not None and definition.cacheable:
        cache.stats.bypassed += 1

    attempt = run.next_attempt(
        definition,
        document_path=document_path,
        execution_status=output.execution_status,
        quality_status=output.quality_status,
        fingerprint=fingerprint,
        input_sha256=input_sha256,
        output_sha256=digest,
        started_at=started_at,
        duration_ms=_elapsed_ms(started),
        fallback_used=output.fallback_used,
        reason_code=output.reason_code,
        detail=output.detail,
        engine=output.engine,
        engine_version=output.engine_version,
        model=output.model,
        model_version=output.model_version,
        coverage=output.coverage,
        reason_chain=output.reason_chain,
    )
    run.add(attempt)
    return attempt, output


@dataclass
class ProcessingRun:
    """One run of the pipeline, with the record every stage leaves behind."""

    project_root: Path
    index_directory: Path
    configured_manifest: ProcessingManifest
    run_id: str = field(default_factory=lambda: new_run_id())
    profile: str = "baseline"
    limits: Mapping[str, Any] = field(default_factory=dict)
    capability: Mapping[str, Any] = field(default_factory=dict)
    runtime: Any = None
    cache: StageCache | None = None
    started_at: str = field(default_factory=_now)
    attempts: list[StageAttempt] = field(default_factory=list)
    history: dict[str, list[StageAttempt]] = field(default_factory=dict)
    reused: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    projection_started_at: str | None = None
    projection_started_clock: float | None = None
    projection_input_sha256: str = ""
    projection_config: Mapping[str, Any] = field(default_factory=dict)
    projection: StageAttempt | None = None
    projection_report: dict[str, Any] = field(default_factory=dict)
    errors: dict[tuple[str, str], BaseException] = field(default_factory=dict)

    # -- recording ---------------------------------------------------------

    def add(self, attempt: StageAttempt) -> None:
        self.attempts.append(attempt)
        self.history.setdefault(attempt.stage, []).append(attempt)

    def record_error(
        self, stage: str, document_path: str, error: BaseException
    ) -> None:
        """Keep the exception a stage swallowed so a caller can re-raise it.

        A stage failure is data inside the run, but a caller that has to abort
        -- a write tool rolling back a move, or a CLI exiting non-zero -- still
        deserves the original exception rather than only its message.
        """

        self.errors[(stage, document_path)] = error

    def error_for(self, attempt: StageAttempt) -> BaseException | None:
        return self.errors.get((attempt.stage, attempt.document_path))

    def reuse(self, stage: str, attempt_id: str) -> None:
        """Carry a previous attempt forward without pretending it ran again."""

        self.reused[stage] = attempt_id

    def next_attempt(
        self,
        definition: StageDefinition,
        *,
        document_path: str,
        execution_status: str,
        quality_status: str,
        fingerprint: str,
        input_sha256: str,
        output_sha256: str,
        started_at: str,
        duration_ms: int,
        cache_hit: bool = False,
        fallback_used: bool = False,
        reason_code: str = "",
        detail: str = "",
        engine: str = "",
        engine_version: str = "",
        model: str = "",
        model_version: str = "",
        coverage: Mapping[str, Any] | None = None,
        reason_chain: Sequence[Mapping[str, Any]] = (),
    ) -> StageAttempt:
        number = len(self.history.get(definition.name, ())) + 1
        return StageAttempt(
            attempt_id=attempt_id(self.run_id, definition.name, document_path, number),
            run_id=self.run_id,
            stage=definition.name,
            attempt_number=number,
            execution_status=execution_status,
            quality_status=quality_status,
            fingerprint=fingerprint,
            input_sha256=input_sha256,
            output_sha256=output_sha256,
            document_path=document_path,
            owner_ticket=definition.owner,
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            cache_hit=cache_hit,
            fallback_used=fallback_used,
            reason_code=reason_code,
            detail=detail,
            engine=engine,
            engine_version=engine_version,
            model=model,
            model_version=model_version,
            coverage=dict(coverage or {}),
            reason_chain=tuple(dict(entry) for entry in reason_chain),
        )

    def begin_projection(
        self, *, input_sha256: str, config: Mapping[str, Any] | None = None
    ) -> None:
        self.projection_started_at = _now()
        self.projection_started_clock = time.monotonic()
        self.projection_input_sha256 = input_sha256
        self.projection_config = dict(config or {})

    def record_projection(self, report: Mapping[str, Any]) -> StageAttempt:
        """Record the projection attempt; called inside the snapshot build."""

        if self.projection is not None:
            return self.projection
        definition = STAGE_BY_NAME["retrieval_projection"]
        digest = _payload_sha256(dict(report))
        started_clock = self.projection_started_clock
        attempt = self.next_attempt(
            definition,
            document_path="",
            execution_status="succeeded",
            quality_status="accepted",
            fingerprint=stage_fingerprint(
                definition,
                input_sha256=self.projection_input_sha256,
                upstream_sha256=self.upstream_hashes(definition),
                runtime_identity="indexer",
                runtime_version=str(self.projection_config.get("schema_version") or ""),
                config=self.projection_config,
            ),
            input_sha256=self.projection_input_sha256,
            output_sha256=digest,
            started_at=self.projection_started_at or _now(),
            duration_ms=(
                int((time.monotonic() - started_clock) * 1000)
                if started_clock is not None
                else 0
            ),
            detail="published a validated index snapshot",
            coverage={"documents": int(report.get("documents_indexed", 0))},
        )
        self.projection = attempt
        # The report is the indexer's own payload: its counters stay numbers, and
        # any structured detail a later stage adds is carried through unchanged
        # rather than being flattened into an int.
        self.projection_report = {str(key): value for key, value in report.items()}
        self.add(attempt)
        return attempt

    # -- projections -------------------------------------------------------

    def upstream_hashes(self, definition: StageDefinition) -> list[str]:
        """The newest output hash of every direct upstream stage."""

        hashes: list[str] = []
        for name in definition.upstream:
            attempts = self.history.get(name) or ()
            if attempts:
                hashes.append(attempts[-1].output_sha256)
        return hashes

    def stage_rows(self) -> list[dict[str, Any]]:
        return [attempt.as_payload() for attempt in self.attempts]

    def blocking_failures(self) -> list[StageAttempt]:
        """Attempts in *required* stages that did not complete.

        A missing optional capability is expected and reported as
        ``unavailable`` while core processing continues. A required stage that
        fails or cannot run is different: nothing downstream of it is
        trustworthy, so callers must surface it instead of publishing quietly.
        """

        blocked: list[StageAttempt] = []
        for attempt in self.attempts:
            if attempt.execution_status not in ("failed", "unavailable"):
                continue
            definition = STAGE_BY_NAME.get(attempt.stage)
            if definition is not None and definition.required:
                blocked.append(attempt)
        return blocked

    def raise_for_blocking_failures(self) -> None:
        blocked = self.blocking_failures()
        if not blocked:
            return
        details = "; ".join(
            f"{attempt.stage}"
            + (f" ({attempt.document_path})" if attempt.document_path else "")
            + f": {attempt.detail or attempt.reason_code}"
            for attempt in blocked
        )
        cause = next(
            (
                error
                for error in (self.error_for(attempt) for attempt in blocked)
                if error is not None
            ),
            None,
        )
        message = (
            f"{len(blocked)} required pipeline stage(s) did not complete: {details}"
        )
        if cause is not None:
            raise ProcessingError(message) from cause
        raise ProcessingError(message)

    def run_manifest(self) -> dict[str, Any]:
        """What actually happened, stage by stage, for the manifest table."""

        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": _now(),
            "profile": self.profile,
            "limits": dict(self.limits),
            "capability": dict(self.capability),
            "pipeline": [stage.as_payload() for stage in DEFAULT_PIPELINE],
            "attempts": [attempt.as_payload() for attempt in self.attempts],
            "reused_attempts": dict(self.reused),
            "cache": self.cache.describe() if self.cache is not None else None,
            "notes": list(self.notes),
        }

    def index_payload(self) -> dict[str, Any]:
        """The rows the published index keeps about this run."""

        return {
            "run_id": self.run_id,
            "configured_fingerprint": self.configured_manifest.fingerprint,
            "configured_manifest": self.configured_manifest.as_payload(),
            "run_manifest": self.run_manifest(),
            "profile": self.profile,
            "capability": dict(self.capability),
            "limits": dict(self.limits),
            "attempts": self.stage_rows(),
        }

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {status: 0 for status in EXECUTION_STATUSES}
        quality: dict[str, int] = {status: 0 for status in QUALITY_STATUSES}
        for attempt in self.attempts:
            counts[attempt.execution_status] = counts.get(attempt.execution_status, 0) + 1
            quality[attempt.quality_status] = quality.get(attempt.quality_status, 0) + 1
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "execution_status_counts": counts,
            "quality_status_counts": quality,
            "attempts": len(self.attempts),
            "cache": self.cache.describe() if self.cache is not None else None,
            "resident_packs": (
                self.runtime.residency()["packs"] if self.runtime is not None else []
            ),
        }


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def attempt_id(run_id: str, stage: str, document_path: str, number: int) -> str:
    """A deterministic id: one attempt is one stage, document, and ordinal."""

    stem = run_id.split("-")[-1]
    slug = normalize_relative_path(document_path).replace("/", "_") if document_path else "_project"
    return f"att-{stem}-{stage}-{slug}-{number}"


def aggregate_output_sha256(attempts: Iterable[StageAttempt]) -> str:
    return _payload_sha256([attempt.output_sha256 for attempt in attempts])


def _payload_sha256(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(f"{text}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "ATTEMPT_SCHEMA_VERSION",
    "CACHEABLE_EXECUTION_STATUSES",
    "CACHE_SCHEMA_VERSION",
    "CacheStats",
    "configured_manifest",
    "default_cache_root",
    "DEFAULT_PIPELINE",
    "EXECUTION_STATUSES",
    "PIPELINE_STAGE_NAMES",
    "ProcessingError",
    "ProcessingRun",
    "QUALITY_STATUSES",
    "STAGE_BY_NAME",
    "StageAttempt",
    "StageCache",
    "StageContext",
    "StageDefinition",
    "StageOutput",
    "aggregate_output_sha256",
    "attempt_id",
    "downstream_stages",
    "execute_stage",
    "new_run_id",
    "stage_fingerprint",
]
