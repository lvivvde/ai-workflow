"""Designer Notation Dictionary: what a token means, in one scope, on the record.

A game-design document is full of shorthand -- an arrow in a flow chart, a brace
that groups a branch, a legend in the corner of one picture, a column heading
that only makes sense inside one spreadsheet. Reading that shorthand is a human
decision *about the project*, so it lives in Durable Project State beside the
confirmed aliases, never in the derived index and never in the source document.

Three rules shape this module:

* A confirmed meaning is scoped. One region of one picture, one document, one
  document type, or the whole project -- and nothing wider by accident.
* A reading the index merely proposes is a *candidate*. Candidates are reported
  apart from confirmed entries and may never be cited as a project fact.
* Nothing here edits source evidence. Correcting a meaning appends a new one and
  keeps the old value, its review event, and the raw transcription intact.

The module owns the vocabulary and the read-side questions (does this token mean
anything here, what is the highest-authority source for it, which confirmed
entry went stale on a new revision). ``review.py`` owns the writes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence
import uuid


NOTATION_DICTIONARY_VERSION = "designer-notation-v1"
NOTATION_DICTIONARY_NAME = "notation.json"
NOTATION_ENTRY_VERSION = 1
CANDIDATE_ID_PREFIX = "candidate:"

PROJECT_SCOPE = "project"
DOCUMENT_TYPE_SCOPE = "document_type"
DOCUMENT_SCOPE = "document"
REGION_SCOPE = "region"
SCOPE_KINDS: tuple[str, ...] = (
    PROJECT_SCOPE,
    DOCUMENT_TYPE_SCOPE,
    DOCUMENT_SCOPE,
    REGION_SCOPE,
)

REGION_LEGEND = "region_legend"
DOCUMENT_DEFINITION = "document_definition"
DOCUMENT_TYPE_DEFINITION = "document_type_definition"
PROJECT_DICTIONARY = "project_dictionary"
CANDIDATE_INTERPRETATION = "candidate_interpretation"
EXTERNAL_COMMON_KNOWLEDGE = "external_common_knowledge"

# Highest authority first. A confirmed entry can only speak from the first three
# layers; the rest are proposals or knowledge that did not come from the project.
AUTHORITY_LAYERS: tuple[str, ...] = (
    REGION_LEGEND,
    DOCUMENT_DEFINITION,
    DOCUMENT_TYPE_DEFINITION,
    PROJECT_DICTIONARY,
    CANDIDATE_INTERPRETATION,
    EXTERNAL_COMMON_KNOWLEDGE,
)
# A person may speak for the project itself, so the project dictionary is a
# confirmed layer too. Only the candidate and outside layers stay provisional.
CONFIRMED_AUTHORITIES: tuple[str, ...] = AUTHORITY_LAYERS[:4]
SCOPE_AUTHORITY: dict[str, str] = {
    REGION_SCOPE: REGION_LEGEND,
    DOCUMENT_SCOPE: DOCUMENT_DEFINITION,
    DOCUMENT_TYPE_SCOPE: DOCUMENT_TYPE_DEFINITION,
    PROJECT_SCOPE: PROJECT_DICTIONARY,
}

CONFIRMED = "confirmed"
REJECTED = "rejected"
SUPERSEDED = "superseded"
ENTRY_STATUSES: tuple[str, ...] = (CONFIRMED, REJECTED, SUPERSEDED)

# Review-action names, as they appear in the append-only journal.
CONFIRM_ACTION = "confirm"
CORRECT_ACTION = "correct"
REJECT_ACTION = "reject"
IGNORE_ACTION = "ignore"
RESOLVE_CONFLICT_ACTION = "resolve_conflict"

AUTHORITY_RESOLUTION = "authority"
HUMAN_CHOICE_RESOLUTION = "human_choice"
RESOLUTION_KINDS: tuple[str, ...] = (AUTHORITY_RESOLUTION, HUMAN_CHOICE_RESOLUTION)

RESOLVED = "resolved"
CANDIDATE_ONLY = "candidate_only"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
QUARANTINED = "quarantined"
RESOLUTION_STATUSES: tuple[str, ...] = (
    RESOLVED,
    CANDIDATE_ONLY,
    AMBIGUOUS,
    NOT_FOUND,
    QUARANTINED,
)

EXCLUSION_SCOPE = "scope_mismatch"
EXCLUSION_NEWER_REVISION = "newer_parse_revision"
EXCLUSION_EXPIRED = "outside_validity"
EXCLUSION_REJECTED = "rejected_by_review"
EXCLUSION_SUPERSEDED = "superseded"

REVIEW_EVENT_SUBJECT_ENTRY = "notation_entry"
REVIEW_EVENT_SUBJECT_CANDIDATE = "notation_candidate"

BOUNDARY = (
    "A confirmed notation meaning is a human decision kept in Durable Project "
    "State. It records how a token was read in one scope; it never rewrites the "
    "source document, its transcription, the visual regions, or the derived "
    "index."
)
CANDIDATE_BOUNDARY = (
    "A notation candidate is a proposal from the derived index. It carries its "
    "own geometry or OCR basis and its claim boundary, it is not in the "
    "dictionary, and it may never be cited as a project fact until a person "
    "confirms it through a review action."
)
EXTERNAL_BOUNDARY = (
    "External common knowledge is quarantined: it is reported next to the "
    "project's own readings so a caller can see it was offered, but it is never "
    "merged with a confirmed meaning and never becomes the resolution."
)
MIGRATION_BOUNDARY = (
    "A confirmed meaning bound to an older parse revision does not carry over by "
    "itself. On a newer revision the binding goes stale, the reading is reported "
    "as a migration candidate, and a new explicit review action is required "
    "before it applies again."
)


def empty_notation_dictionary() -> dict[str, Any]:
    """The dictionary before anyone has confirmed a meaning."""

    return {
        "entry_schema_version": NOTATION_ENTRY_VERSION,
        "dictionary_version": NOTATION_DICTIONARY_VERSION,
        "updated_at": None,
        "entries": [],
    }


def new_entry_id() -> str:
    return f"nota-{uuid.uuid4().hex[:16]}"


def normalize_document_path(value: Any) -> str:
    """A project-relative document path, or a refusal."""

    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("a notation scope needs a document path")
    if text.startswith("/") or "://" in text or re.match(r"^[A-Za-z]:", text):
        raise ValueError(
            f"a notation scope takes a project-relative path, not {value!r}"
        )
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"a notation scope may not leave the project root: {value!r}")
    if not parts:
        raise ValueError(f"a notation scope needs a real path: {value!r}")
    return "/".join(parts)


def normalize_scope(
    kind: Any,
    value: Any = None,
    *,
    document: Any = None,
    region: Any = None,
) -> dict[str, str]:
    """Normalise one scope into the exact shape stored in the dictionary."""

    name = str(kind or "").strip().lower()
    if name not in SCOPE_KINDS:
        raise ValueError(
            f"scope_kind must be one of {list(SCOPE_KINDS)}, not {kind!r}"
        )
    if name == PROJECT_SCOPE:
        return {"kind": PROJECT_SCOPE, "value": "*"}
    if name == DOCUMENT_TYPE_SCOPE:
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError("a document_type scope needs a type, for example 'docx'")
        if any(separator in text for separator in ("/", "\\")):
            raise ValueError(f"a document_type scope is a type, not a path: {value!r}")
        return {"kind": DOCUMENT_TYPE_SCOPE, "value": text}
    if name == DOCUMENT_SCOPE:
        return {
            "kind": DOCUMENT_SCOPE,
            "value": normalize_document_path(value or document),
        }

    raw = str(value or "").strip()
    label = str(region or "").strip()
    source = document
    if raw and not source and "#" in raw:
        head, _, tail = raw.rpartition("#")
        source = head
        label = label or tail
    if not label and raw and not source:
        raise ValueError("a region scope needs a region label, for example 'region-3'")
    document_path = normalize_document_path(source or raw)
    if not label:
        raise ValueError("a region scope needs a region label, for example 'region-3'")
    if "#" in label:
        raise ValueError(f"a region label may not contain '#': {region!r}")
    return {
        "kind": REGION_SCOPE,
        "value": f"{document_path}#{label}",
        "document": document_path,
        "region": label,
    }


def scope_document(scope: Mapping[str, Any]) -> str:
    """The document a scope is pinned to, or an empty string for wider scopes."""

    if str(scope.get("kind")) == REGION_SCOPE:
        document = str(scope.get("document") or "")
        if document:
            return document
        return str(scope.get("value") or "").rpartition("#")[0]
    if str(scope.get("kind")) == DOCUMENT_SCOPE:
        return str(scope.get("value") or "")
    return ""


def scope_region(scope: Mapping[str, Any]) -> str:
    if str(scope.get("kind")) != REGION_SCOPE:
        return ""
    label = str(scope.get("region") or "")
    if label:
        return label
    return str(scope.get("value") or "").rpartition("#")[2]


def scope_key(scope: Mapping[str, Any]) -> str:
    return f"{scope.get('kind')}:{scope.get('value')}"


def layer_for_scope(scope: Mapping[str, Any]) -> str:
    return SCOPE_AUTHORITY.get(str(scope.get("kind")), DOCUMENT_DEFINITION)


def scope_covers(
    scope: Mapping[str, Any],
    *,
    document: str,
    document_type: str = "",
    region: str = "",
) -> bool:
    """Whether one scope reaches an exact ``(document, type, region)`` question.

    Matching is deliberately literal: a meaning confirmed for one region never
    answers a question about another region, another document, or another
    document type. Wider scopes only answer the wider question they name.
    """

    kind = str(scope.get("kind"))
    if kind == PROJECT_SCOPE:
        return True
    if kind == DOCUMENT_TYPE_SCOPE:
        return bool(document_type) and str(scope.get("value")) == str(document_type)
    if kind == DOCUMENT_SCOPE:
        return str(scope.get("value")) == str(document)
    if kind == REGION_SCOPE:
        if not region:
            return False
        return (
            scope_document(scope) == str(document)
            and scope_region(scope) == str(region)
        )
    return False


def validate_entry(entry: Any) -> None:
    """Refuse a notation entry that could not be audited later."""

    if not isinstance(entry, Mapping):
        raise ValueError("Every notation entry must be a JSON object")
    for key in ("entry_id", "notation_token", "meaning"):
        if not str(entry.get(key) or "").strip():
            raise ValueError(f"A notation entry needs {key}")
    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("A notation entry needs a scope object")
    if str(scope.get("kind")) not in SCOPE_KINDS:
        raise ValueError(
            f"A notation entry scope must use one of {list(SCOPE_KINDS)}"
        )
    if not str(scope.get("value") or "").strip():
        raise ValueError("A notation entry scope needs a value")
    if str(scope.get("kind")) in (DOCUMENT_SCOPE, REGION_SCOPE):
        normalize_document_path(scope_document(scope))
        if str(scope.get("kind")) == REGION_SCOPE and not scope_region(scope):
            raise ValueError("A region scope needs a region label")
    status = str(entry.get("status") or "")
    if status not in ENTRY_STATUSES:
        raise ValueError(
            f"A notation entry status must be one of {list(ENTRY_STATUSES)}, "
            f"not {status!r}"
        )
    authority = str(entry.get("authority") or "")
    if authority not in CONFIRMED_AUTHORITIES:
        raise ValueError(
            f"A confirmed notation entry may only speak from "
            f"{list(CONFIRMED_AUTHORITIES)}, not {authority!r}"
        )
    for key in ("confirmed_by", "confirmed_at"):
        if not str(entry.get(key) or "").strip():
            raise ValueError(
                f"A notation entry needs {key}; the MCP server never confirms a "
                "meaning by itself"
            )
    version = entry.get("version") or {}
    if not isinstance(version, Mapping):
        raise ValueError("A notation entry version must be a JSON object")
    resolution = entry.get("resolution")
    if resolution is not None:
        if not isinstance(resolution, Mapping):
            raise ValueError("A notation entry resolution must be a JSON object")
        if str(resolution.get("kind")) not in RESOLUTION_KINDS:
            raise ValueError(
                f"A conflict resolution must be one of {list(RESOLUTION_KINDS)}"
            )
        if not str(resolution.get("winner_entry_id") or "").strip():
            raise ValueError("A conflict resolution needs a winning entry")


def build_entry(
    *,
    notation_token: str,
    meaning: str,
    scope: Mapping[str, Any],
    authority: str = "",
    version: Mapping[str, Any] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    basis: Mapping[str, Any] | None = None,
    reason: str = "",
    actor: str = "",
    occurred_at: str,
    entry_id: str | None = None,
    deferred_id: bool = False,
    supersedes: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """One confirmed entry, in the shape ``validate_entry`` accepts.

    ``deferred_id`` builds the entry a review plan *would* write: the identifier
    is minted when the action is applied, so a preview and the apply that follows
    it describe the same decision instead of two random identifiers.
    """

    token = str(notation_token or "").strip()
    if not token:
        raise ValueError("a notation entry needs the token it explains")
    text = str(meaning or "").strip()
    if not text:
        raise ValueError("a notation entry needs a meaning")
    speaker = str(authority or "").strip().lower() or layer_for_scope(scope)
    if speaker not in CONFIRMED_AUTHORITIES:
        raise ValueError(
            f"a confirmed notation entry may only speak from "
            f"{list(CONFIRMED_AUTHORITIES)}, not {authority!r}"
        )
    if not str(actor or "").strip():
        raise ValueError("a notation entry needs the person who confirmed it")
    entry = {
        "record_type": "notation_entry",
        "entry_schema_version": NOTATION_ENTRY_VERSION,
        "entry_id": "" if deferred_id else str(entry_id or new_entry_id()),
        "notation_token": token,
        "meaning": text,
        "scope": dict(scope),
        "authority": speaker,
        "status": CONFIRMED,
        "version": dict(version or {}),
        "valid_from": str(valid_from) if valid_from else None,
        "valid_until": str(valid_until) if valid_until else None,
        "basis": dict(basis or {}),
        "reason": str(reason or ""),
        "note": str(note or ""),
        "supersedes": str(supersedes) if supersedes else None,
        "superseded_by": None,
        "resolution": None,
        "confirmed_by": str(actor).strip(),
        "confirmed_at": str(occurred_at),
        "updated_at": str(occurred_at),
    }
    if not deferred_id:
        validate_entry(entry)
    return entry


def live_entries(dictionary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Confirmed entries -- the only ones allowed to speak for the project."""

    return [
        dict(entry)
        for entry in dictionary.get("entries") or []
        if str(entry.get("status")) == CONFIRMED
    ]


