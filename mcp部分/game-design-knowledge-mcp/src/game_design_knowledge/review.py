"""Review actions: a preview first, then an append-only record of the decision.

Every write to the Designer Notation Dictionary goes through two calls. The
first returns what would change and a token derived from exactly that change;
the second only applies when it is handed the same token. An operator therefore
never confirms a plan they have not seen, and a plan that stopped being true --
because the dictionary moved underneath it -- is refused instead of applied.

Two promises hold for every action here:

* The append-only Review Event is written *before* the dictionary is
  materialised, so a crash leaves a recorded decision that is not yet reflected,
  never a reflected decision nobody recorded.
* Nothing in this module touches the derived knowledge index or the source
  evidence. Rejecting a reading, or rolling a rejection back with a later
  action, is a statement about a human reading, not about the documents.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .notation import (
    AUTHORITY_LAYERS,
    AUTHORITY_RESOLUTION,
    BOUNDARY,
    CONFIRMED,
    DOCUMENT_SCOPE,
    HUMAN_CHOICE_RESOLUTION,
    NOTATION_DICTIONARY_VERSION,
    REGION_SCOPE,
    REJECTED,
    RESOLUTION_KINDS,
    REVIEW_EVENT_SUBJECT_CANDIDATE,
    REVIEW_EVENT_SUBJECT_ENTRY,
    SUPERSEDED,
    active_parse_revision_for,
    build_entry,
    candidates_from_index,
    digest,
    entry_by_id,
    layer_for_scope,
    live_entries,
    new_entry_id,
    normalize_document_path,
    normalize_scope,
    plan_token as token_for,
    scope_covers,
    scope_document,
    scope_key,
    scope_region,
    validate_entry,
)


CONFIRM = "confirm"
CORRECT = "correct"
REJECT = "reject"
IGNORE = "ignore"
RESOLVE_CONFLICT = "resolve_conflict"
REVIEW_ACTIONS: tuple[str, ...] = (
    CONFIRM,
    CORRECT,
    REJECT,
    IGNORE,
    RESOLVE_CONFLICT,
)

PLAN_STATUS = "confirmation_required"
APPLIED_STATUS = "applied"
UNCHANGED_STATUS = "unchanged"
BLOCKED_STATUS = "blocked"

JOURNAL_NAME = "journal/review_events.jsonl"

LIMITATIONS = [
    "A review action only writes Durable Project State; the derived knowledge "
    "index and the source evidence are never modified by it.",
    "The preview token covers the exact change; if the dictionary moves before "
    "apply, the token no longer matches and the action is refused.",
    "A rejection is reversed by a later review event, never by editing history.",
]


class ReviewError(ValueError):
    """A review action that cannot be planned or applied as asked."""


def _occurred_at(value: str | None = None) -> str:
    if value and str(value).strip():
        return str(value).strip()
    return datetime.now(timezone.utc).isoformat()


def review_history(
    state: Any,
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """The append-only record: what a person decided, and what it replaced."""

    events = state.review_events(subject_type=subject_type, subject_id=subject_id)
    if action:
        events = [event for event in events if str(event.action) == str(action)]
    chosen = events[-max(1, int(limit)) :]
    return {
        "status": "recorded" if chosen else "not_recorded",
        "schema_version": NOTATION_DICTIONARY_VERSION,
        "events": [
            {
                "event_id": event.event_id,
                "occurred_at": event.occurred_at,
                "actor": event.actor,
                "action": event.action,
                "subject_type": event.subject_type,
                "subject_id": event.subject_id,
                "note": event.note,
                "payload": dict(event.payload),
            }
            for event in chosen
        ],
        "total": len(events),
        "boundary": BOUNDARY,
        "limitations": ["Review events are append-only; a reversal is a new event."],
    }


def plan_review_action(
    state: Any,
    index: Any = None,
    **intent: Any,
) -> dict[str, Any]:
    """Describe one review action, and the token that would apply it."""

    return _plan(state, index, intent)


def apply_review_action(
    state: Any,
    index: Any = None,
    *,
    plan_token: str,
    **intent: Any,
) -> dict[str, Any]:
    """Apply a review action whose preview the caller is holding."""

    if not str(plan_token or "").strip():
        raise ReviewError("plan_token is required; plan the review action first")
    plan = _plan(state, index, intent)
    if not hmac.compare_digest(str(plan["plan_token"]), str(plan_token)):
        raise ReviewError(
            "The review plan changed since it was previewed; call "
            "plan_review_action again and confirm the new plan"
        )
    if str(plan["status"]) == UNCHANGED_STATUS:
        return {
            **plan,
            "status": UNCHANGED_STATUS,
            "applied": False,
            "review_event": None,
            "derived_knowledge_index_modified": False,
            "source_evidence_modified": False,
        }
    return _apply(state, plan)


def _normalized_intent(intent: Mapping[str, Any]) -> dict[str, Any]:
    action = str(intent.get("action") or "").strip().lower()
    if action not in REVIEW_ACTIONS:
        raise ReviewError(
            f"action must be one of {list(REVIEW_ACTIONS)}, "
            f"not {intent.get('action')!r}"
        )
    return {
        "action": action,
        "notation_token": str(intent.get("notation_token") or "").strip(),
        "meaning": str(intent.get("meaning") or "").strip(),
        "entry_id": str(intent.get("entry_id") or "").strip(),
        "scope_kind": str(intent.get("scope_kind") or "").strip().lower(),
        "scope_value": str(intent.get("scope_value") or "").strip(),
        "document": str(intent.get("document") or "").strip(),
        "document_type": str(intent.get("document_type") or "").strip(),
        "region": str(intent.get("region") or "").strip(),
        "authority": str(intent.get("authority") or "").strip().lower(),
        "parse_revision_id": str(intent.get("parse_revision_id") or "").strip(),
        "source_revision_id": str(intent.get("source_revision_id") or "").strip(),
        "valid_from": str(intent.get("valid_from") or "").strip(),
        "valid_until": str(intent.get("valid_until") or "").strip(),
        "resolution_kind": str(intent.get("resolution_kind") or "").strip().lower(),
        "candidate_id": str(intent.get("candidate_id") or "").strip(),
        "reason": str(intent.get("reason") or "").strip(),
        "actor": str(intent.get("actor") or "").strip(),
        "note": str(intent.get("note") or "").strip(),
        "basis": dict(intent.get("basis") or {}),
        "at": _occurred_at(intent.get("at")),
    }


def _scope_from(values: Mapping[str, Any]) -> dict[str, str] | None:
    kind = str(values["scope_kind"])
    if kind:
        return normalize_scope(
            kind,
            values["scope_value"],
            document=values["document"],
            region=values["region"],
        )
    if values["document"]:
        if values["region"]:
            return normalize_scope(
                REGION_SCOPE, document=values["document"], region=values["region"]
            )
        return normalize_scope(DOCUMENT_SCOPE, document=values["document"])
    if values["scope_value"]:
        return normalize_scope(DOCUMENT_SCOPE, values["scope_value"])
    return None


def _preview_entry(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "record_type": entry.get("record_type", "notation_entry"),
        "entry_schema_version": entry.get("entry_schema_version", 1),
        "entry_id": entry.get("entry_id"),
        "notation_token": entry.get("notation_token"),
        "meaning": entry.get("meaning"),
        "scope": dict(entry.get("scope") or {}),
        "authority": entry.get("authority"),
        "status": entry.get("status"),
        "version": dict(entry.get("version") or {}),
        "valid_from": entry.get("valid_from"),
        "valid_until": entry.get("valid_until"),
        "basis": dict(entry.get("basis") or {}),
        "reason": entry.get("reason", ""),
        "note": entry.get("note", ""),
        "confirmed_by": entry.get("confirmed_by"),
        "confirmed_at": entry.get("confirmed_at"),
        "supersedes": entry.get("supersedes"),
        "superseded_by": entry.get("superseded_by"),
        "resolution": entry.get("resolution"),
    }


def _affected(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    if scope is None:
        return {
            "project": False,
            "documents": [],
            "document_types": [],
            "regions": [],
        }
    kind = str(scope.get("kind"))
    document = scope_document(scope)
    return {
        "project": kind == "project",
        "documents": [document] if document else [],
        "document_types": [str(scope.get("value"))]
        if kind == "document_type"
        else [],
        "regions": [str(scope.get("value"))] if kind == REGION_SCOPE else [],
    }


def _plan(state: Any, index: Any, intent: Mapping[str, Any]) -> dict[str, Any]:
    values = _normalized_intent(intent)
    action = values["action"]
    dictionary = state.read_notation()
    entries = [dict(entry) for entry in dictionary.get("entries") or []]

    before: Mapping[str, Any] | None = None
    after: dict[str, Any] | None = None
    changes: list[str] = []
    subjects = {"subject_type": "", "subject_id": ""}
    writes: list[str] = []
    conflicts: list[dict[str, Any]] = []
    blocked = ""
    scope: dict[str, str] | None = None
    resolution: dict[str, Any] | None = None
    losers: list[dict[str, Any]] = []
    status = PLAN_STATUS

    if action == CONFIRM and not values["notation_token"]:
        raise ReviewError("a review action needs the notation token it explains")
    if action in (CONFIRM, CORRECT) and not values["meaning"]:
        raise ReviewError("a review action needs the meaning it records")
    if action in (CORRECT, RESOLVE_CONFLICT) and not values["entry_id"]:
        raise ReviewError(f"{action} needs the entry it acts on")
    if action == REJECT and not values["entry_id"] and not values["notation_token"]:
        raise ReviewError("reject acts either on an entry or on a candidate reading")
    if action in (CONFIRM, IGNORE, REJECT, RESOLVE_CONFLICT):
        scope = _scope_from(values)
    if action in (CONFIRM, IGNORE) and scope is None:
        raise ReviewError(f"{action} needs the scope the reading belongs to")
    if action == REJECT and not values["entry_id"] and scope is None:
        raise ReviewError("rejecting a candidate needs the scope it belongs to")

    if action == CONFIRM:
        assert scope is not None
        authority = values["authority"] or layer_for_scope(scope)
        document = scope_document(scope)
        active = values["parse_revision_id"] or (
            active_parse_revision_for(state, document) if document else ""
        )
        version = {
            key: value
            for key, value in (
                ("document", document),
                ("parse_revision_id", active),
                (
                    "source_revision_id",
                    values["source_revision_id"]
                    or _source_revision_for(state, document, active),
                ),
            )
            if value
        }
        peers = [
            entry
            for entry in live_entries({"entries": entries})
            if str(entry["notation_token"]) == values["notation_token"]
            and str(entry["authority"]) == authority
            and dict(entry["scope"]) == dict(scope)
            and str((entry.get("version") or {}).get("parse_revision_id") or "")
            == active
        ]
        # A meaning confirmed on an older revision is a migration candidate, not
        # a peer: re-confirming the same reading on the new revision is how that
        # candidate is closed, and the older binding is then superseded.
        stale_peers = [
            entry
            for entry in live_entries({"entries": entries})
            if str(entry["notation_token"]) == values["notation_token"]
            and str(entry["authority"]) == authority
            and dict(entry["scope"]) == dict(scope)
            and str((entry.get("version") or {}).get("parse_revision_id") or "")
            not in ("", active)
            and str(entry["meaning"]) == values["meaning"]
        ]
        losers = [*losers, *stale_peers]
        before = _preview_entry(peers[0]) if peers else None
        if (
            peers
            and not stale_peers
            and all(str(entry["meaning"]) == values["meaning"] for entry in peers)
        ):
            status = UNCHANGED_STATUS
            after = _preview_entry(peers[0])
        else:
            if peers:
                conflicts = [_preview_entry(entry) for entry in peers]
                blocked = (
                    "This scope already holds a different confirmed meaning at the "
                    "same authority layer; use resolve_conflict to record which one "
                    "wins, or correct the existing entry."
                )
            after = build_entry(
                notation_token=values["notation_token"],
                meaning=values["meaning"],
                scope=scope,
                authority=authority,
                version=version,
                valid_from=values["valid_from"] or None,
                valid_until=values["valid_until"] or None,
                basis=_basis(values),
                reason=values["reason"],
                actor=values["actor"],
                occurred_at=values["at"],
                note=values["note"],
                deferred_id=True,
            )
            changes = [
                f"confirmed meaning for {values['notation_token']!r} in "
                f"{scope_key(scope)}"
            ]
            if losers:
                changes.append(
                    "superseded "
                    + ", ".join(str(entry["entry_id"]) for entry in losers)
                    + " whose revision binding went stale"
                )
            writes = ["notation.json", JOURNAL_NAME]
        subjects = {
            "subject_type": REVIEW_EVENT_SUBJECT_ENTRY,
            "subject_id": f"{values['notation_token']}@{scope_key(scope)}",
            "subject_id_from_entry": True,
        }

    elif action == CORRECT:
        target = entry_by_id({"entries": entries}, values["entry_id"])
        if target is None:
            raise ReviewError(f"Unknown notation entry: {values['entry_id']}")
        if str(target["status"]) == SUPERSEDED:
            raise ReviewError(
                f"Notation entry {values['entry_id']} is already superseded; "
                "correct the entry that replaced it"
            )
        if str(target["status"]) == REJECTED:
            raise ReviewError(
                f"Notation entry {values['entry_id']} was rejected; confirm the "
                "reading again instead of correcting it"
            )
        scope = dict(target["scope"])
        before = _preview_entry(target)
        after = build_entry(
            notation_token=str(target["notation_token"]),
            meaning=values["meaning"],
            scope=scope,
            authority=str(target["authority"]),
            version=dict(target.get("version") or {}),
            valid_from=values["valid_from"] or target.get("valid_from"),
            valid_until=values["valid_until"] or target.get("valid_until"),
            basis=_basis(values),
            reason=values["reason"],
            actor=values["actor"],
            occurred_at=values["at"],
            supersedes=str(target["entry_id"]),
            note=values["note"],
            deferred_id=True,
        )
        changes = [
            f"meaning {target['meaning']!r} -> {values['meaning']!r}",
            f"entry {target['entry_id']} -> superseded by the corrected entry",
        ]
        writes = ["notation.json", JOURNAL_NAME]
        subjects = {
            "subject_type": REVIEW_EVENT_SUBJECT_ENTRY,
            "subject_id": str(target["entry_id"]),
        }

    elif action == REJECT:
        if values["entry_id"]:
            target = entry_by_id({"entries": entries}, values["entry_id"])
            if target is None:
                raise ReviewError(f"Unknown notation entry: {values['entry_id']}")
            scope = dict(target["scope"])
            before = _preview_entry(target)
            if str(target["status"]) == REJECTED:
                status = UNCHANGED_STATUS
                after = _preview_entry(target)
            elif str(target["status"]) == SUPERSEDED:
                raise ReviewError(
                    f"Notation entry {values['entry_id']} is already superseded; "
                    "reject the entry that replaced it"
                )
            else:
                after = {**_preview_entry(target), "status": REJECTED}
                changes = [
                    f"status confirmed -> rejected for entry {target['entry_id']}"
                ]
                writes = ["notation.json", JOURNAL_NAME]
            subjects = {
                "subject_type": REVIEW_EVENT_SUBJECT_ENTRY,
                "subject_id": str(target["entry_id"]),
            }
        else:
            assert scope is not None
            match = _match_candidate(
                index, getattr(state, "project_root", None), values, scope
            )
            if values["candidate_id"] and match is None:
                raise ReviewError(
                    f"This index holds no candidate {values['candidate_id']}"
                )
            subject_id = values["candidate_id"] or f"{values['notation_token']}@" + (
                scope_key(scope)
            )
            subjects = {
                "subject_type": REVIEW_EVENT_SUBJECT_CANDIDATE,
                "subject_id": subject_id,
            }
            before = match
            after = {"status": REJECTED, "candidate_id": subject_id}
            changes = [f"rejected candidate {subject_id} in {scope_key(scope)}"]
            writes = [JOURNAL_NAME]

    elif action == IGNORE:
        assert scope is not None
        match = _match_candidate(
            index, getattr(state, "project_root", None), values, scope
        )
        if values["candidate_id"] and match is None:
            raise ReviewError(f"This index holds no candidate {values['candidate_id']}")
        subject_id = values["candidate_id"] or f"{values['notation_token']}@" + (
            scope_key(scope)
        )
        subjects = {
            "subject_type": REVIEW_EVENT_SUBJECT_CANDIDATE,
            "subject_id": subject_id,
        }
        before = match
        after = {"status": "ignored", "candidate_id": subject_id}
        changes = [f"ignored candidate {subject_id} in {scope_key(scope)}"]
        writes = [JOURNAL_NAME]

    else:  # RESOLVE_CONFLICT
        winner = entry_by_id({"entries": entries}, values["entry_id"])
        if winner is None:
            raise ReviewError(f"Unknown notation entry: {values['entry_id']}")
        if str(winner["status"]) != CONFIRMED:
            raise ReviewError(
                f"Notation entry {values['entry_id']} is {winner['status']}, so it "
                "cannot be the winning reading"
            )
        scope = dict(winner["scope"])
        question = _question_for(winner, values)
        group = [
            entry
            for entry in live_entries({"entries": entries})
            if str(entry["notation_token"]) == str(winner["notation_token"])
            and scope_covers(entry["scope"], **question)
        ]
        if len({str(entry["meaning"]) for entry in group}) < 2:
            raise ReviewError(
                "There is no conflict to resolve: no other confirmed meaning for "
                "this token covers "
                f"{question['document'] or question['document_type'] or 'the project'}"
                "; pass the document and region the conflict is about"
            )
        losers = [
            entry for entry in group if str(entry["meaning"]) != str(winner["meaning"])
        ]
        rank = _authority_rank()
        strictly_lower = all(
            rank[str(entry["authority"])] > rank[str(winner["authority"])]
            for entry in losers
        )
        kind = values["resolution_kind"] or (
            AUTHORITY_RESOLUTION if strictly_lower else HUMAN_CHOICE_RESOLUTION
        )
        if kind not in RESOLUTION_KINDS:
            raise ReviewError(
                f"resolution_kind must be one of {list(RESOLUTION_KINDS)}"
            )
        if kind == AUTHORITY_RESOLUTION and not strictly_lower:
            raise ReviewError(
                "A losing meaning speaks from the same authority layer as the "
                "winner, so this resolution has to be recorded as a human_choice"
            )
        if not values["reason"]:
            raise ReviewError("resolve_conflict needs a reason")
        resolution = {
            "kind": kind,
            "winner_entry_id": str(winner["entry_id"]),
            "resolved_entry_ids": [str(entry["entry_id"]) for entry in losers],
            "winner_authority": str(winner["authority"]),
            "loser_authorities": sorted({str(entry["authority"]) for entry in losers}),
            "reason": values["reason"],
            "resolved_by": values["actor"],
            "resolved_at": values["at"],
            "source_evidence_modified": False,
        }
        before = _preview_entry(winner)
        after = {**before, "resolution": resolution}
        changes = [
            f"{kind} resolution: {winner['entry_id']} wins in {scope_key(scope)}",
            *(f"entry {entry['entry_id']} -> superseded" for entry in losers),
        ]
        writes = ["notation.json", JOURNAL_NAME]
        subjects = {
            "subject_type": REVIEW_EVENT_SUBJECT_ENTRY,
            "subject_id": str(winner["entry_id"]),
        }

    event = {
        "action": action,
        "subject_type": subjects["subject_type"],
        "subject_id": subjects["subject_id"],
        "actor": values["actor"],
        "occurred_at": values["at"],
    }
    event_payload = {
        "action": action,
        "notation_token": values["notation_token"] or _token_of(before, after),
        "scope": dict(scope) if scope else {},
        "before": _preview_entry(before),
        "after": _preview_entry(after) if after else None,
        "reason": values["reason"],
        "basis": _basis(values),
        "actor": values["actor"],
        "occurred_at": values["at"],
        "superseded_entry_ids": [str(entry["entry_id"]) for entry in losers],
        "resolution": resolution,
        "derived_knowledge_index_modified": False,
        "source_evidence_modified": False,
    }
    plan: dict[str, Any] = {
        "status": status,
        "schema_version": NOTATION_DICTIONARY_VERSION,
        "action": action,
        "plan_token": "",
        "preview": {
            "before": _preview_entry(before),
            "after": _preview_entry(after) if after else None,
            "changes": changes,
        },
        "scope": dict(scope) if scope else None,
        "authority": (after or before or {}).get("authority"),
        "affected": _affected(scope),
        "propagates_beyond_scope": False,
        "writes": writes,
        "event": event,
        "event_payload": event_payload,
        "conflicts": conflicts,
        "blocked": blocked,
        "candidate": before if action in (REJECT, IGNORE) and not values["entry_id"] else None,
        "resolution": resolution,
        "derived_knowledge_index_modified": False,
        "source_evidence_modified": False,
        "requires_plan_token": True,
        "boundary": BOUNDARY,
        "limitations": list(LIMITATIONS),
    }
    plan["plan_token"] = token_for(
        {
            "action": action,
            "scope": plan["scope"],
            "authority": plan["authority"],
            "before": digest(_token_preview(plan["preview"]["before"])),
            "after": digest(_token_preview(plan["preview"]["after"])),
            "subject_type": subjects["subject_type"],
            "subject_id": subjects["subject_id"],
            "actor": values["actor"],
            "reason": values["reason"],
            "resolution": (
                digest(
                    {
                        key: value
                        for key, value in dict(resolution).items()
                        if key != "resolved_at"
                    }
                )
                if resolution
                else None
            ),
            "entry_state": _entry_state(entries),
        }
    )
    if blocked:
        plan["status"] = BLOCKED_STATUS
    return plan


def _apply(state: Any, plan: Mapping[str, Any]) -> dict[str, Any]:
    action = str(plan["action"])
    event = dict(plan["event"])
    payload = {
        **dict(plan["event_payload"]),
        "after": _stamped(plan["event_payload"].get("after"), str(event["occurred_at"])),
    }
    preview = plan["preview"]
    stamp = str(event["occurred_at"])
    written: dict[str, Any] | None = None
    minted_id = ""
    with state.lock():
        dictionary = state.read_notation()
        entries = [dict(entry) for entry in dictionary.get("entries") or []]
        # The event is appended first: a crash can leave a recorded decision that
        # is not yet materialised, which stays auditable. The reverse cannot be
        # recovered, so it is not allowed to happen.
        if action in (CONFIRM, CORRECT):
            minted_id = new_entry_id()
            if payload.get("after"):
                payload["after"] = {**payload["after"], "entry_id": minted_id}
        recorded = state.append_review_event(
            action,
            subject_type=str(event["subject_type"]),
            subject_id=minted_id or str(event["subject_id"]),
            payload={**payload, "plan_token": str(plan["plan_token"])},
            actor=str(event["actor"]),
            occurred_at=stamp,
        )
        if action in (CONFIRM, CORRECT):
            entry = {
                **dict(preview["after"]),
                "entry_id": minted_id,
                "confirmed_at": stamp,
                "updated_at": stamp,
            }
            validate_entry(entry)
            entries.append(entry)
            removed = {str(value) for value in payload["superseded_entry_ids"]}
            if action == CORRECT and entry.get("supersedes"):
                removed.add(str(entry["supersedes"]))
            for stored in entries:
                if str(stored["entry_id"]) in removed:
                    stored["status"] = SUPERSEDED
                    stored["superseded_by"] = entry["entry_id"]
                    stored["updated_at"] = stamp
            written = state.write_notation({**dictionary, "entries": entries})
        elif action == REJECT and _after_status(preview) == REJECTED:
            target_id = str(preview["after"]["entry_id"])
            for stored in entries:
                if str(stored["entry_id"]) == target_id:
                    stored["status"] = REJECTED
                    stored["updated_at"] = stamp
            written = state.write_notation({**dictionary, "entries": entries})
        elif action == RESOLVE_CONFLICT:
            resolution = {**dict(plan["resolution"]), "resolved_at": stamp}
            payload["resolution"] = resolution
            target_id = str(preview["after"]["entry_id"])
            removed = {str(value) for value in payload["superseded_entry_ids"]}
            for stored in entries:
                identifier = str(stored["entry_id"])
                if identifier == target_id:
                    stored["resolution"] = resolution
                    stored["updated_at"] = stamp
                elif identifier in removed:
                    stored["status"] = SUPERSEDED
                    stored["superseded_by"] = target_id
                    stored["updated_at"] = stamp
            written = state.write_notation({**dictionary, "entries": entries})

    dictionary = state.read_notation()
    target_id = minted_id or (
        str(preview["after"]["entry_id"])
        if preview.get("after") and preview["after"].get("entry_id")
        else ""
    )
    return {
        "status": APPLIED_STATUS,
        "schema_version": NOTATION_DICTIONARY_VERSION,
        "action": action,
        "applied": True,
        "scope": plan["scope"],
        "authority": plan["authority"],
        "entry": entry_by_id(dictionary, target_id) if target_id else None,
        "review_event": {
            **recorded.as_payload(),
            "payload": {
                key: value
                for key, value in dict(recorded.payload).items()
                if key != "plan_token"
            },
        },
        "history": {
            "journal": JOURNAL_NAME,
            "append_only": True,
            "recorded_before_materialising": True,
        },
        "dictionary_entries": len(written["entries"]) if written else len(
            dictionary.get("entries") or []
        ),
        "previewed": preview,
        "conflicts": plan["conflicts"],
        "resolution": plan["resolution"],
        "affected": plan["affected"],
        "propagates_beyond_scope": False,
        "derived_knowledge_index_modified": False,
        "source_evidence_modified": False,
        "boundary": BOUNDARY,
        "limitations": list(LIMITATIONS),
    }


def _after_status(preview: Mapping[str, Any]) -> str:
    """The status a rejected entry is moved to, or an empty string."""

    after = preview.get("after") or {}
    return str(after.get("status") or "")


def _basis(values: Mapping[str, Any]) -> dict[str, Any]:
    basis = dict(values.get("basis") or {})
    if values.get("candidate_id"):
        basis.setdefault("candidate_id", values["candidate_id"])
    if values.get("reason") and "reason" not in basis:
        basis["reason"] = values["reason"]
    return basis


def _token_of(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> str:
    for entry in (before, after):
        if entry and entry.get("notation_token"):
            return str(entry["notation_token"])
    return ""


def _entry_state(entries: Sequence[Mapping[str, Any]]) -> str:
    """A digest of the dictionary, so a stale plan token cannot be replayed."""

    return digest(
        [
            {
                "entry_id": entry.get("entry_id"),
                "status": entry.get("status"),
                "meaning": entry.get("meaning"),
                "resolution": entry.get("resolution"),
            }
            for entry in entries
        ]
    )


def _token_preview(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The previewed entry without the clock.

    A plan is previewed and then applied a moment later, so the recorded time
    cannot be part of what the token covers. Everything a person decided --
    meaning, scope, authority, supersession -- is.
    """

    if entry is None:
        return None
    preview = {
        key: value for key, value in entry.items() if key != "confirmed_at"
    }
    resolution = preview.get("resolution")
    if isinstance(resolution, Mapping):
        preview["resolution"] = {
            key: value for key, value in resolution.items() if key != "resolved_at"
        }
    return preview


