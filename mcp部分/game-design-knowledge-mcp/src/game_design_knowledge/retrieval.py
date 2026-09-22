"""V2 deterministic retrieval: candidates first, facts only after a re-read.

Retrieval has one job here: decide which retrieval units a question is worth
reading, and in what order. It never decides what is true. Every hit is read
back from the current index before it is returned, every hit says which channel
found it and where that channel ranked it, and a disagreement between two
sources stays visible instead of being averaged, ranked away, or resolved by
the ordering.

Nothing in this module is semantic. The channels are exact text, human
confirmed names and notation, SQLite FTS5 over isolated namespaces, and -- only
when a caller asks for them -- unconfirmed candidates in an explicit
exploration mode. The vector channel is a declared capability of the contract:
this build does not serve it, and asking for it degrades the answer instead of
quietly answering as if the channel had run.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .evidence_package import (
    UNIT_EVIDENCE_PREFIX,
    UNIT_IMAGE_PREFIX,
    display_locator,
    evidence_package,
    hydrated_hit,
    image_layout,
    image_transcription,
    source_reference,
)
from .explanation import claim_topic, claim_values, digest
from .notation import (
    CONFIRMED,
    live_entries,
    normalize_document_path,
    scope_covers,
    scope_key,
)


RETRIEVAL_VERSION = "retrieval-v1"

#: ``auto`` is the default the specification fixes for the V2 retrieval tool:
#: the original query, the deterministic expansions and FTS5, with no vector
#: capability required. ``lexical`` narrows that to the frozen V1 channels.
RETRIEVAL_MODES: tuple[str, ...] = ("lexical", "auto", "hybrid", "semantic")
DEFAULT_MODE = "auto"
VECTOR_MODES: tuple[str, ...] = ("hybrid", "semantic")

MATCH_TYPES: tuple[str, ...] = (
    "exact",
    "confirmed_alias",
    "lexical",
    "semantic_candidate",
)
#: Deterministic priority. It orders candidates; it never ranks evidence.
MATCH_PRIORITY: Mapping[str, int] = {
    name: index for index, name in enumerate(MATCH_TYPES)
}

SOURCE_FACTS = "source_facts"
IMAGE_TRANSCRIPTION = "image_transcription"
VISUAL_INTERPRETATION = "visual_interpretation"
EXPLANATION_NAMESPACE = "explanation"
UNCONFIRMED_CANDIDATES = "unconfirmed_candidates"
NAMESPACES: tuple[str, ...] = (
    SOURCE_FACTS,
    IMAGE_TRANSCRIPTION,
    VISUAL_INTERPRETATION,
    EXPLANATION_NAMESPACE,
    UNCONFIRMED_CANDIDATES,
)
FACT_NAMESPACES = frozenset(
    (SOURCE_FACTS, IMAGE_TRANSCRIPTION, VISUAL_INTERPRETATION)
)

EXACT_CHANNEL = "exact"
ALIAS_CHANNEL = "confirmed_alias"
LEXICAL_CHANNEL = "lexical"
STRUCTURE_CHANNEL = "structures"
CONFLICT_CHANNEL = "conflict_scan"
CANDIDATE_CHANNEL = "unconfirmed"
SEMANTIC_CHANNEL = "semantic"
CHANNELS: tuple[str, ...] = (
    EXACT_CHANNEL,
    LEXICAL_CHANNEL,
    ALIAS_CHANNEL,
    STRUCTURE_CHANNEL,
    CANDIDATE_CHANNEL,
    SEMANTIC_CHANNEL,
    # The scan runs last, over the units the other channels found, so it is
    # reported last as well.
    CONFLICT_CHANNEL,
)

CHANNEL_MATCHED = "matched"
CHANNEL_NO_MATCH = "no_match"
CHANNEL_NOT_CONFIGURED = "not_configured"
CHANNEL_UNAVAILABLE = "unavailable"
CHANNEL_SKIPPED = "skipped"
CHANNEL_FAILED = "failed"

RESPONSE_STATES: tuple[str, ...] = (
    "found",
    "not_found",
    "partial",
    "ambiguous",
    "degraded",
    "failed",
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
#: The explanation channel only ever *adds* a match reason, and it reads one
#: package per candidate, so it is bounded and says how far it got.
EXPLANATION_ASSIST_LIMIT = 10
MAX_ALIAS_EXPANSIONS = 8
#: The claim scan reads rows to find the other side of a claim, so it is capped
#: instead of being allowed to grow into a second full search.
CONFLICT_SCAN_LIMIT = 200

#: OCR output enters a fact query only through a run that passed its own gate.
ACCEPTED_QUALITY = "accepted"
MACHINE_SUPPORTED = "machine-supported"
UNQUALIFIED_OCR = "unqualified_transcription"

#: Mirrors ``explanation.EVIDENCE_STATUSES``; a test pins the two together so a
#: retrieval response can never carry a status the invariants do not know.
EVIDENCE_STATUS_EXPLICIT = "explicit"
EVIDENCE_STATUS_MACHINE = MACHINE_SUPPORTED
EVIDENCE_STATUS_VERIFIED = "verified"
EVIDENCE_STATUS_CANDIDATE = "candidate"
EVIDENCE_STATUS_CONFLICT = "conflict"
EVIDENCE_STATUSES: tuple[str, ...] = (
    EVIDENCE_STATUS_EXPLICIT,
    EVIDENCE_STATUS_MACHINE,
    EVIDENCE_STATUS_VERIFIED,
    EVIDENCE_STATUS_CANDIDATE,
    EVIDENCE_STATUS_CONFLICT,
)

#: A version marker as designers write it, kept as written: ``v2``, ``V2.1``,
#: ``版本 3`` and ``第 3 版`` are four ways to say the same thing.
VERSION_PATTERN = re.compile(
    r"[vV]\s?\d+(?:\.\d+)*|版本\s?\d+(?:\.\d+)*|第\s?\d+\s?版"
)
#: A clock time or a date, kept as written. Retrieval compares them; it never
#: rewrites them into one normalised form, because the source's own writing is
#: what a person checks against the document.
TIME_PATTERN = re.compile(
    r"\d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日"
)

RETRIEVAL_BOUNDARY = (
    "Retrieval picks what to read and in what order; it does not decide what is "
    "true. Every candidate is read back from the current index and keeps its "
    "own source reference and locator."
)
FUSION_BOUNDARY = (
    "Raw scores from different channels are never added together. Candidates "
    "are fused by the sources they point at, then ordered by the deterministic "
    "priority, and every channel keeps its own rank and raw score."
)
CONFLICT_BOUNDARY = (
    "Two sources that disagree about the same scoped claim stay two sides of "
    "one conflict group. A ranking may not hide a value, a unit, a scope, a "
    "version or a time, and retrieval never picks a winner."
)
UNTRACEABLE_BOUNDARY = (
    "A hit that can no longer be read back -- its row is gone, its source "
    "changed, or its locator no longer resolves -- is reported as an "
    "untraceable candidate and never as an answer."
)
CONFLICT_SCAN_BOUNDARY = (
    "The conflict scan re-reads the other sides of a scoped claim that was "
    "found, so a high ranked sentence cannot hide a different value. A scanned "
    "unit is never a query match; it only appears as a side of its own claim."
)
EXPLORATION_BOUNDARY = (
    "Unconfirmed candidates are searchable only when a caller explicitly asks "
    "for them. They are never returned as source facts and never answer a "
    "fact question."
)
DEGRADATION_BOUNDARY = (
    "A channel this build cannot run is reported as missing, with the mode that "
    "actually ran, so a caller can tell a degraded answer from a complete one."
)

#: Fields a conflict dimension can differ in; a group has to name at least one.
#: Scope is not among the reported dimensions: it is the identity of the claim
#: (a group only holds sides that share one section and one unit type), so the
#: scope of every side is published on the side instead of being listed here.
CONFLICT_DIMENSIONS: tuple[str, ...] = ("value", "unit", "scope", "version", "time")


class RetrievalError(ValueError):
    """A retrieval request this build refuses rather than answering loosely."""


def retrieve(
    index: Any,
    *,
    query: str,
    document_type: str | None = None,
    evidence_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
    mode: str = DEFAULT_MODE,
    include_candidates: bool = False,
    document: str = "",
    notation: Mapping[str, Any] | None = None,
    degradations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Answer one query with hydrated candidates, channels and conflicts.

    ``include_candidates`` is the explicit exploration mode: it searches
    unconfirmed candidates that a default fact query must not use.
    ``degradations`` carries the capabilities the caller could not supply (an
    unreadable notation dictionary, for instance), so a degraded answer says so
    instead of looking complete.
    """

    text = str(query or "").strip()
    if not text:
        raise RetrievalError("query must not be empty")
    if mode not in RETRIEVAL_MODES:
        raise RetrievalError(f"mode must be one of {list(RETRIEVAL_MODES)}, not {mode!r}")
    size = _limit(limit)
    document_path = str(document or "").strip()
    if document_path:
        document_path = normalize_document_path(document_path)

    limitation_notes: list[str] = []
    hits: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []

    try:
        plan = _plan(
            index,
            text,
            document=document_path,
            document_type=str(document_type or ""),
            notation=notation,
        )
    except RetrievalError:
        raise
    except (sqlite3.DatabaseError, OSError) as error:
        return _failed(text, mode, error)

    variants = plan["variants"]
    expansions = plan["expansions"]
    original = [variant for variant in variants if variant["channel"] == LEXICAL_CHANNEL]
    aliases = [variant for variant in variants if variant["channel"] == ALIAS_CHANNEL]
    # An ``evidence_type`` names a source-fact type, so asking for one drops the
    # units that are not statements at all instead of silently ignoring it.
    units_only = bool(str(evidence_type or "").strip())

    exact = _collect(
        _sources(
            index,
            document_type=document_type,
            evidence_type=evidence_type,
            include_images=not units_only,
            include_regions=not units_only,
            limit=size,
        ),
        original,
        limit=size,
        exact=True,
    )
    hits.extend(exact["hits"])
    reports.append(
        _channel_report(
            EXACT_CHANNEL,
            exact,
            detail="原文与查询逐字相同的检索单位",
        )
    )

    lexical = _collect(
        _sources(
            index,
            document_type=document_type,
            evidence_type=evidence_type,
            include_images=not units_only,
            include_regions=not units_only,
            limit=size,
        ),
        original,
        limit=size,
        exact=False,
    )
    hits.extend(lexical["hits"])
    reports.append(
        _channel_report(
            LEXICAL_CHANNEL,
            lexical,
            detail="FTS5 短语匹配，短查询回退为词面包含",
        )
    )

    alias = _collect(
        _sources(
            index,
            document_type=document_type,
            evidence_type=evidence_type,
            include_images=not units_only,
            include_regions=not units_only,
            limit=size,
        ),
        aliases,
        limit=size,
        exact=False,
    )
    reports.append(
        _channel_report(
            ALIAS_CHANNEL,
            alias,
            detail="只使用人工确认过的正式名、别名与记法含义做确定性扩展",
            expansions=len(expansions),
        )
    )
    hits.extend(alias["hits"])

    structures = _structures(index, variants, document_type=document_type, limit=size)
    hits.extend(structures["hits"])
    reports.append(
        _channel_report(
            STRUCTURE_CHANNEL,
            structures,
            detail="已确认结构关系的阅读顺序文本；几何结论，不含因果",
        )
    )

    if include_candidates:
        unconfirmed = _unconfirmed(index, variants, document_type=document_type, limit=size)
        hits.extend(unconfirmed["hits"])
        reports.append(
            _channel_report(
                CANDIDATE_CHANNEL,
                unconfirmed,
                detail="探索模式：未通过质量门槛的转写与候选关系",
            )
        )
    else:
        reports.append(
            {
                "channel": CANDIDATE_CHANNEL,
                "status": CHANNEL_SKIPPED,
                "hits": 0,
                "detail": EXPLORATION_BOUNDARY,
                "reason": "exploration_not_requested",
            }
        )

    reports.append(_semantic_report(mode))

    fused_hits: list[dict[str, Any]] = []
    held_back = 0
    for hit in _fuse(hits):
        if not include_candidates and str(hit["namespace"]) == UNCONFIRMED_CANDIDATES:
            # A default fact query never answers with an unconfirmed candidate,
            # and never hides that it held one back either.
            held_back += 1
            continue
        fused_hits.append(hit)
    hydrated, untraceable = _hydrate(index, fused_hits)
    fused = hydrated[:size]
    fused, assist = _explanation_assist(
        index, fused, variants=variants, notation=notation
    )
    if assist["status"] == CHANNEL_UNAVAILABLE:
        limitation_notes.append(assist["detail"])

    scanned, scan = _conflict_scan(
        index,
        fused,
        document_type=document_type,
        evidence_type=evidence_type,
    )
    reports.append(
        _channel_report(
            CONFLICT_CHANNEL,
            scan,
            detail=CONFLICT_SCAN_BOUNDARY,
        )
    )
    scanned, scan_untraceable = _hydrate(index, scanned)
    untraceable.extend(scan_untraceable)

    namespaces = _namespace_report(fused, untraceable, reports)
    groups, potentials = _conflicts([*fused, *scanned])
    facts = [candidate for candidate in fused if candidate["namespace"] in FACT_NAMESPACES]
    kind = _kind(mode)
    kind["degradation_events"] = [
        *kind["degradation_events"],
        *(
            _capability_event(mode, event, kind["effective"])
            for event in degradations
        ),
    ]
    state = _state(
        facts=facts,
        untraceable=untraceable,
        reports=reports,
        groups=groups,
        mode=mode,
        degradations=degradations,
    )
    if state == "not_found":
        limitation_notes.append(
            "所有通道都没有合格候选：当前索引的文档证据里未找到该查询，且不做相似玩法联想。"
        )
    if held_back:
        limitation_notes.append(
            f"{held_back} 个未确认候选被挡在事实答案之外："
            "默认事实查询不使用它们作答，探索模式可以显式查看。"
        )
    for event in degradations:
        limitation_notes.append(
            f"{event.get('channel')} 不可用：本次回答是在缺少它的情况下给出的。"
        )
    if include_candidates and any(
        candidate["namespace"] == UNCONFIRMED_CANDIDATES for candidate in fused
    ):
        limitation_notes.append(
            "本次包含探索模式返回的未确认候选：它们是候选读法，不是项目事实。"
        )

    evidence = [_evidence_shape(candidate) for candidate in facts]
    return {
        "status": state,
        "schema_version": RETRIEVAL_VERSION,
        "query": text,
        "mode": {"requested": mode, "effective": kind["effective"]},
        "expansions": expansions,
        "namespaces": namespaces,
        "channels": reports,
        "candidates": fused,
        "evidence": evidence,
        "untraceable": untraceable,
        "conflicts": potentials,
        "conflict_groups": groups,
        "conflict_dimensions": list(CONFLICT_DIMENSIONS),
        "retrieval": {
            "limit": size,
            "document": document_path or None,
            "document_type": document_type or None,
            "evidence_type": evidence_type or None,
            "exploration": bool(include_candidates),
            "held_back_unconfirmed": held_back,
            "unavailable_capabilities": [
                str(event.get("channel")) for event in degradations
            ],
            "candidate_count": len(fused),
            "returned": len(fused),
            "fusion_boundary": FUSION_BOUNDARY,
            "boundary": RETRIEVAL_BOUNDARY,
        },
        "response_meta": {
            "retrieval_state": state,
            "requested_mode": mode,
            "effective_mode": kind["effective"],
            "channels": {report["channel"]: report["status"] for report in reports},
            "vector": kind["vector"],
            "degradation_events": kind["degradation_events"],
            "rebuild_vector_index_recommended": any(
                str(event.get("channel")) == SEMANTIC_CHANNEL
                for event in kind["degradation_events"]
            ),
            "expansions": len(expansions),
            "explanation_assist": {
                "status": assist["status"],
                "examined": assist["examined"],
                "assisted": assist["assisted"],
                "creates_candidates": False,
                "detail": assist["detail"],
            },
            "boundary": DEGRADATION_BOUNDARY,
        },
        "limitations": limitation_notes,
        "boundary": RETRIEVAL_BOUNDARY,
    }