def entry_by_id(dictionary: Mapping[str, Any], entry_id: str) -> dict[str, Any] | None:
    for entry in dictionary.get("entries") or []:
        if str(entry.get("entry_id")) == str(entry_id):
            return dict(entry)
    return None


def history_of(dictionary: Mapping[str, Any], entry_id: str) -> list[dict[str, Any]]:
    """The chain a corrected or rejected meaning left behind, oldest first."""

    entries = {
        str(entry.get("entry_id")): dict(entry)
        for entry in dictionary.get("entries") or []
    }
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = entries.get(str(entry_id))
    while cursor is not None and cursor["entry_id"] not in seen:
        seen.add(cursor["entry_id"])
        chain.append(cursor)
        parent = cursor.get("supersedes")
        cursor = entries.get(str(parent)) if parent else None
    chain.reverse()
    newest = chain[-1]["entry_id"] if chain else ""
    seen.clear()
    cursor = entries.get(newest)
    while cursor is not None and cursor["entry_id"] not in seen:
        seen.add(cursor["entry_id"])
        child = cursor.get("superseded_by")
        cursor = entries.get(str(child)) if child else None
        if cursor is not None:
            chain.append(cursor)
    return chain


def _event_key(event: Any) -> tuple[str, str]:
    return (str(event.action), str(event.subject_id))