def _stamped(entry: Mapping[str, Any] | None, stamp: str) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {**dict(entry), "confirmed_at": stamp}


def _authority_rank() -> dict[str, int]:
    return {
        authority: index for index, authority in enumerate(AUTHORITY_LAYERS)
    }


def _match_candidate(
    index: Any,
    project_root: Any,
    values: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidates = candidates_from_index(
        index,
        project_root=project_root,
        document=scope_document(scope) or None,
    )
    for candidate in candidates:
        if values["candidate_id"]:
            if str(candidate.get("candidate_id")) == values["candidate_id"]:
                return dict(candidate)
            continue
        if values["notation_token"] and (
            str(candidate.get("notation_token")) != values["notation_token"]
        ):
            continue
        if dict(candidate.get("scope") or {}) == dict(scope):
            return dict(candidate)
    return None


def _question_for(winner: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, str]:
    """The question a conflict is about, so every source answering it is found.

    A local legend and the project dictionary disagree about the *same* reading
    in the same place. The place comes from the winning entry unless the caller
    names a wider one, in which case the wider question is the one being settled.
    """

    scope = winner.get("scope") or {}
    kind = str(scope.get("kind"))
    document = (
        normalize_document_path(values["document"])
        if values["document"]
        else scope_document(scope)
    )
    document_type = values["document_type"] or (
        str(scope.get("value")) if kind == "document_type" else ""
    )
    region = values["region"] or (scope_region(scope) if kind == REGION_SCOPE else "")
    return {
        "document": document,
        "document_type": document_type,
        "region": region,
    }


def _source_revision_for(state: Any, document: str, parse_revision: str) -> str:
    if not document or not parse_revision:
        return ""
    record = state.parse_revision(parse_revision)
    if record is None:
        return ""
    return str(record.source_revision_id or "")


__all__ = [
    "APPLIED_STATUS",
    "BLOCKED_STATUS",
    "CONFIRM",
    "CORRECT",
    "IGNORE",
    "JOURNAL_NAME",
    "LIMITATIONS",
    "PLAN_STATUS",
    "REJECT",
    "RESOLVE_CONFLICT",
    "REVIEW_ACTIONS",
    "ReviewError",
    "UNCHANGED_STATUS",
    "apply_review_action",
    "plan_review_action",
    "review_history",
]
