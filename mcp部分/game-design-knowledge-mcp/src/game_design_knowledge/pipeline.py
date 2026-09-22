"""Handlers for the declared stages, and the entry point that runs them.

Only two stages do heavy work today: ``source_parse`` normalizes source
identity, and ``retrieval_projection`` publishes the index snapshot that the
rest of the server reads. ``ocr`` resolves the degradation chain and records
which engine the projection must use. The remaining stages are declared with
their owning ticket and report ``unavailable`` so that a missing capability is
visible instead of being silently skipped.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Iterable, Mapping, Sequence

from .capabilities import CapabilityRuntime, detect_pack
from .index_build import build_index_atomically
from .indexer import SCHEMA_VERSION, source_documents, excluded_directories, SOURCE_PATTERNS
from .ocr import (
    DEFAULT_CHAIN,
    OcrEngine,
    OcrSelection,
    TESSERACT_ENGINE,
    select_engine,
)
from .ocr_regions import DEFAULT_QUALITY_GATE, QualityGate
from .processing import (
    DEFAULT_PIPELINE,
    ProcessingRun,
    StageAttempt,
    StageCache,
    StageContext,
    StageDefinition,
    StageOutput,
    aggregate_output_sha256,
    configured_manifest,
    default_cache_root,
    downstream_stages,
    execute_stage,
    stage_fingerprint,
)
from .revisions import (
    document_id,
    file_sha256,
    normalize_relative_path,
    parse_revision_id,
    source_revision_id,
)


SOURCE_SUFFIXES = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
PIPELINE_NAMES = tuple(stage.name for stage in DEFAULT_PIPELINE)


# -- handlers -------------------------------------------------------------


def source_parse(context: StageContext) -> StageOutput:
    """Normalize one document's identity without interpreting its content."""

    source_path = (context.project_root / context.document_path).resolve()
    if not source_path.is_file():
        return StageOutput(
            payload={"path": context.document_path, "present": False},
            execution_status="unavailable",
            quality_status="rejected",
            reason_code="source_missing",
            detail=f"{context.document_path} is not readable",
        )
    digest = file_sha256(source_path)
    document_type = SOURCE_SUFFIXES.get(source_path.suffix.lower(), "")
    logical = document_id(context.document_path)
    engine = (
        "image-preflight"
        if source_path.suffix.lower() in IMAGE_SUFFIXES
        else "ooxml-parser"
    )
    return StageOutput(
        payload={
            "path": context.document_path,
            "document_type": document_type,
            "byte_size": source_path.stat().st_size,
            "content_sha256": digest,
            "logical_document_id": logical,
            "source_revision_id": source_revision_id(logical, digest),
            "parse_revision_id": parse_revision_id(
                source_revision_id(logical, digest),
                database_schema_version=SCHEMA_VERSION,
                processing_fingerprint_value=context.config.get(
                    "configured_fingerprint", ""
                )
                or "unconfigured",
            ),
        },
        engine=engine,
        engine_version=f"schema-{SCHEMA_VERSION}",
        coverage={"documents": 1},
    )


def select_ocr(context: StageContext) -> StageOutput:
    """Record the engine this run selected and how the chain got there.

    The run picks the engine once (``run_pipeline``) so every image in the build
    is transcribed by the same tier; the handler grades that decision per
    document and reports the pack status next to it. When the handler is called
    without a recorded selection it walks the chain itself, which is what a
    direct stage test does.
    """

    recorded = context.config.get("selection")
    selection: OcrSelection
    if isinstance(recorded, Mapping):
        engine = _engine_for(str(recorded.get("selected") or ""))
        selection = OcrSelection(
            engine=engine,
            chain=tuple(dict(entry) for entry in recorded.get("chain") or ()),
            execution_status=str(recorded.get("execution_status") or "unavailable"),
            reason_code=str(recorded.get("reason_code") or ""),
            reason=str(recorded.get("reason") or ""),
        )
    else:
        selection = select_engine(runtime=context.runtime)
    payload = selection.as_payload()
    pack_status = detect_pack(
        "core",
        model_root=getattr(context.runtime, "model_root", None),
        hardware=getattr(context.runtime, "hardware", None),
    )
    payload["capability_pack_status"] = pack_status.status
    if selection.engine is None:
        return StageOutput(
            payload=payload,
            execution_status="unavailable",
            quality_status="rejected",
            reason_code=selection.reason_code,
            detail=selection.reason,
            fallback_used=True,
            reason_chain=selection.chain,
            coverage={"images": 0},
        )
    degraded = selection.fallback_used or pack_status.status != "available"
    return StageOutput(
        payload=payload,
        execution_status="partial" if degraded else "succeeded",
        quality_status="accepted",
        reason_code="fallback_engine" if selection.fallback_used else "",
        detail=(
            f"{selection.engine.name} selected"
            + (" as a compatibility fallback" if selection.fallback_used else "")
        ),
        fallback_used=selection.fallback_used,
        reason_chain=selection.chain,
        engine=selection.engine.name,
        engine_version=selection.engine.version(),
    )