def suppressed_candidate_ids(events: Iterable[Any]) -> tuple[dict[str, str], set[str]]:
    """Which candidates a person already rejected or silenced.

    Rejection and silence are different promises: a rejection says the reading
    is wrong, ignoring says only that the same proposal need not be raised
    again. Both live in the append-only journal, and neither touches the index.
    """

    rejected: dict[str, str] = {}
    ignored: set[str] = set()
    for event in events:
        if str(event.subject_type) != REVIEW_EVENT_SUBJECT_CANDIDATE:
            continue
        subject = str(event.subject_id)
        if str(event.action) == REJECT_ACTION:
            rejected[subject] = str(event.event_id)
        elif str(event.action) == IGNORE_ACTION:
            ignored.add(subject)
    return rejected, ignored


def candidate_id(image_id: int, relation_id: int) -> str:
    return f"{CANDIDATE_ID_PREFIX}{image_id}:{relation_id}"


def candidate_subjects(candidate: Mapping[str, Any]) -> list[str]:
    """Every name a review event may use for one candidate.

    A person can point at the exact relation, or reject "this reading in this
    scope" without having a candidate id to hand. Both are the same decision, so
    both keys are checked against the journal.
    """

    subjects = [str(candidate.get("candidate_id") or "")]
    token = str(candidate.get("notation_token") or "")
    scope = candidate.get("scope") or {}
    if token and scope:
        subjects.append(f"{token}@{scope_key(scope)}")
    return [subject for subject in subjects if subject]