def _limit(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError) as failure:
        raise RetrievalError("limit must be an integer") from failure
    if size < 1 or size > MAX_LIMIT:
        raise RetrievalError(f"limit must be between 1 and {MAX_LIMIT}")
    return size


def _plan(
    index: Any,
    query: str,
    *,
    document: str,
    document_type: str,
    notation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The original question, plus every expansion this build is allowed to add.

    The original query is never rewritten. Annotation keeps the rule that
    triggered each expansion, so a caller can audit why a hit was found.
    """

    variants: list[dict[str, Any]] = [
        {
            "text": query,
            "match_type": "lexical",
            "channel": LEXICAL_CHANNEL,
            "reason": "原始查询",
            "rule": None,
        }
    ]
    expansions: list[dict[str, Any]] = []
    dictionary = notation if isinstance(notation, Mapping) else {}
    entries = _live_entries(dictionary)
    for entry in entries:
        if len(expansions) >= MAX_ALIAS_EXPANSIONS:
            break
        scope = entry.get("scope") or {}
        if not scope_covers(
            scope, document=document, document_type=document_type, region=""
        ):
            continue
        token = str(entry.get("notation_token") or "").strip()
        meaning = str(entry.get("meaning") or "").strip()
        if not token or not meaning:
            continue
        if token in query:
            expanded, direction = meaning, "token_to_meaning"
        elif meaning in query:
            expanded, direction = token, "meaning_to_token"
        else:
            continue
        rule = {
            "kind": "notation_entry",
            "entry_id": str(entry.get("entry_id") or ""),
            "scope": scope_key(scope),
            "authority": str(entry.get("authority") or ""),
            "notation_token": token,
            "meaning": meaning,
            "direction": direction,
            "status": CONFIRMED,
        }
        expansions.append(rule)
        variants.append(
            {
                "text": expanded,
                "match_type": "confirmed_alias",
                "channel": ALIAS_CHANNEL,
                "reason": f"已确认记法：{token} 读作「{meaning}」",
                "rule": rule,
            }
        )

    for variant in _catalog_variants(index, query):
        if len(expansions) >= MAX_ALIAS_EXPANSIONS:
            break
        expansions.append(variant["rule"])
        variants.append(variant)
    return {
        "variants": variants,
        "expansions": expansions,
        "dictionary": {
            "status": "served" if entries else "empty",
            "entries": len(entries),
        },
    }


def _catalog_variants(index: Any, query: str) -> list[dict[str, Any]]:
    """Confirmed feature names and aliases, expanded in both directions."""

    try:
        rows = index.fetchall(
            """
            SELECT f.feature_key, f.canonical_name, f.source,
                   a.alias, a.confirmed_at, a.confirmed_by
            FROM catalog_features AS f
            LEFT JOIN catalog_aliases AS a ON a.feature_id = f.id
            ORDER BY f.feature_key, a.alias
            """
        )
    except sqlite3.OperationalError:
        return []
    features: dict[str, dict[str, Any]] = {}
    for row in rows:
        feature = features.setdefault(
            str(row["feature_key"]),
            {
                "canonical_name": str(row["canonical_name"]),
                "source": str(row["source"]),
                "aliases": [],
            },
        )
        if row["alias"]:
            feature["aliases"].append(
                {
                    "name": str(row["alias"]),
                    "confirmed_at": row["confirmed_at"],
                    "confirmed_by": row["confirmed_by"],
                }
            )

    variants: list[dict[str, Any]] = []
    for key, feature in sorted(features.items()):
        canonical = feature["canonical_name"]
        names = [canonical, *[alias["name"] for alias in feature["aliases"]]]
        matched = next((name for name in names if name and name in query), "")
        if not matched:
            continue
        for name in names:
            if not name or name == matched:
                continue
            rule = {
                "kind": "catalog_alias",
                "feature_key": key,
                "canonical_name": canonical,
                "matched_name": matched,
                "expanded_name": name,
                "source": feature["source"],
            }
            variants.append(
                {
                    "text": name,
                    "match_type": "confirmed_alias",
                    "channel": ALIAS_CHANNEL,
                    "reason": f"已确认别名：{matched} ↔ {name}",
                    "rule": rule,
                }
            )
            if len(variants) >= MAX_ALIAS_EXPANSIONS:
                return variants
    return variants


def _live_entries(dictionary: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        return [dict(entry) for entry in live_entries(dictionary)]
    except (TypeError, ValueError):
        return []


def _sources(
    index: Any,
    *,
    document_type: str | None,
    evidence_type: str | None,
    include_images: bool,
    include_regions: bool,
    limit: int,
) -> list[tuple[str, Any]]:
    """The namespaces one text channel may reach, as callables over a variant."""

    sources: list[tuple[str, Any]] = [
        (
            "statements",
            lambda variant, exact: _statement_hits(
                index,
                variant,
                document_type=document_type,
                evidence_type=evidence_type,
                limit=limit,
                exact=exact,
            ),
        )
    ]
    if include_images:
        sources.append(
            (
                "image_text",
                lambda variant, exact: _image_text_hits(
                    index,
                    variant,
                    document_type=document_type,
                    limit=limit,
                    exact=exact,
                ),
            )
        )
    if include_regions:
        sources.append(
            (
                "regions",
                lambda variant, exact: _region_hits(
                    index,
                    variant,
                    document_type=document_type,
                    limit=limit,
                    exact=exact,
                ),
            )
        )
    return sources


def _collect(
    sources: Sequence[tuple[str, Any]],
    variants: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    exact: bool,
) -> dict[str, Any]:
    """Run one text channel over the variants and report what it reached."""

    if not variants:
        return {
            "hits": [],
            "status": CHANNEL_NO_MATCH,
            "detail": "没有可用的查询扩展",
        }
    hits: list[dict[str, Any]] = []
    reached: list[str] = []
    blocked: set[str] = set()
    for variant in variants:
        for source, gather in sources:
            try:
                found = gather(variant, exact)
            except sqlite3.OperationalError as error:
                # One unreadable namespace degrades this channel; it never
                # discards the namespaces that did answer.
                blocked.add(f"{source}（{error}）")
                continue
            if found:
                reached.append(source)
            hits.extend(list(found)[:limit])
    if blocked:
        return {
            "hits": hits,
            "status": CHANNEL_UNAVAILABLE,
            "reason": "namespace_not_in_index",
            "reached": sorted(set(reached)),
            "detail": "部分命名空间在这个索引里不可读：" + "；".join(sorted(blocked)),
        }
    if not hits:
        return {"hits": [], "status": CHANNEL_NO_MATCH, "reached": sorted(set(reached))}
    return {
        "hits": hits,
        "status": CHANNEL_MATCHED,
        "reached": sorted(set(reached)),
    }


def _statement_hits(
    index: Any,
    variant: Mapping[str, Any],
    *,
    document_type: str | None,
    evidence_type: str | None,
    limit: int,
    exact: bool,
) -> list[dict[str, Any]]:
    """Source statements and configuration facts."""

    text = str(variant["text"])
    if exact:
        rows = index.fetchall(
            """
            SELECT e.id AS evidence_id, e.evidence_type, e.text, e.section_path,
                   e.locator, e.authority, b.ordinal AS block_ordinal,
                   d.path AS source_document, d.document_type, d.source_sha256,
                   0.0 AS score
            FROM evidence AS e
            JOIN documents AS d ON d.id = e.document_id
            LEFT JOIN document_blocks AS b
              ON e.source_table = 'document_blocks' AND b.id = e.source_record_id
            WHERE TRIM(e.text) = ? COLLATE NOCASE
              AND (? IS NULL OR d.document_type = ?)
              AND (? IS NULL OR e.evidence_type = ?)
            ORDER BY e.id
            LIMIT ?
            """,
            (text, document_type, document_type, evidence_type, evidence_type, limit),
        )
    else:
        rows = index.fetchall(
            """
            SELECT e.id AS evidence_id, e.evidence_type, e.text, e.section_path,
                   e.locator, e.authority, b.ordinal AS block_ordinal,
                   d.path AS source_document, d.document_type, d.source_sha256,
                   bm25(evidence_fts) AS score
            FROM evidence_fts
            JOIN evidence AS e ON e.id = evidence_fts.evidence_id
            JOIN documents AS d ON d.id = e.document_id
            LEFT JOIN document_blocks AS b
              ON e.source_table = 'document_blocks' AND b.id = e.source_record_id
            WHERE evidence_fts MATCH ?
              AND (? IS NULL OR d.document_type = ?)
              AND (? IS NULL OR e.evidence_type = ?)
            ORDER BY score, e.id
            LIMIT ?
            """,
            (_phrase(text), document_type, document_type, evidence_type, evidence_type, limit),
        )
        if not rows and len(text) < 3:
            rows = index.fetchall(
                """
                SELECT e.id AS evidence_id, e.evidence_type, e.text, e.section_path,
                       e.locator, e.authority, b.ordinal AS block_ordinal,
                       d.path AS source_document, d.document_type, d.source_sha256,
                       0.0 AS score
                FROM evidence AS e
                JOIN documents AS d ON d.id = e.document_id
                LEFT JOIN document_blocks AS b
                  ON e.source_table = 'document_blocks' AND b.id = e.source_record_id
                WHERE (instr(e.text, ?) > 0 OR instr(e.section_path, ?) > 0)
                  AND (? IS NULL OR d.document_type = ?)
                  AND (? IS NULL OR e.evidence_type = ?)
                ORDER BY e.id
                LIMIT ?
                """,
                (
                    text,
                    text,
                    document_type,
                    document_type,
                    evidence_type,
                    evidence_type,
                    limit,
                ),
            )

    return [
        _hit(
            variant,
            rank=rank,
            exact=exact,
            unit_id=f"{UNIT_EVIDENCE_PREFIX}{int(row['evidence_id'])}",
            namespace=SOURCE_FACTS,
            unit_type=str(row["evidence_type"]),
            text=str(row["text"]),
            # A statement or a configuration cell is the document's own text:
            # which of the two it is stays in ``authority``, not in the status.
            evidence_status=EVIDENCE_STATUS_EXPLICIT,
            source_document=str(row["source_document"]),
            document_type=str(row["document_type"]),
            record_id=int(row["evidence_id"]),
            score=row["score"],
            recorded_sha256=str(row["source_sha256"]),
        )
        for rank, row in enumerate(rows, start=1)
    ]


def _image_text_hits(
    index: Any,
    variant: Mapping[str, Any],
    *,
    document_type: str | None,
    limit: int,
    exact: bool,
) -> list[dict[str, Any]]:
    """Text recorded next to an image, and the image's own coarse OCR text."""

    if exact:
        rows = index.fetchall(
            """
            SELECT i.id AS image_id, i.heading, i.context_text, i.ocr_text,
                   i.ocr_status, i.relationship_id, i.paragraph_index, i.sheet_name,
                   i.cell_anchor, i.source_part, i.asset_path,
                   d.path AS source_document, d.document_type, d.source_sha256,
                   (SELECT run.quality_status FROM ocr_runs AS run
                     WHERE run.image_id = i.id ORDER BY run.id DESC LIMIT 1)
                     AS quality_status,
                   (SELECT run.evidence_state FROM ocr_runs AS run
                     WHERE run.image_id = i.id ORDER BY run.id DESC LIMIT 1)
                     AS evidence_state
            FROM images AS i
            JOIN documents AS d ON d.id = i.document_id
            WHERE (TRIM(COALESCE(i.heading, '')) = ? COLLATE NOCASE
                   OR TRIM(i.context_text) = ? COLLATE NOCASE
                   OR TRIM(i.ocr_text) = ? COLLATE NOCASE)
              AND (? IS NULL OR d.document_type = ?)
            ORDER BY i.id
            LIMIT ?
            """,
            (
                str(variant["text"]),
                str(variant["text"]),
                str(variant["text"]),
                document_type,
                document_type,
                limit,
            ),
        )
        scores: dict[int, Any] = {int(row["image_id"]): None for row in rows}
    else:
        rows = index.fetchall(
            """
            SELECT i.id AS image_id, i.heading, i.context_text, i.ocr_text,
                   i.ocr_status, i.relationship_id, i.paragraph_index, i.sheet_name,
                   i.cell_anchor, i.source_part, i.asset_path,
                   d.path AS source_document, d.document_type, d.source_sha256,
                   bm25(image_fts) AS score,
                   (SELECT run.quality_status FROM ocr_runs AS run
                     WHERE run.image_id = i.id ORDER BY run.id DESC LIMIT 1)
                     AS quality_status,
                   (SELECT run.evidence_state FROM ocr_runs AS run
                     WHERE run.image_id = i.id ORDER BY run.id DESC LIMIT 1)
                     AS evidence_state
            FROM image_fts
            JOIN images AS i ON i.id = image_fts.image_id
            JOIN documents AS d ON d.id = i.document_id
            WHERE image_fts MATCH ?
              AND (? IS NULL OR d.document_type = ?)
            ORDER BY score, i.id
            LIMIT ?
            """,
            (_phrase(str(variant["text"])), document_type, document_type, limit),
        )
        scores = {int(row["image_id"]): row["score"] for row in rows}

    hits: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        text = str(variant["text"])
        heading = str(row["heading"] or "")
        context = str(row["context_text"] or "")
        ocr_text = str(row["ocr_text"] or "")
        qualified = (
            str(row["quality_status"] or "") == ACCEPTED_QUALITY
            and str(row["evidence_state"] or "") == MACHINE_SUPPORTED
        )
        in_source_text = (heading and text in heading) or (context and text in context)
        if in_source_text:
            namespace, status = SOURCE_FACTS, EVIDENCE_STATUS_EXPLICIT
            unit_text = _first_line(heading, context, ocr_text)
        elif ocr_text:
            namespace = IMAGE_TRANSCRIPTION if qualified else UNCONFIRMED_CANDIDATES
            status = (
                EVIDENCE_STATUS_MACHINE if qualified else EVIDENCE_STATUS_CANDIDATE
            )
            unit_text = ocr_text
        else:
            namespace, status = SOURCE_FACTS, EVIDENCE_STATUS_EXPLICIT
            unit_text = _first_line(heading, context, ocr_text)
        hits.append(
            _hit(
                variant,
                rank=rank,
                exact=exact,
                unit_id=f"{UNIT_IMAGE_PREFIX}{int(row['image_id'])}",
                namespace=namespace,
                unit_type="image",
                text=unit_text,
                evidence_status=status,
                source_document=str(row["source_document"]),
                document_type=str(row["document_type"]),
                record_id=int(row["image_id"]),
                score=scores.get(int(row["image_id"])),
                recorded_sha256=str(row["source_sha256"]),
                regions={"region_index": None},
            )
        )
    return hits


def _region_hits(
    index: Any,
    variant: Mapping[str, Any],
    *,
    document_type: str | None,
    limit: int,
    exact: bool,
) -> list[dict[str, Any]]:
    """Re-qualified region transcription of one image.

    The channel deliberately reaches both gates: a run whose quality was
    accepted and whose evidence state is machine-supported is a transcription
    this build may show as machine-supported, and anything else is only
    reachable through the exploration channel.
    """

    if exact:
        predicate = "TRIM(r.text_raw) = ? COLLATE NOCASE"
        parameters: tuple[Any, ...] = (str(variant["text"]),)
    else:
        predicate = "instr(r.text_raw, ?) > 0"
        parameters = (str(variant["text"]),)
    rows = index.fetchall(
        f"""
        SELECT r.image_id, r.region_index, r.reading_order, r.text_raw,
               r.text_confidence, r.region_confidence, r.key_mark_confidence,
               r.bbox, run.quality_status, run.evidence_state,
               i.relationship_id, i.heading, i.paragraph_index, i.sheet_name,
               i.cell_anchor, i.source_part, i.asset_path,
               d.path AS source_document, d.document_type, d.source_sha256
        FROM ocr_regions AS r
        JOIN ocr_runs AS run ON run.id = r.run_id
        JOIN images AS i ON i.id = r.image_id
        JOIN documents AS d ON d.id = i.document_id
        WHERE {predicate}
          AND (? IS NULL OR d.document_type = ?)
        ORDER BY r.image_id, r.reading_order, r.region_index
        LIMIT ?
        """,
        (*parameters, document_type, document_type, limit),
    )
    hits: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        qualified = (
            str(row["quality_status"] or "") == ACCEPTED_QUALITY
            and str(row["evidence_state"] or "") == MACHINE_SUPPORTED
        )
        hits.append(
            _hit(
                variant,
                rank=rank,
                exact=exact,
                unit_id=f"{UNIT_IMAGE_PREFIX}{int(row['image_id'])}",
                namespace=IMAGE_TRANSCRIPTION if qualified else UNCONFIRMED_CANDIDATES,
                unit_type="image_text",
                text=str(row["text_raw"]),
                evidence_status=(
                    EVIDENCE_STATUS_MACHINE if qualified else EVIDENCE_STATUS_CANDIDATE
                ),
                source_document=str(row["source_document"]),
                document_type=str(row["document_type"]),
                record_id=int(row["image_id"]),
                score=None,
                recorded_sha256=str(row["source_sha256"]),
                regions={"region_index": int(row["region_index"])},
                extra=(
                    {}
                    if qualified
                    else {
                        "reason": UNQUALIFIED_OCR,
                        "quality_status": str(row["quality_status"] or ""),
                        "evidence_state": str(row["evidence_state"] or ""),
                    }
                ),
            )
        )
    return hits


def _structures(
    index: Any,
    variants: Sequence[Mapping[str, Any]],
    *,
    document_type: str | None,
    limit: int,
) -> dict[str, Any]:
    """Reading-order statements derived from structural relations.

    A confirmed relation becomes a sentence a reader can check against the
    picture ("A 之后是 B, geometry only"). An unconfirmed one is only reachable
    in exploration mode, where it stays a candidate.
    """

    try:
        rows = index.fetchall(
            """
            SELECT rel.id AS relation_id, rel.image_id, rel.kind, rel.status,
                   rel.source_region, rel.target_region, rel.via_regions,
                   rel.direction, rel.geometry_basis, rel.detail, rel.uncertainty,
                   rel.geometry_confidence, rel.ocr_confidence, rel.rule_version,
                   src.text_raw AS source_text, tgt.text_raw AS target_text,
                   i.relationship_id, i.heading, i.paragraph_index, i.sheet_name,
                   i.cell_anchor, i.source_part, i.asset_path,
                   d.path AS source_document, d.document_type, d.source_sha256
            FROM structural_relations AS rel
            JOIN images AS i ON i.id = rel.image_id
            JOIN documents AS d ON d.id = i.document_id
            LEFT JOIN ocr_regions AS src
              ON src.image_id = rel.image_id AND src.region_index = rel.source_region
            LEFT JOIN ocr_regions AS tgt
              ON tgt.image_id = rel.image_id AND tgt.region_index = rel.target_region
            WHERE (? IS NULL OR d.document_type = ?)
            ORDER BY rel.image_id, rel.id
            LIMIT ?
            """,
            (document_type, document_type, max(limit * 8, 40)),
        )
    except sqlite3.OperationalError:
        return {
            "hits": [],
            "status": CHANNEL_UNAVAILABLE,
            "reason": "namespace_not_in_index",
            "detail": "这个索引没有结构关系层。",
        }

    hits: list[dict[str, Any]] = []
    for row in rows:
        source_text = str(row["source_text"] or "")
        target_text = str(row["target_text"] or "")
        if not source_text or not target_text:
            continue
        statement = f"{source_text} 之后是 {target_text}（{str(row['kind'])}）"
        matched = next(
            (
                variant
                for variant in variants
                if str(variant["text"]) in statement
                or str(variant["text"]) in source_text
                or str(variant["text"]) in target_text
            ),
            None,
        )
        if matched is None:
            continue
        confirmed = str(row["status"]) == CONFIRMED
        hits.append(
            _hit(
                matched,
                rank=len(hits) + 1,
                unit_id=f"{UNIT_IMAGE_PREFIX}{int(row['image_id'])}",
                namespace=VISUAL_INTERPRETATION if confirmed else UNCONFIRMED_CANDIDATES,
                unit_type="structural_relation",
                text=statement,
                evidence_status=(
                    EVIDENCE_STATUS_VERIFIED if confirmed else EVIDENCE_STATUS_CANDIDATE
                ),
                source_document=str(row["source_document"]),
                document_type=str(row["document_type"]),
                record_id=int(row["image_id"]),
                score=None,
                recorded_sha256=str(row["source_sha256"]),
                regions={"region_index": int(row["source_region"])},
                extra={
                    "relation": {
                        "kind": str(row["kind"]),
                        "status": str(row["status"]),
                        "source_region": int(row["source_region"]),
                        "target_region": int(row["target_region"]),
                        "rule_version": str(row["rule_version"]),
                        "uncertainty": str(row["uncertainty"] or ""),
                        "claim_boundary": (
                            "a confirmed step records visible geometry only"
                        ),
                    }
                },
            )
        )
        if len(hits) >= limit:
            break
    return {"hits": hits, "status": CHANNEL_MATCHED if hits else CHANNEL_NO_MATCH}


def _unconfirmed(
    index: Any,
    variants: Sequence[Mapping[str, Any]],
    *,
    document_type: str | None,
    limit: int,
) -> dict[str, Any]:
    """The exploration channel: candidates a fact query may not answer with."""

    hits: list[dict[str, Any]] = []
    reports: list[str] = []
    for variant in variants:
        try:
            hits.extend(
                hit
                for hit in _region_hits(
                    index,
                    variant,
                    document_type=document_type,
                    limit=limit,
                    exact=False,
                )
                if hit["namespace"] == UNCONFIRMED_CANDIDATES
            )
        except sqlite3.OperationalError:
            reports.append("regions")
        try:
            hits.extend(
                hit
                for hit in _structures(
                    index, [variant], document_type=document_type, limit=limit
                )["hits"]
                if hit["namespace"] == UNCONFIRMED_CANDIDATES
            )
        except sqlite3.OperationalError:
            reports.append("structures")
    if not hits:
        return {"hits": [], "status": CHANNEL_NO_MATCH}
    return {"hits": hits, "status": CHANNEL_MATCHED, "reached": ["unconfirmed_candidates"]}


def _semantic_report(mode: str) -> dict[str, Any]:
    """The vector channel: declared by the contract, not served by this build."""

    if mode == "lexical" or mode == "auto":
        return {
            "channel": SEMANTIC_CHANNEL,
            "status": CHANNEL_NOT_CONFIGURED,
            "hits": 0,
            "detail": "本构建不提供向量召回：auto 只跑确定性通道，因此没有降级。",
            "reason": "vector_capability_not_configured",
        }
    return {
        "channel": SEMANTIC_CHANNEL,
        "status": CHANNEL_NOT_CONFIGURED,
        "hits": 0,
        "detail": "请求了向量召回，本构建没有 embedding 模型与向量索引，已降级为确定性通道。",
        "reason": "vector_capability_not_configured",
    }


def _hit(
    variant: Mapping[str, Any],
    *,
    rank: int,
    exact: bool = False,
    unit_id: str,
    namespace: str,
    unit_type: str,
    text: str,
    evidence_status: str,
    source_document: str,
    document_type: str,
    record_id: int,
    score: Any,
    recorded_sha256: str,
    regions: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    matched_query: bool = True,
) -> dict[str, Any]:
    """One hit, before the units it points at are read back."""

    hit = {
        "unit_id": unit_id,
        "namespace": namespace,
        "unit_type": unit_type,
        "matched_query": bool(matched_query),
        # An exact hit is exact whichever variant matched it: the deterministic
        # priority puts it above the alias and lexical channels.
        "match_type": "exact" if exact else str(variant["match_type"]),
        "channel": str(variant["channel"]),
        "rank": rank,
        "score": score,
        "reason": str(variant["reason"]),
        "rule": variant.get("rule"),
        "queried_text": str(variant["text"]),
        "text": text,
        "evidence_status": evidence_status,
        "source_document": source_document,
        "document_type": document_type,
        "record_id": record_id,
        "recorded_sha256": recorded_sha256,
        "regions": dict(regions or {}),
    }
    if extra:
        hit.update(dict(extra))
    return hit


def _fuse(hits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge the channels that point at the same retrieval unit."""

    merged: dict[str, dict[str, Any]] = {}
    for hit in hits:
        unit_id = str(hit["unit_id"])
        candidate = merged.get(unit_id)
        if candidate is None:
            candidate = {
                "unit_id": unit_id,
                "namespace": str(hit["namespace"]),
                "unit_type": str(hit["unit_type"]),
                "match_type": str(hit["match_type"]),
                "evidence_status": str(hit["evidence_status"]),
                "text": str(hit["text"]),
                "source_document": str(hit["source_document"]),
                "document_type": str(hit["document_type"]),
                "record_id": int(hit["record_id"]),
                "recorded_sha256": str(hit["recorded_sha256"]),
                "regions": dict(hit["regions"]),
                "matched_query": bool(hit.get("matched_query", True)),
                "channels": [],
            }
            merged[unit_id] = candidate
        if MATCH_PRIORITY[str(hit["match_type"])] < MATCH_PRIORITY[
            str(candidate["match_type"])
        ]:
            candidate["match_type"] = str(hit["match_type"])
        # A unit that arrived as both fact and candidate is reported as the
        # candidate: the stronger claim is the one that has to be earned.
        if str(hit["namespace"]) == UNCONFIRMED_CANDIDATES:
            candidate["namespace"] = UNCONFIRMED_CANDIDATES
        if (
            str(hit["namespace"]) == UNCONFIRMED_CANDIDATES
            or str(hit["evidence_status"]) == EVIDENCE_STATUS_CANDIDATE
        ):
            candidate["evidence_status"] = EVIDENCE_STATUS_CANDIDATE
        if not hit.get("matched_query", True):
            # A unit the conflict scan re-read never becomes a query match.
            candidate["matched_query"] = False
        entry: dict[str, Any] = {
            "channel": str(hit["channel"]),
            "match_type": str(hit["match_type"]),
            "rank": int(hit["rank"]),
            "raw_score": hit["score"],
            "reason": str(hit["reason"]),
        }
        if hit.get("rule"):
            entry["rule"] = dict(hit["rule"])
        if hit.get("relation"):
            entry["relation"] = dict(hit["relation"])
        key = (entry["channel"], entry["match_type"], entry["reason"])
        if any(
            existing["channel"] == key[0]
            and existing["match_type"] == key[1]
            and existing["reason"] == key[2]
            for existing in candidate["channels"]
        ):
            continue
        candidate["channels"].append(entry)
        if hit.get("reason") == UNQUALIFIED_OCR:
            candidate["unqualified_transcription"] = True
    for candidate in merged.values():
        candidate["channels"].sort(
            key=lambda entry: (
                MATCH_PRIORITY[str(entry["match_type"])],
                int(entry["rank"]),
                str(entry["channel"]),
            )
        )
    return sorted(
        merged.values(),
        key=lambda candidate: (
            MATCH_PRIORITY[str(candidate["match_type"])],
            int(candidate["channels"][0]["rank"]) if candidate["channels"] else 0,
            str(candidate["unit_id"]),
        ),
    )


def _hydrate(
    index: Any, candidates: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read every candidate back from the current index before returning it."""

    hydrated: list[dict[str, Any]] = []
    untraceable: list[dict[str, Any]] = []
    for candidate in candidates:
        resolved, reason, detail = _resolve(index, candidate)
        if resolved is None:
            untraceable.append(
                {
                    "unit_id": str(candidate["unit_id"]),
                    "reason": reason,
                    "detail": detail,
                    "match_type": str(candidate["match_type"]),
                    "channels": list(candidate["channels"]),
                    "supports_project_fact": False,
                    "boundary": UNTRACEABLE_BOUNDARY,
                }
            )
            continue
        hydrated.append(resolved)
    return hydrated, untraceable


def _resolve(
    index: Any, candidate: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str, str]:
    unit_id = str(candidate["unit_id"])
    if unit_id.startswith(UNIT_EVIDENCE_PREFIX):
        return _resolve_statement(index, candidate)
    if unit_id.startswith(UNIT_IMAGE_PREFIX):
        return _resolve_image(index, candidate)
    return None, "unknown_unit", f"{unit_id!r} 不是本构建签发过的检索单位。"


def _resolve_statement(
    index: Any, candidate: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str, str]:
    row = index.fetchone(
        """
        SELECT e.id AS evidence_id, e.evidence_type, e.text, e.section_path,
               e.locator, e.authority, b.ordinal AS block_ordinal,
               d.path AS source_document, d.document_type, d.source_sha256,
               d.logical_document_id, d.source_revision_id, d.parse_revision_id
        FROM evidence AS e
        JOIN documents AS d ON d.id = e.document_id
        LEFT JOIN document_blocks AS b
          ON e.source_table = 'document_blocks' AND b.id = e.source_record_id
        WHERE e.id = ?
        """,
        (int(candidate["record_id"]),),
    )
    if row is None:
        return None, "unit_not_in_index", "这个检索单位已不在当前索引里。"
    if (
        candidate.get("recorded_sha256")
        and str(candidate["recorded_sha256"]) != str(row["source_sha256"])
    ):
        return (
            None,
            "source_changed_since_hit",
            "命中的来源与当前索引记录的来源哈希不一致。",
        )
    locator = _json(row["locator"], {})
    section_path = _json(row["section_path"], [])
    if not locator:
        return None, "locator_missing", "这个检索单位没有可用定位。"
    reference = _reference(
        row,
        locator=locator,
        section_path=section_path,
        authority=str(row["authority"]),
    )
    resolved = _resolved_payload(candidate, reference=reference)
    resolved.update(
        {
            "unit_type": str(row["evidence_type"]),
            "text": str(row["text"]),
            "section_path": list(section_path),
            "locator": dict(locator),
            "authority": str(row["authority"]),
            "block_ordinal": row["block_ordinal"],
            "source_document": str(row["source_document"]),
            "document_type": str(row["document_type"]),
        }
    )
    return resolved, "", ""


def _resolve_image(
    index: Any, candidate: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str, str]:
    image_id = int(candidate["record_id"])
    row = index.fetchone(
        """
        SELECT i.id AS image_id, i.relationship_id, i.source_part, i.asset_path,
               i.heading, i.paragraph_index, i.sheet_name, i.cell_anchor,
               i.ocr_status, d.path AS source_document, d.document_type,
               d.source_sha256, d.logical_document_id, d.source_revision_id,
               d.parse_revision_id
        FROM images AS i
        JOIN documents AS d ON d.id = i.document_id
        WHERE i.id = ?
        """,
        (image_id,),
    )
    if row is None:
        return None, "unit_not_in_index", "这张图片已不在当前索引里。"
    if (
        candidate.get("recorded_sha256")
        and str(candidate["recorded_sha256"]) != str(row["source_sha256"])
    ):
        return (
            None,
            "source_changed_since_hit",
            "命中的来源与当前索引记录的来源哈希不一致。",
        )
    locator = _image_locator(row)
    region_index = (candidate.get("regions") or {}).get("region_index")
    if region_index is not None:
        locator = {**locator, "region_index": int(region_index)}
    reference = _reference(
        row,
        locator=locator,
        section_path=[],
        authority="source-image",
        region={"source_part": str(row["source_part"])},
    )
    resolved = _resolved_payload(candidate, reference=reference, asset_name=row["asset_path"])
    resolved.update(
        {
            "unit_type": str(candidate["unit_type"]),
            "section_path": [],
            "locator": locator,
            "authority": "source-image",
            "source_document": str(row["source_document"]),
            "document_type": str(row["document_type"]),
            "asset_path": str(row["asset_path"]),
            "ocr_status": str(row["ocr_status"]),
        }
    )
    if region_index is not None:
        transcription = image_transcription(index, image_id)
        region = _region_at(transcription, int(region_index))
        if region is None:
            return (
                None,
                "region_no_longer_readable",
                "这个区域的转写已不在当前索引里。",
            )
        run = (transcription or {}).get("run") or {}
        resolved["region"] = region
        resolved["text"] = str(region.get("text") or resolved["text"])
        resolved["transcription"] = {
            "engine": run.get("engine"),
            "engine_version": run.get("engine_version"),
            "quality_status": run.get("quality_status"),
            "evidence_state": run.get("evidence_state"),
            "fallback_used": run.get("fallback_used"),
        }
    relation = next(
        (
            entry["relation"]
            for entry in candidate["channels"]
            if isinstance(entry, Mapping) and entry.get("relation")
        ),
        None,
    )
    if relation is not None and not _relation_present(
        image_layout(index, image_id), relation
    ):
        return (
            None,
            "relation_no_longer_readable",
            "这条结构关系已不在当前索引里。",
        )
    return resolved, "", ""


def _region_at(
    transcription: Mapping[str, Any] | None, region_index: int
) -> dict[str, Any] | None:
    for region in (transcription or {}).get("regions") or []:
        if int(region["region_index"]) == region_index:
            return dict(region)
    return None


def _relation_present(
    layout: Mapping[str, Any] | None, relation: Mapping[str, Any]
) -> bool:
    """Whether the relation a hit was built from is still in the index."""

    wanted = (
        str(relation.get("kind") or ""),
        str(relation.get("status") or ""),
        int(relation.get("source_region") or 0),
        int(relation.get("target_region") or 0),
    )
    for item in (layout or {}).get("relations") or []:
        if (
            str(item.get("kind") or ""),
            str(item.get("status") or ""),
            int(item.get("source_region") or 0),
            int(item.get("target_region") or 0),
        ) == wanted:
            return True
    return False


def _resolved_payload(
    candidate: Mapping[str, Any],
    *,
    reference: Mapping[str, Any],
    asset_name: str = "",
) -> dict[str, Any]:
    display = display_locator(
        str(candidate["document_type"]),
        reference.get("locator"),
        section_path=[],
        asset_name=str(asset_name or ""),
    )
    resolved = {
        "unit_id": str(candidate["unit_id"]),
        "namespace": str(candidate["namespace"]),
        "match_type": str(candidate["match_type"]),
        "matched_query": bool(candidate.get("matched_query", True)),
        "evidence_status": str(candidate["evidence_status"]),
        "supports_project_fact": (
            str(candidate["namespace"]) in FACT_NAMESPACES
            and str(candidate["evidence_status"]) != EVIDENCE_STATUS_CANDIDATE
        ),
        "channels": list(candidate["channels"]),
        "source_reference": dict(reference),
        "display_locator": display,
        "traceable": True,
        "v2": hydrated_hit(
            unit_id=str(candidate["unit_id"]),
            unit_type=str(candidate["unit_type"]),
            reference=reference,
            display=display,
            authority=str(reference.get("authority") or ""),
        ),
    }
    if candidate.get("unqualified_transcription"):
        resolved["unqualified_transcription"] = True
    return resolved


def _conflict_scan(
    index: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    document_type: str | None,
    evidence_type: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the other sides of each scoped claim the query did find.

    Comparing only the units a query matched is not enough for the conflict
    rule: the same scoped rule can carry a different value in a document the
    query never matched, and a high ranked sentence must not hide it. The scan
    is deliberately narrow -- same section, same unit type, same value-free
    topic, inside the filters the caller asked for -- and bounded by
    ``CONFLICT_SCAN_LIMIT`` so it can never become a second full search.
    """

    claims: dict[tuple[str, str, str], None] = {}
    known = {str(candidate["unit_id"]) for candidate in candidates}
    for candidate in candidates:
        if str(candidate["namespace"]) not in FACT_NAMESPACES:
            continue
        if not str(candidate["unit_id"]).startswith(UNIT_EVIDENCE_PREFIX):
            continue
        topic = claim_topic(str(candidate.get("text") or ""))
        if not topic:
            continue
        section = " / ".join(str(part) for part in candidate.get("section_path") or [])
        claims[(section, topic, str(candidate["unit_type"]))] = None
    if not claims:
        return [], {
            "hits": [],
            "status": CHANNEL_NO_MATCH,
            "detail": "没有需要核对另一侧的 scoped claim。",
        }

    try:
        rows = index.fetchall(
            """
            SELECT e.id AS evidence_id, e.evidence_type, e.text, e.section_path,
                   e.locator, e.authority, b.ordinal AS block_ordinal,
                   d.path AS source_document, d.document_type, d.source_sha256
            FROM evidence AS e
            JOIN documents AS d ON d.id = e.document_id
            LEFT JOIN document_blocks AS b
              ON e.source_table = 'document_blocks' AND b.id = e.source_record_id
            WHERE (? IS NULL OR d.document_type = ?)
              AND (? IS NULL OR e.evidence_type = ?)
            ORDER BY e.id
            LIMIT ?
            """,
            (
                document_type,
                document_type,
                evidence_type,
                evidence_type,
                CONFLICT_SCAN_LIMIT,
            ),
        )
    except sqlite3.OperationalError as error:
        return [], {
            "hits": [],
            "status": CHANNEL_UNAVAILABLE,
            "reason": "namespace_not_in_index",
            "detail": f"事实表在这个索引里不可读：{error}",
        }

    variant = {
        "text": "",
        "match_type": "lexical",
        "channel": CONFLICT_CHANNEL,
        "reason": "同一 scoped claim 的另一侧证据：未命中查询也必须可见",
        "rule": None,
    }
    hits: list[dict[str, Any]] = []
    for row in rows:
        topic = claim_topic(str(row["text"]))
        if not topic:
            continue
        section = " / ".join(
            str(part) for part in _json(row["section_path"], [])
        )
        if (section, topic, str(row["evidence_type"])) not in claims:
            continue
        unit_id = f"{UNIT_EVIDENCE_PREFIX}{int(row['evidence_id'])}"
        if unit_id in known:
            continue
        known.add(unit_id)
        hits.append(
            _hit(
                variant,
                rank=len(hits) + 1,
                unit_id=unit_id,
                namespace=SOURCE_FACTS,
                unit_type=str(row["evidence_type"]),
                text=str(row["text"]),
                evidence_status=EVIDENCE_STATUS_EXPLICIT,
                source_document=str(row["source_document"]),
                document_type=str(row["document_type"]),
                record_id=int(row["evidence_id"]),
                score=None,
                recorded_sha256=str(row["source_sha256"]),
                matched_query=False,
            )
        )
    detail = (
        f"核对同一 scoped claim 的另一侧：在 {len(rows)} 条候选里补上 "
        f"{len(hits)} 条未命中查询的证据。"
    )
    fused = _fuse(hits)
    return fused, {
        "hits": fused,
        "status": CHANNEL_MATCHED if hits else CHANNEL_NO_MATCH,
        "scanned": len(rows),
        "detail": detail,
    }


def _conflicts(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Structure the disagreements the ordering must not hide.

    Two candidates join one conflict group only when they describe the same
    scoped claim -- same section, same value-free topic -- and differ in a
    value, a unit, a scope, a version or a time. Anything looser is reported as
    a potential conflict candidate and nothing more.
    """

    claims: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        if str(candidate["namespace"]) not in FACT_NAMESPACES:
            continue
        text = str(candidate.get("text") or "")
        topic = claim_topic(text)
        if not topic:
            continue
        section_path = [str(part) for part in candidate.get("section_path") or []]
        section = " / ".join(section_path)
        claims.setdefault((section, str(candidate["unit_type"]), topic), []).append(
            {
                "unit_id": str(candidate["unit_id"]),
                "evidence_id": _evidence_id(str(candidate["unit_id"])),
                "source_document": str(candidate["source_document"]),
                "locator": dict(candidate.get("locator") or {}),
                "text": text,
                "values": claim_values(text),
                "evidence_status": str(candidate["evidence_status"]),
                "retrieved": bool(candidate.get("matched_query", True)),
                "source_reference": dict(candidate.get("source_reference") or {}),
                "display_locator": dict(candidate.get("display_locator") or {}),
                "section_path": section_path,
                # The applicable scope of the claim is its section path; a
                # claim with no section is unscoped and says so with "".
                "scope": section,
                "version": _version_signature(text),
                "time": _time_signature(text),
            }
        )

    groups: list[dict[str, Any]] = []
    potentials: list[dict[str, Any]] = []
    for (section, unit_type, topic), sides in sorted(claims.items()):
        if len(sides) < 2:
            continue
        # Two sides contradict each other only when the comparable dimensions
        # really differ. Scope alone never makes a conflict: the same value in
        # two places is the same value.
        signatures = {_claim_signature(side) for side in sides}
        documents = {side["source_document"] for side in sides}
        texts = {side["text"] for side in sides}
        if len(signatures) < 2:
            # The compared dimensions agree. Identical sentences corroborate
            # each other; only a different wording is a similarity worth
            # showing, and even then it is a candidate, never a conflict.
            if len(texts) < 2:
                continue
            potentials.append(
                _potential(
                    topic=topic,
                    section=section,
                    unit_type=unit_type,
                    sides=sides,
                    reason=(
                        "同一 scoped claim 的两种措辞：值、单位、版本与时间都还没分歧，"
                        "所以只标为候选。"
                    ),
                )
            )
            continue
        if len(documents) < 2:
            potentials.append(
                _potential(
                    topic=topic,
                    section=section,
                    unit_type=unit_type,
                    sides=sides,
                    reason=(
                        "同一个来源里出现两个不同的值：在第二个来源确认之前，"
                        "它只是候选冲突。"
                    ),
                )
            )
            continue
        dimensions = _conflict_dimensions(sides)
        group_id = f"conflict-{digest([section, topic, sorted(signatures)])[:12]}"
        groups.append(
            {
                "type": "conflict_group",
                "conflict_id": group_id,
                "topic": topic,
                "claim": {
                    "section": section,
                    "topic": topic,
                    "evidence_type": unit_type,
                },
                "dimensions": dimensions,
                "sides": [
                    {
                        "unit_id": side["unit_id"],
                        "evidence_id": side["evidence_id"],
                        "source_document": side["source_document"],
                        "locator": side["locator"],
                        "display_locator": side["display_locator"],
                        "text": side["text"],
                        "value": "、".join(
                            f"{value['value']}{value['unit']}" for value in side["values"]
                        ),
                        "unit": _unit_signature(side["values"]),
                        "version": side["version"],
                        "time": side["time"],
                        "evidence_status": side["evidence_status"],
                        "scope": side["scope"],
                        "retrieved": side["retrieved"],
                        "source_reference": side["source_reference"],
                    }
                    for side in sorted(
                        sides,
                        key=lambda side: (
                            side["source_document"],
                            _claim_signature(side),
                        ),
                    )
                ],
                "resolution_state": "unresolved",
                "winner": None,
                "boundary": CONFLICT_BOUNDARY,
            }
        )
    return groups, potentials


def _potential(
    *,
    topic: str,
    section: str,
    unit_type: str,
    sides: Sequence[Mapping[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """A pair of units that look like one claim but are not a conflict yet."""

    return {
        "type": "potential_conflict_candidate",
        "topic": topic,
        "section": section,
        "evidence_type": unit_type,
        "reason": reason,
        "evidence": [
            {
                "unit_id": side["unit_id"],
                "evidence_id": side["evidence_id"],
                "source_document": side["source_document"],
                "text": side["text"],
                "locator": side["locator"],
                "retrieved": side["retrieved"],
            }
            for side in sorted(sides, key=lambda side: side["unit_id"])
        ],
        "boundary": CONFLICT_BOUNDARY,
    }


def _conflict_dimensions(sides: Sequence[Mapping[str, Any]]) -> list[str]:
    """Which dimensions the sides actually disagree in, never assumed.

    The scope is not compared here: two sides only meet in one group when they
    already share a section and a unit type, so their scope is equal by
    construction and each side publishes it for a caller to check.
    """

    dimensions: list[str] = []
    if len({_value_signature(side["values"]) for side in sides}) > 1:
        dimensions.append("value")
    if len({_unit_signature(side["values"]) for side in sides}) > 1:
        dimensions.append("unit")
    if len({str(side["version"]) for side in sides}) > 1:
        dimensions.append("version")
    if len({str(side["time"]) for side in sides}) > 1:
        dimensions.append("time")
    # A group only forms once one of these differs, so this is never empty.
    return dimensions


def _value_signature(values: Sequence[Mapping[str, Any]]) -> str:
    return "、".join(sorted(str(value["value"]) for value in values))


def _unit_signature(values: Sequence[Mapping[str, Any]]) -> str:
    return "、".join(sorted(str(value["unit"]) for value in values))


def _claim_signature(side: Mapping[str, Any]) -> str:
    """Everything a claim compares, so two sides are one claim only if all agree."""

    return "|".join(
        (
            _value_signature(side["values"]),
            _unit_signature(side["values"]),
            str(side["version"]),
            str(side["time"]),
        )
    )


def _version_signature(text: str) -> str:
    return "、".join(sorted({match.group(0) for match in VERSION_PATTERN.finditer(text)}))


def _time_signature(text: str) -> str:
    return "、".join(sorted({match.group(0) for match in TIME_PATTERN.finditer(text)}))


def _explanation_assist(
    index: Any,
    candidates: Sequence[dict[str, Any]],
    *,
    variants: Sequence[Mapping[str, Any]],
    notation: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Let the plain reading of a candidate add a match reason.

    The explanation channel never creates a candidate and never answers: it
    reads the layers this build already publishes for the candidates the other
    channels found, and says when the question also appears in a derived atom.
    """

    terms = [str(variant["text"]) for variant in variants]
    examined = 0
    assisted = 0
    for candidate in candidates[:EXPLANATION_ASSIST_LIMIT]:
        examined += 1
        package = evidence_package(
            index,
            unit_id=str(candidate["unit_id"]),
            sections=("explanation",),
            notation=notation,
        )
        atoms = _package_atoms(package)
        for atom in atoms:
            text = str(atom.get("text") or "")
            section = str(atom.get("section") or "")
            if section == "raw_content" or str(atom.get("wording")) == "verbatim":
                continue
            matched = next((term for term in terms if term and term in text), "")
            if not matched:
                continue
            candidate.setdefault("channels", []).append(
                {
                    "channel": EXPLANATION_NAMESPACE,
                    "match_type": "lexical",
                    "rank": 1,
                    "raw_score": None,
                    "reason": f"该单元的白话释义里出现「{matched}」",
                    "atom": {
                        "atom_id": str(atom.get("atom_id") or ""),
                        "section": section,
                        "statement_kind": str(atom.get("statement_kind") or ""),
                    },
                }
            )
            assisted += 1
            break
    detail = (
        "白话释义只作为匹配理由附在它支持的检索单位上；它不产生新候选。"
        if examined
        else "没有候选需要白话释义辅助。"
    )
    return list(candidates), {
        "status": CHANNEL_MATCHED if assisted else CHANNEL_NO_MATCH,
        "examined": examined,
        "assisted": assisted,
        "detail": detail,
    }


def _package_atoms(package: Mapping[str, Any]) -> list[dict[str, Any]]:
    sections = package.get("sections") or {}
    items = sections.get("explanation") if isinstance(sections, Mapping) else None
    atoms: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        explanation = item.get("explanation") or {}
        for atom in explanation.get("atoms") or []:
            if isinstance(atom, Mapping):
                atoms.append(dict(atom))
    return atoms


def _namespace_report(
    candidates: Sequence[Mapping[str, Any]],
    untraceable: Sequence[Mapping[str, Any]],
    reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    served = {str(candidate["namespace"]) for candidate in candidates}
    counts: dict[str, int] = {}
    for candidate in candidates:
        namespace = str(candidate["namespace"])
        counts[namespace] = counts.get(namespace, 0) + 1
    report: dict[str, Any] = {}
    for namespace in NAMESPACES:
        report[namespace] = {
            "status": CHANNEL_MATCHED if namespace in served else CHANNEL_NO_MATCH,
            "returned": counts.get(namespace, 0),
            "facts": namespace in FACT_NAMESPACES,
        }
    report[UNCONFIRMED_CANDIDATES]["boundary"] = EXPLORATION_BOUNDARY
    report[EXPLANATION_NAMESPACE] = {
        "status": "assist_only",
        "returned": 0,
        "facts": False,
        "creates_candidates": False,
        "detail": "白话释义只提供匹配理由，命中后回到它支持的检索单位。",
    }
    report["untraceable_candidates"] = {
        "status": CHANNEL_MATCHED if untraceable else CHANNEL_NO_MATCH,
        "returned": len(untraceable),
        "facts": False,
        "boundary": UNTRACEABLE_BOUNDARY,
    }
    return report


def _state(
    *,
    facts: Sequence[Mapping[str, Any]],
    untraceable: Sequence[Mapping[str, Any]],
    reports: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    mode: str,
    degradations: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The one state this response is in, most serious first.

    A capability that could not run is reported before the absence of a
    candidate: while a channel is missing, "nothing was found" is not something
    this build can claim, because the missing channel is not ruled out as the
    reason.
    """

    # A channel that could not run degrades the answer. The vector channel is
    # only a degradation when the caller asked for it: ``auto`` never required
    # a capability this build does not have.
    unmet = [
        report
        for report in reports
        if str(report["status"]) in {CHANNEL_UNAVAILABLE, CHANNEL_FAILED}
        or (
            str(report["channel"]) == SEMANTIC_CHANNEL
            and str(report["status"]) == CHANNEL_NOT_CONFIGURED
            and mode in VECTOR_MODES
        )
    ]
    if unmet or degradations:
        return "degraded"
    if not facts:
        return "not_found"
    if groups:
        return "ambiguous"
    if untraceable:
        return "partial"
    return "found"


def _kind(mode: str) -> dict[str, Any]:
    """What ran, what was asked for, and what that means for the caller."""

    if mode == "lexical":
        return {"effective": "lexical", "vector": _vector_detail("not_requested"), "degradation_events": []}
    if mode == "auto":
        return {"effective": "auto", "vector": _vector_detail("not_configured"), "degradation_events": []}
    events = [
        {
            "channel": SEMANTIC_CHANNEL,
            "requested_mode": mode,
            "effective_mode": "auto",
            "reason": "vector_capability_not_configured",
            "detail": (
                "本构建没有 embedding 模型与向量索引，请求的模式降级为确定性通道；"
                "事实库与 FTS5 不受影响。"
            ),
        }
    ]
    return {"effective": "auto", "vector": _vector_detail("not_configured"), "degradation_events": events}


def _vector_detail(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "provider": None,
        "model": None,
        "index_version": None,
        "namespace": "semantic_candidate",
    }


def _capability_event(
    mode: str, event: Mapping[str, Any], effective: str
) -> dict[str, Any]:
    """One capability the caller had to do without, named and explained.

    The caller reports what it could not supply; this build only fills in the
    mode it actually ran, so the two can never disagree about what happened.
    """

    reported = dict(event)
    reported.setdefault("channel", "capability")
    reported.setdefault("reason", "capability_unavailable")
    reported["requested_mode"] = mode
    reported["effective_mode"] = effective
    return reported


def _evidence_shape(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The V1-shaped view of one fact candidate, plus what V2 adds."""

    item: dict[str, Any] = {
        "source_document": str(candidate.get("source_document") or ""),
        "document_type": str(candidate.get("document_type") or ""),
        "evidence_type": str(candidate.get("unit_type") or ""),
        "text": str(candidate.get("text") or ""),
        "section_path": list(candidate.get("section_path") or []),
        "locator": dict(candidate.get("locator") or {}),
        "score": _best_score(candidate),
        "unit_id": str(candidate["unit_id"]),
        "namespace": str(candidate["namespace"]),
        "match_type": str(candidate["match_type"]),
        "evidence_status": str(candidate["evidence_status"]),
        "supports_project_fact": bool(candidate.get("supports_project_fact")),
        "channels": list(candidate.get("channels") or []),
        "v2": dict(candidate.get("v2") or {}),
    }
    if str(candidate["unit_id"]).startswith(UNIT_EVIDENCE_PREFIX):
        item["evidence_id"] = _evidence_id(str(candidate["unit_id"]))
        item["block_ordinal"] = candidate.get("block_ordinal")
    if candidate.get("asset_path"):
        item["asset_path"] = str(candidate["asset_path"])
    if candidate.get("transcription"):
        item["transcription"] = dict(candidate["transcription"])
    if candidate.get("region"):
        item["region"] = dict(candidate["region"])
    return item


def _best_score(candidate: Mapping[str, Any]) -> float:
    """The strongest raw score any channel gave this candidate.

    Scores from different channels are never added together, so this is the
    best one alone; the per-channel scores stay in ``channels``.
    """

    scores = [
        float(entry["raw_score"])
        for entry in candidate.get("channels") or ()
        if entry.get("raw_score") is not None
    ]
    return min(scores) if scores else 0.0


def _failed(query: str, mode: str, error: BaseException) -> dict[str, Any]:
    return {
        "status": "failed",
        "schema_version": RETRIEVAL_VERSION,
        "query": query,
        "mode": {"requested": mode, "effective": "failed"},
        "expansions": [],
        "namespaces": {
            namespace: {"status": CHANNEL_FAILED, "returned": 0, "facts": namespace in FACT_NAMESPACES}
            for namespace in NAMESPACES
        },
        "channels": [
            {"channel": channel, "status": CHANNEL_FAILED, "hits": 0, "detail": str(error)}
            for channel in CHANNELS
        ],
        "candidates": [],
        "evidence": [],
        "untraceable": [],
        "conflicts": [],
        "conflict_groups": [],
        "retrieval": {"candidate_count": 0, "returned": 0, "boundary": RETRIEVAL_BOUNDARY},
        "response_meta": {
            "retrieval_state": "failed",
            "requested_mode": mode,
            "effective_mode": "failed",
            "channels": {channel: CHANNEL_FAILED for channel in CHANNELS},
            "vector": _vector_detail("not_configured"),
            "degradation_events": [
                {
                    "channel": "index",
                    "reason": "index_unreadable",
                    "detail": str(error),
                }
            ],
            "rebuild_vector_index_recommended": False,
            "expansions": 0,
            "boundary": DEGRADATION_BOUNDARY,
        },
        "limitations": [f"无法读取当前索引：{error}"],
        "boundary": RETRIEVAL_BOUNDARY,
    }


def _channel_report(
    channel: str,
    result: Mapping[str, Any],
    *,
    detail: str,
    expansions: int = 0,
) -> dict[str, Any]:
    report = {
        "channel": channel,
        "status": str(result.get("status") or CHANNEL_NO_MATCH),
        "hits": len(result.get("hits") or ()),
        "detail": str(result.get("detail") or detail),
    }
    if result.get("reason"):
        report["reason"] = str(result["reason"])
    if result.get("scanned") is not None:
        report["scanned"] = int(result["scanned"])
    if result.get("reached"):
        report["reached"] = list(result["reached"])
    if expansions:
        report["expansions"] = expansions
    return report


def _phrase(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _evidence_id(unit_id: str) -> int | None:
    """The row a statement unit points at, or ``None`` for a non-statement."""

    if not unit_id.startswith(UNIT_EVIDENCE_PREFIX):
        return None
    return int(unit_id[len(UNIT_EVIDENCE_PREFIX) :])


def _first_line(*values: str) -> str:
    for value in values:
        if str(value or "").strip():
            return str(value)
    return ""


def _image_locator(row: Mapping[str, Any]) -> dict[str, Any]:
    locator: dict[str, Any] = {"relationship_id": str(row["relationship_id"])}
    if row["sheet_name"]:
        locator["sheet_name"] = str(row["sheet_name"])
    if row["cell_anchor"]:
        locator["cell_anchor"] = str(row["cell_anchor"])
    if row["heading"]:
        locator["heading"] = str(row["heading"])
    if row["paragraph_index"] is not None:
        locator["paragraph_index"] = int(row["paragraph_index"])
    if not row["sheet_name"] and not row["heading"]:
        locator["source_part"] = str(row["source_part"])
    return locator


def _reference(
    row: Mapping[str, Any],
    *,
    locator: Mapping[str, Any],
    section_path: Sequence[Any],
    authority: str,
    region: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return source_reference(
        logical_document_id=row["logical_document_id"],
        source_revision_id=row["source_revision_id"],
        parse_revision_id=row["parse_revision_id"],
        path=str(row["source_document"]),
        document_type=str(row["document_type"]),
        source_sha256=str(row["source_sha256"]),
        locator=locator,
        section_path=section_path,
        authority=authority,
        region=region,
    )


def _json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "CANDIDATE_CHANNEL",
    "CHANNELS",
    "CONFLICT_BOUNDARY",
    "CONFLICT_CHANNEL",
    "CONFLICT_DIMENSIONS",
    "CONFLICT_SCAN_BOUNDARY",
    "CONFLICT_SCAN_LIMIT",
    "DEFAULT_LIMIT",
    "DEFAULT_MODE",
    "DEGRADATION_BOUNDARY",
    "EVIDENCE_STATUSES",
    "EXPLANATION_NAMESPACE",
    "EXPLORATION_BOUNDARY",
    "FACT_NAMESPACES",
    "FUSION_BOUNDARY",
    "IMAGE_TRANSCRIPTION",
    "MATCH_PRIORITY",
    "MATCH_TYPES",
    "MAX_LIMIT",
    "NAMESPACES",
    "RESPONSE_STATES",
    "RETRIEVAL_BOUNDARY",
    "RETRIEVAL_MODES",
    "RETRIEVAL_VERSION",
    "RetrievalError",
    "SOURCE_FACTS",
    "UNCONFIRMED_CANDIDATES",
    "UNTRACEABLE_BOUNDARY",
    "VISUAL_INTERPRETATION",
    "retrieve",
]