def declared_stage(
    context: StageContext,
    *,
    status: str,
    reason_code: str,
    detail: str,
) -> StageOutput:
    """A stage this build declares but does not implement yet."""

    return StageOutput(
        payload={
            "declared": True,
            "owner_ticket": context.definition.owner,
            "capability_pack": context.definition.capability_pack,
        },
        execution_status=status,
        quality_status="rejected",
        reason_code=reason_code,
        detail=detail,
        coverage={},
    )


def _engine_for(name: str) -> OcrEngine | None:
    """The engine object behind a recorded selection name."""

    for candidate in DEFAULT_CHAIN:
        if candidate.name == name:
            return candidate
    return None


def layout(context: StageContext) -> StageOutput:
    return declared_stage(
        context,
        status="unavailable",
        reason_code="stage_not_implemented",
        detail=(
            "Visual elements, reading order, and region association are delivered "
            f"by {context.definition.owner}; core processing continues without them."
        ),
    )


def structure_relations(context: StageContext) -> StageOutput:
    return declared_stage(
        context,
        status="unavailable",
        reason_code="stage_not_implemented",
        detail=(
            "Structural relations are delivered by "
            f"{context.definition.owner}; nothing is inferred in the meantime."
        ),
    )


def notation(context: StageContext) -> StageOutput:
    return declared_stage(
        context,
        status="unavailable",
        reason_code="stage_not_implemented",
        detail=(
            "Notation occurrences need the review dictionary delivered by "
            f"{context.definition.owner}."
        ),
    )


def statements(context: StageContext) -> StageOutput:
    return declared_stage(
        context,
        status="unavailable",
        reason_code="stage_not_implemented",
        detail=(
            f"Atomic statements are delivered by {context.definition.owner}; "
            "no statement is asserted before then."
        ),
    )


def explanation_cache(context: StageContext) -> StageOutput:
    return declared_stage(
        context,
        status="unavailable",
        reason_code="not_configured",
        detail=(
            "No explanation cache is written by this version; explanations are "
            f"produced per request by {context.definition.owner}."
        ),
    )


HANDLERS = {
    "source_parse": source_parse,
    "ocr": select_ocr,
    "layout": layout,
    "structure_relations": structure_relations,
    "notation": notation,
    "statements": statements,
    "explanation_cache": explanation_cache,
}


# -- running --------------------------------------------------------------