def mark_suppressed(
    candidate: dict[str, Any],
    rejected: Mapping[str, str],
    ignored: Iterable[str],
) -> dict[str, Any]:
    """Label a derived candidate with the review events that already cover it."""

    ignored_keys = set(ignored)
    for subject in candidate_subjects(candidate):
        if subject in rejected:
            candidate["rejected"] = True
            candidate["rejected_event_id"] = rejected[subject]
        if subject in ignored_keys:
            candidate["ignored"] = True
    if candidate.get("rejected") or candidate.get("ignored"):
        candidate["supports_project_fact"] = False
    return candidate


CANDIDATE_QUERY = """
    SELECT r.id AS relation_id, r.kind, r.status, r.source_region,
           r.target_region, r.via_regions, r.direction, r.geometry_basis, r.detail,
           r.uncertainty, r.rule_version, r.claim_boundary,
           r.geometry_confidence, r.ocr_confidence,
           i.id AS image_id, d.path AS document_path, d.document_type,
           d.source_sha256, d.logical_document_id, d.source_revision_id,
           d.parse_revision_id
    FROM structural_relations AS r
    JOIN images AS i ON i.id = r.image_id
    JOIN documents AS d ON d.id = i.document_id
    ORDER BY r.id
"""


def candidates_from_index(
    index: Any,
    *,
    project_root: Any = None,
    document: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Notation readings the index proposes but no person has confirmed.

    Only relations the layout engine itself left as candidates are reported:
    a relation the ruleset could confirm is machine-supported structure, not a
    disputed reading, and it is already available through the evidence package.
    """

    if index is None:
        return []
    try:
        rows = index.fetchall(CANDIDATE_QUERY)
    except sqlite3.OperationalError:
        return []
    wanted = normalize_document_path(document) if document else ""
    entries: list[dict[str, Any]] = []
    for row in rows:
        if str(row["status"]) != "candidate":
            continue
        path = document_path_of(index, row["document_path"], project_root)
        if not path:
            continue
        if wanted and path != wanted:
            continue
        entries.append(_candidate_payload(row, path=path))
        if len(entries) >= max(1, int(limit)):
            break
    return entries


def document_path_of(
    index: Any, stored_path: Any, project_root: Any = None
) -> str:
    """A document's path as the project sees it, from the path the index stored.

    The index keeps paths relative to itself, so a caller that speaks in
    project-relative paths -- which is what a dictionary scope uses -- needs the
    two translated at the one place they meet.
    """

    stored = Path(str(stored_path or ""))
    if project_root is None:
        if stored.is_absolute():
            return ""
        try:
            return normalize_document_path(stored.as_posix())
        except ValueError:
            return ""
    resolver = getattr(index, "resolve_source_path", None)
    resolved = resolver(str(stored)) if callable(resolver) else stored
    if not resolved.is_absolute():
        resolved = (Path(project_root) / resolved).resolve()
    try:
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return ""


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value]
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return list(decoded) if isinstance(decoded, list) else []


def _candidate_payload(row: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    anchor = row["source_region"]
    if anchor is None or int(anchor) < 0:
        passed = _json_list(row["via_regions"])
        anchor = passed[0] if passed else anchor
    region = f"region-{anchor}"
    basis = {
        "source_reference": {
            "path": path,
            "document_type": str(row["document_type"] or ""),
            "logical_document_id": str(row["logical_document_id"] or ""),
            "source_revision_id": str(row["source_revision_id"] or ""),
            "parse_revision_id": str(row["parse_revision_id"] or ""),
            "source_sha256": str(row["source_sha256"] or ""),
            "region": {
                "region_index": row["source_region"],
                "label": region,
            },
            "authority": "derived",
        },
        "relation_id": row["relation_id"],
        "image_id": row["image_id"],
        "target_region": row["target_region"],
        "via_regions": _json_list(row["via_regions"]),
        "rule_version": str(row["rule_version"] or ""),
        "geometry_basis": str(row["geometry_basis"] or ""),
        "geometry_confidence": row["geometry_confidence"],
        "ocr_confidence": row["ocr_confidence"],
        "detail": str(row["detail"] or ""),
    }
    return {
        "candidate_id": candidate_id(int(row["image_id"]), int(row["relation_id"])),
        "kind": "notation_candidate",
        "status": "candidate",
        "notation_token": str(row["kind"]),
        "meaning": "",
        "document": path,
        "document_type": str(row["document_type"] or ""),
        "region": region,
        "scope": {"kind": REGION_SCOPE, "value": f"{path}#{region}"},
        "direction": str(row["direction"] or ""),
        "uncertainty": str(row["uncertainty"] or ""),
        "claim_boundary": str(row["claim_boundary"] or ""),
        "basis": basis,
        "authority": CANDIDATE_INTERPRETATION,
        "supports_project_fact": False,
        "requires_review_action": True,
        "boundary": CANDIDATE_BOUNDARY,
    }


def active_parse_revision_for(state: Any, document: str) -> str:
    """The parse revision reads currently use for one project document."""

    wanted = normalize_document_path(document)
    try:
        records = state.documents()
    except Exception:  # pragma: no cover - unreadable state is reported by callers
        return ""
    for record in records.values():
        if normalize_document_path(record.relative_path) == wanted:
            return str(record.active_parse_revision or "")
    return ""


def migration_candidates(
    state: Any,
    *,
    document: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Confirmed meanings whose revision binding went stale.

    A human decision was made about one parse revision of one document. When a
    newer revision is active, that decision does not silently follow: the entry
    is reported here as a migration candidate and waits for a new explicit
    review action.
    """

    dictionary = state.read_notation()
    wanted = normalize_document_path(document) if document else ""
    entries: list[dict[str, Any]] = []
    for entry in live_entries(dictionary):
        bound = str((entry.get("version") or {}).get("parse_revision_id") or "")
        document_path = scope_document(entry.get("scope") or {})
        if not bound or not document_path:
            continue
        if wanted and document_path != wanted:
            continue
        record = entry.get("version") or {}
        active = str(record.get("active_parse_revision") or "")
        if not active:
            active = active_parse_revision_for(state, document_path)
        if not active or active == bound:
            continue
        entries.append(
            {
                "candidate_id": f"migration:{entry['entry_id']}:{active}",
                "kind": "review_migration_candidate",
                "status": "migration_candidate",
                "entry_id": entry["entry_id"],
                "notation_token": entry["notation_token"],
                "meaning": entry["meaning"],
                "authority": entry["authority"],
                "scope": dict(entry["scope"]),
                "document": document_path,
                "from_parse_revision": bound,
                "from_source_revision": str(record.get("source_revision_id") or ""),
                "to_parse_revision": active,
                "confirmed_by": entry.get("confirmed_by"),
                "confirmed_at": entry.get("confirmed_at"),
                "requires_review_event": True,
                "auto_applied": False,
                "supports_project_fact": False,
                "boundary": MIGRATION_BOUNDARY,
            }
        )
        if len(entries) >= max(1, int(limit)):
            break
    return entries


def _version_state(
    entry: Mapping[str, Any],
    *,
    parse_revision_id: str,
    document: str,
) -> str:
    bound = str((entry.get("version") or {}).get("parse_revision_id") or "")
    if not bound:
        return ""
    if not parse_revision_id:
        return EXCLUSION_NEWER_REVISION
    if bound != str(parse_revision_id):
        return EXCLUSION_NEWER_REVISION
    if scope_document(entry.get("scope") or {}) not in ("", document):
        return EXCLUSION_NEWER_REVISION
    return ""


def _validity_state(entry: Mapping[str, Any], at: str) -> str:
    start = str(entry.get("valid_from") or "")
    end = str(entry.get("valid_until") or "")
    if start and at < start:
        return EXCLUSION_EXPIRED
    if end and at > end:
        return EXCLUSION_EXPIRED
    return ""


def _resolution_of(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    ids = {str(entry.get("entry_id")) for entry in entries}
    for entry in entries:
        resolution = entry.get("resolution")
        if isinstance(resolution, Mapping) and str(
            resolution.get("winner_entry_id")
        ) in ids:
            return resolution
    return None


def resolve(
    state: Any,
    index: Any = None,
    *,
    notation_token: str,
    document: str,
    document_type: str = "",
    region: str = "",
    parse_revision_id: str = "",
    at: str = "",
    external_common_knowledge: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Read one token in one place, honouring scope, revision, and authority.

    The layers are consulted highest authority first, and the first layer that
    holds a live confirmed meaning answers the question. Independent of that,
    the answer reports what stayed out: entries bound to an older revision
    (migration candidates), candidates, and quarantined external knowledge.
    """

    token = str(notation_token or "").strip()
    if not token:
        raise ValueError("resolve_notation needs the notation token to look up")
    path = normalize_document_path(document)
    active = str(parse_revision_id or "").strip() or active_parse_revision_for(
        state, path
    )
    moment = str(at or "").strip() or _now()
    dictionary = state.read_notation()
    events = state.review_events()
    rejected_ids, ignored_ids = suppressed_candidate_ids(events)

    layers: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for authority in CONFIRMED_AUTHORITIES:
        matching: list[dict[str, Any]] = []
        for entry in live_entries(dictionary):
            if str(entry["notation_token"]) != token:
                continue
            if str(entry["authority"]) != authority:
                continue
            scope = entry.get("scope") or {}
            if not scope_covers(
                scope, document=path, document_type=document_type, region=region
            ):
                excluded.append(
                    {
                        "entry_id": entry["entry_id"],
                        "authority": authority,
                        "scope": dict(scope),
                        "reason": EXCLUSION_SCOPE,
                    }
                )
                continue
            version_state = _version_state(
                entry, parse_revision_id=active, document=path
            )
            if version_state:
                stale.append(
                    {
                        "entry_id": entry["entry_id"],
                        "notation_token": token,
                        "meaning": entry["meaning"],
                        "authority": authority,
                        "scope": dict(scope),
                        "confirmed_by": entry.get("confirmed_by"),
                        "confirmed_at": entry.get("confirmed_at"),
                        "from_parse_revision": str(
                            (entry.get("version") or {}).get("parse_revision_id") or ""
                        ),
                        "to_parse_revision": active,
                        "requires_review_event": True,
                        "auto_applied": False,
                        "reason": version_state,
                        "boundary": MIGRATION_BOUNDARY,
                    }
                )
                continue
            validity = _validity_state(entry, moment)
            if validity:
                excluded.append(
                    {
                        "entry_id": entry["entry_id"],
                        "authority": authority,
                        "scope": dict(scope),
                        "reason": validity,
                        "valid_from": entry.get("valid_from"),
                        "valid_until": entry.get("valid_until"),
                        "at": moment,
                    }
                )
                continue
            matching.append(entry)
        if matching:
            layers.append(_layer_payload(authority, matching))

    candidates = [
        candidate
        for candidate in candidates_from_index(
            index,
            project_root=getattr(state, "project_root", None),
            document=path,
            limit=limit,
        )
        if str(candidate["notation_token"]) == token
        and (not region or str(candidate["region"]) == str(region))
    ]
    for candidate in candidates:
        mark_suppressed(candidate, rejected_ids, ignored_ids)

    quarantined = None
    if external_common_knowledge is not None and str(external_common_knowledge):
        quarantined = {
            "authority": EXTERNAL_COMMON_KNOWLEDGE,
            "meaning": str(external_common_knowledge),
            "source": "caller",
            "supports_project_fact": False,
            "boundary": EXTERNAL_BOUNDARY,
        }

    resolved: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] = []
    resolution: Mapping[str, Any] | None = None
    status = NOT_FOUND
    requires_resolution = False
    if layers:
        # The highest layer that holds a live meaning decides the answer. A lower
        # layer can disagree, and that disagreement is reported, but it can never
        # outrank the layer above it by itself.
        top = layers[0]
        if not top["agrees"]:
            group = list(top["entries"])
            conflicts = [_conflict_payload(entry) for entry in group]
            resolution = _resolution_of(group)
        else:
            winner = top["entries"][0]
            dissent = [
                entry
                for layer in layers[1:]
                for entry in layer["entries"]
                if str(entry["meaning"]) != str(winner["meaning"])
            ]
            group = [winner, *dissent]
            resolution = _resolution_of(group)
            if dissent:
                conflicts = [_conflict_payload(entry) for entry in group]
                requires_resolution = True
        if resolution is not None:
            chosen = next(
                (
                    entry
                    for entry in group
                    if entry["entry_id"] == str(resolution.get("winner_entry_id"))
                ),
                None,
            )
        else:
            chosen = group[0]
        if conflicts and resolution is None:
            status = AMBIGUOUS
        else:
            resolved = _resolved_payload(
                chosen,
                token,
                corroborating=[
                    entry
                    for entry in group
                    if entry is not chosen
                    and str(entry["meaning"]) == str(chosen["meaning"])
                ],
            )
            status = RESOLVED
            requires_resolution = False

    if status == NOT_FOUND:
        live_candidates = [
            candidate
            for candidate in candidates
            if not candidate.get("rejected") and not candidate.get("ignored")
        ]
        if live_candidates:
            status = CANDIDATE_ONLY
        elif quarantined is not None:
            status = QUARANTINED

    limitations = [
        "Only a confirmed entry can be cited as a project fact; candidates below "
        "the confirmed layers carry their own claim boundary.",
        "A meaning confirmed for one scope never answers a question about another "
        "region, document, or document type.",
    ]
    if status == AMBIGUOUS:
        limitations.append(
            "Two confirmed meanings disagree for this token in this scope; the "
            "answer stays ambiguous until resolve_conflict records which source "
            "wins."
        )
    if stale:
        limitations.append(
            "The newest parse revision is a different one from the revision these "
            "meanings were confirmed on, so they wait as migration candidates."
        )
    return {
        "status": status,
        "schema_version": NOTATION_DICTIONARY_VERSION,
        "notation_token": token,
        "query": {
            "document": path,
            "document_type": str(document_type or ""),
            "region": str(region or ""),
            "parse_revision_id": active,
            "at": moment,
        },
        "resolved": resolved,
        "resolution": dict(resolution) if resolution is not None else None,
        "requires_resolution": requires_resolution,
        "authority_winner": (
            _conflict_payload(layers[0]["entries"][0]) if conflicts else None
        ),
        "layers": layers,
        "conflicts": conflicts,
        "migration_candidates": stale + migration_candidates(state, document=path),
        "excluded": excluded,
        "candidates": candidates,
        "quarantined": quarantined,
        "supports_project_fact": status == RESOLVED,
        "boundary": BOUNDARY,
        "candidate_boundary": CANDIDATE_BOUNDARY,
        "limitations": limitations,
    }


def _conflict_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": entry["entry_id"],
        "meaning": entry["meaning"],
        "authority": entry["authority"],
        "scope": dict(entry["scope"]),
        "confirmed_by": entry.get("confirmed_by"),
        "confirmed_at": entry.get("confirmed_at"),
        "resolution": entry.get("resolution"),
        "supports_project_fact": False,
    }


def _resolved_payload(
    entry: Mapping[str, Any],
    token: str,
    *,
    corroborating: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "entry_id": entry["entry_id"],
        "notation_token": token,
        "meaning": entry["meaning"],
        "authority": entry["authority"],
        "status": entry.get("status"),
        "scope": dict(entry["scope"]),
        "confirmed_by": entry.get("confirmed_by"),
        "confirmed_at": entry.get("confirmed_at"),
        "valid_from": entry.get("valid_from"),
        "valid_until": entry.get("valid_until"),
        "version": dict(entry.get("version") or {}),
        "basis": dict(entry.get("basis") or {}),
        "resolution": entry.get("resolution"),
        "supports_project_fact": True,
        "corroborating_entry_ids": [
            entry["entry_id"] for entry in corroborating
        ],
    }


def _layer_payload(authority: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    meanings: list[str] = []
    for entry in entries:
        if entry["meaning"] not in meanings:
            meanings.append(str(entry["meaning"]))
    return {
        "authority": authority,
        "entries": [
            {
                "entry_id": entry["entry_id"],
                "meaning": entry["meaning"],
                "scope": dict(entry["scope"]),
                "authority": entry["authority"],
                "status": entry["status"],
                "confirmed_by": entry.get("confirmed_by"),
                "confirmed_at": entry.get("confirmed_at"),
                "valid_from": entry.get("valid_from"),
                "valid_until": entry.get("valid_until"),
                "version": dict(entry.get("version") or {}),
                "supersedes": entry.get("supersedes"),
                "resolution": entry.get("resolution"),
            }
            for entry in entries
        ],
        "meanings": meanings,
        "agrees": len(meanings) == 1,
    }


def dictionary_view(
    state: Any,
    index: Any = None,
    *,
    document: str | None = None,
    include_history: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """The whole dictionary: confirmed meanings, then everything below them."""

    dictionary = state.read_notation()
    events = state.review_events()
    rejected_ids, ignored_ids = suppressed_candidate_ids(events)
    entries = [dict(entry) for entry in dictionary.get("entries") or []]
    confirmed = [entry for entry in entries if str(entry["status"]) == CONFIRMED]
    history = [
        entry for entry in entries if str(entry["status"]) in (SUPERSEDED, REJECTED)
    ]
    candidates = candidates_from_index(
        index,
        project_root=getattr(state, "project_root", None),
        document=document,
        limit=limit,
    )
    for candidate in candidates:
        mark_suppressed(candidate, rejected_ids, ignored_ids)

    payload: dict[str, Any] = {
        "status": "ready" if confirmed or candidates else "empty",
        "schema_version": NOTATION_DICTIONARY_VERSION,
        "dictionary": {
            "entry_schema_version": dictionary.get("entry_schema_version"),
            "dictionary_version": dictionary.get("dictionary_version"),
            "updated_at": dictionary.get("updated_at"),
            "entries": confirmed,
            "counts": {
                "confirmed": len(confirmed),
                "rejected": sum(1 for entry in entries if entry["status"] == REJECTED),
                "superseded": sum(
                    1 for entry in entries if entry["status"] == SUPERSEDED
                ),
            },
        },
        "candidates": candidates,
        "migration_candidates": migration_candidates(
            state, document=document, limit=limit
        ),
        "supports_project_fact": {
            "confirmed_entries": True,
            "candidates": False,
            "migration_candidates": False,
            "external_common_knowledge": False,
        },
        "authority_order": list(AUTHORITY_LAYERS),
        "confirmed_authorities": list(CONFIRMED_AUTHORITIES),
        "boundary": BOUNDARY,
        "candidate_boundary": CANDIDATE_BOUNDARY,
        "limitations": [
            "An unconfirmed candidate is never part of the dictionary and never "
            "supports a project fact; confirming one is a review action.",
            "A meaning confirmed on one parse revision does not follow the next "
            "revision on its own; it waits as a migration candidate.",
        ],
    }
    if include_history:
        payload["history"] = history
    return payload


def plan_token(payload: Mapping[str, Any]) -> str:
    """A digest of exactly what a caller is about to apply."""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AMBIGUOUS",
    "AUTHORITY_LAYERS",
    "AUTHORITY_RESOLUTION",
    "BOUNDARY",
    "CANDIDATE_BOUNDARY",
    "CANDIDATE_ID_PREFIX",
    "CANDIDATE_INTERPRETATION",
    "CANDIDATE_ONLY",
    "CONFIRMED",
    "CONFIRMED_AUTHORITIES",
    "CONFIRM_ACTION",
    "CORRECT_ACTION",
    "DOCUMENT_DEFINITION",
    "DOCUMENT_SCOPE",
    "DOCUMENT_TYPE_DEFINITION",
    "DOCUMENT_TYPE_SCOPE",
    "ENTRY_STATUSES",
    "EXCLUSION_EXPIRED",
    "EXCLUSION_NEWER_REVISION",
    "EXCLUSION_REJECTED",
    "EXCLUSION_SCOPE",
    "EXCLUSION_SUPERSEDED",
    "EXTERNAL_BOUNDARY",
    "EXTERNAL_COMMON_KNOWLEDGE",
    "HUMAN_CHOICE_RESOLUTION",
    "IGNORE_ACTION",
    "MIGRATION_BOUNDARY",
    "NOTATION_DICTIONARY_NAME",
    "NOTATION_DICTIONARY_VERSION",
    "NOTATION_ENTRY_VERSION",
    "NOT_FOUND",
    "PROJECT_DICTIONARY",
    "PROJECT_SCOPE",
    "QUARANTINED",
    "REGION_LEGEND",
    "REGION_SCOPE",
    "REJECTED",
    "REJECT_ACTION",
    "RESOLVE_CONFLICT_ACTION",
    "RESOLUTION_KINDS",
    "RESOLVED",
    "REVIEW_EVENT_SUBJECT_CANDIDATE",
    "REVIEW_EVENT_SUBJECT_ENTRY",
    "SCOPE_AUTHORITY",
    "SCOPE_KINDS",
    "SUPERSEDED",
    "active_parse_revision_for",
    "build_entry",
    "candidate_id",
    "candidate_subjects",
    "candidates_from_index",
    "digest",
    "dictionary_view",
    "empty_notation_dictionary",
    "entry_by_id",
    "history_of",
    "layer_for_scope",
    "live_entries",
    "migration_candidates",
    "mark_suppressed",
    "new_entry_id",
    "normalize_document_path",
    "normalize_scope",
    "plan_token",
    "resolve",
    "scope_covers",
    "scope_document",
    "scope_key",
    "scope_region",
    "suppressed_candidate_ids",
    "validate_entry",
]