def run_pipeline(
    project_root: Path,
    index_directory: Path,
    *,
    runtime: CapabilityRuntime | None = None,
    cache: StageCache | None = None,
    low_memory: bool = False,
    idle_timeout: float | None = None,
    retry_stages: Iterable[str] = (),
    ocr_engine: OcrEngine | None = None,
    allow_compatibility_fallback: bool = True,
    ocr_gate: QualityGate | None = None,
    ocr_language: str | None = None,
    ocr_providers: Mapping[str, Any] | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    prior_history: Mapping[str, Sequence[StageAttempt]] | None = None,
) -> ProcessingRun:
    """Run the declared pipeline and return one fully recorded run.

    ``retry_stages`` re-runs those stages and everything downstream of them;
    every other stage is carried forward as a reuse of its previous attempt
    rather than being re-executed or hidden.
    """

    project_root = Path(project_root).resolve()
    index_directory = Path(index_directory).resolve()
    runtime = runtime or CapabilityRuntime(
        low_memory=low_memory, idle_timeout=idle_timeout
    )
    cache = cache if cache is not None else StageCache(default_cache_root(index_directory))
    manifest = configured_manifest()
    run = ProcessingRun(
        project_root=project_root,
        index_directory=index_directory,
        configured_manifest=manifest,
        profile=runtime.profile,
        limits=runtime.status()["limits"],
        capability={
            "profile": runtime.profile,
            "recommended_profile": runtime.hardware.recommended,
            "low_memory": runtime.low_memory,
            "model_root": str(runtime.model_root),
            "packs": {
                status.name: status.status
                for status in (
                    detect_pack(
                        pack,
                        model_root=runtime.model_root,
                        hardware=runtime.hardware,
                    )
                    for pack in ("core", "enhanced_ocr", "visual")
                )
            },
        },
        runtime=runtime,
        cache=cache,
        history={
            name: list(attempts)
            for name, attempts in (prior_history or {}).items()
        },
    )
    selected = _selected_stages(
        retry_stages=tuple(retry_stages), include=include, exclude=exclude
    )
    documents = _documents(project_root)
    configured_fingerprint = manifest.fingerprint
    config = {
        "configured_fingerprint": configured_fingerprint,
        "schema_version": SCHEMA_VERSION,
        "allow_compatibility_fallback": allow_compatibility_fallback,
    }

    with runtime:
        # One engine for the whole build: the indexer is handed a single engine
        # name, so the chain has to be walked once, not once per document.
        selection = select_engine(
            chain=(
                (ocr_engine, *DEFAULT_CHAIN) if ocr_engine is not None else DEFAULT_CHAIN
            ),
            allow_compatibility_fallback=allow_compatibility_fallback,
            runtime=runtime,
        )
        ocr_settings: dict[str, Any] = {
            "selection": selection.as_payload(),
            "ocr_gate": (ocr_gate or DEFAULT_QUALITY_GATE).as_payload(),
            "ocr_language": ocr_language or "",
            "allow_compatibility_fallback": allow_compatibility_fallback,
        }
        for stage in DEFAULT_PIPELINE:
            if stage.name not in selected:
                previous = (prior_history or {}).get(stage.name) or ()
                if previous:
                    run.reuse(stage.name, previous[-1].attempt_id)
                continue
            if stage.name == "retrieval_projection":
                _run_projection_stage(
                    run,
                    stage,
                    documents,
                    config,
                    selection.engine or TESSERACT_ENGINE,
                    ocr_settings=ocr_settings,
                    ocr_providers=ocr_providers,
                )
                continue
            if not stage.per_document:
                _run_document_stage(
                    run,
                    stage,
                    "",
                    config,
                    runtime,
                    ocr_engine,
                    ocr_settings=ocr_settings,
                )
                continue
            for document in documents:
                _run_document_stage(
                    run,
                    stage,
                    document,
                    config,
                    runtime,
                    ocr_engine,
                    ocr_settings=ocr_settings,
                )
        runtime.end_batch()
    return run


def _selected_stages(
    *,
    retry_stages: tuple[str, ...],
    include: Iterable[str] | None,
    exclude: Iterable[str] | None,
) -> tuple[str, ...]:
    names = [stage.name for stage in DEFAULT_PIPELINE]
    if include is not None:
        requested = set(include)
        unknown = sorted(requested - set(names))
        if unknown:
            raise ValueError(f"unknown stages: {unknown}")
        names = [name for name in names if name in requested]
    if retry_stages:
        requested = set(retry_stages)
        unknown = sorted(requested - set(PIPELINE_NAMES))
        if unknown:
            raise ValueError(f"unknown stages: {unknown}")
        affected: set[str] = set()
        for name in requested:
            affected.update(downstream_stages(name))
        names = [name for name in names if name in affected]
    if exclude is not None:
        names = [name for name in names if name not in set(exclude)]
    return tuple(names)


def _documents(project_root: Path) -> list[str]:
    """The source documents the pipeline and the indexer both agree on.

    The exclusion rule is deliberately the *same* one ``index_documents`` uses,
    so the stage ledger can never describe a different document set than the
    published index. Notably the index directory is not excluded by name: an
    index output holds no ``.docx``/``.xlsx``, while a business directory that
    happens to share its name would be silently dropped from the ledger.
    """

    excluded = excluded_directories(project_root, None)
    found: dict[str, Path] = {}
    for pattern in SOURCE_PATTERNS:
        for path in source_documents(project_root, pattern, excluded):
            relative = normalize_relative_path(
                path.resolve().relative_to(project_root).as_posix()
            )
            found[relative] = path
    return sorted(found)


def _run_document_stage(
    run: ProcessingRun,
    stage: StageDefinition,
    document: str,
    config: Mapping[str, Any],
    runtime: CapabilityRuntime,
    ocr_engine: OcrEngine | None,
    *,
    ocr_settings: Mapping[str, Any] | None = None,
) -> tuple[StageAttempt, StageOutput]:
    handler = HANDLERS.get(stage.name)
    if handler is None:
        raise ValueError(f"no handler for stage {stage.name}")
    source_path = (run.project_root / document).resolve()
    input_sha256 = file_sha256(source_path) if source_path.is_file() else ""
    selection: OcrSelection | None = None
    runtime_identity = ""
    runtime_version = ""
    stage_config: dict[str, Any] = dict(config)
    handler_config: dict[str, Any] = {}
    if stage.name == "ocr":
        stage_config.update(ocr_settings or {})
        selection = select_engine(
            chain=(ocr_engine, *DEFAULT_CHAIN)
            if ocr_engine is not None
            else DEFAULT_CHAIN,
            allow_compatibility_fallback=bool(
                config.get("allow_compatibility_fallback", True)
            ),
            runtime=runtime,
        )
        stage_config["requested_engine"] = (
            ocr_engine.name if ocr_engine is not None else DEFAULT_CHAIN[0].name
        )
        stage_config["selected_engine"] = (
            selection.engine.name if selection.engine is not None else ""
        )
        handler_config["selection"] = selection
        runtime_identity = selection.engine.name if selection.engine else "none"
        runtime_version = (
            selection.engine.version() if selection.engine is not None else ""
        )
    return execute_stage(
        stage,
        handler,
        run=run,
        document_path=document,
        input_sha256=input_sha256,
        upstream=_upstream_payloads(run, stage),
        upstream_sha256=run.upstream_hashes(stage),
        runtime_identity=runtime_identity,
        runtime_version=runtime_version,
        config=stage_config,
        handler_config=handler_config,
    )


def _upstream_payloads(
    run: ProcessingRun, stage: StageDefinition
) -> dict[str, Mapping[str, Any]]:
    payloads: dict[str, Mapping[str, Any]] = {}
    for name in stage.upstream:
        attempts = run.history.get(name) or ()
        if attempts:
            payloads[name] = {"output_sha256": attempts[-1].output_sha256}
    return payloads


def _run_projection_stage(
    run: ProcessingRun,
    stage: StageDefinition,
    documents: Sequence[str],
    config: Mapping[str, Any],
    ocr_engine: OcrEngine | None,
    *,
    ocr_settings: Mapping[str, Any] | None = None,
    ocr_providers: Mapping[str, Any] | None = None,
) -> StageAttempt:
    document_attempts = [
        attempt
        for attempt in run.attempts
        if attempt.stage == "source_parse" and attempt.execution_status == "succeeded"
    ]
    engine = ocr_engine
    if engine is None:
        engine = TESSERACT_ENGINE
    settings = dict(ocr_settings or {})
    run.begin_projection(
        input_sha256=aggregate_output_sha256(document_attempts),
        config={
            "schema_version": SCHEMA_VERSION,
            "documents": len(documents),
            "ocr_engine": engine.name,
            "configured_fingerprint": config.get("configured_fingerprint", ""),
            "ocr_gate": settings.get("ocr_gate", {}),
            "ocr_language": settings.get("ocr_language", ""),
        },
    )
    started = time.monotonic()
    try:
        build_index_atomically(
            run.project_root,
            run.index_directory,
            processing_manifest=run.configured_manifest,
            ocr_engine=engine.name,
            ocr_providers=ocr_providers,
            ocr_gate=settings.get("ocr_gate"),
            ocr_language=settings.get("ocr_language") or None,
            allow_compatibility_fallback=bool(
                settings.get("allow_compatibility_fallback", True)
            ),
            processing_run=run,
        )
    except Exception as error:
        run.record_error(stage.name, "", error)
        definition = stage
        attempt = run.next_attempt(
            stage,
            document_path="",
            execution_status="failed",
            quality_status="rejected",
            fingerprint=stage_fingerprint(
                definition,
                input_sha256=run.projection_input_sha256,
                upstream_sha256=run.upstream_hashes(definition),
                runtime_identity=engine.name,
                runtime_version=engine.version(),
                config=run.projection_config,
            ),
            input_sha256=run.projection_input_sha256,
            output_sha256="",
            started_at=run.projection_started_at or "",
            duration_ms=int((time.monotonic() - started) * 1000),
            reason_code="projection_failed",
            detail=f"{type(error).__name__}: {error}",
        )
        run.add(attempt)
        run.notes.append(f"retrieval_projection failed: {type(error).__name__}")
        return attempt
    if run.projection is None:
        raise RuntimeError("the published build did not record its projection attempt")
    return run.projection


__all__ = [
    "HANDLERS",
    "PIPELINE_NAMES",
    "configured_manifest",
    "run_pipeline",
]
