"""Durable project state: the copy of the truth that outlives the derived index.

The derived knowledge index is disposable. Anything a human decided, or any
identity that must survive a rebuild, lives here instead: logical documents,
the immutable source revision archive, parse revisions, review events, the
confirmed dictionary, and published revision bundles.

Every file is written through a temporary sibling and ``os.replace``, and every
mutation happens under one lock file, so an interrupted process leaves the
previous state readable rather than half-applied.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from typing import Any, Callable, Iterator, Mapping, TypeVar

from .revisions import (
    ProcessingManifest,
    bundle_id,
    document_id,
    file_sha256,
    normalize_relative_path,
    parse_revision_id,
    source_revision_id,
)
from .notation import (
    CONFIRMED as NOTATION_CONFIRMED,
    NOTATION_DICTIONARY_NAME,
    REJECTED as NOTATION_REJECTED,
    SUPERSEDED as NOTATION_SUPERSEDED,
    empty_notation_dictionary,
    validate_entry as validate_notation_entry,
)


STATE_SCHEMA_VERSION = 1
DEFAULT_STATE_DIRECTORY_NAME = ".design-state"
BACKUP_LIMIT = 10
LOCK_STALE_SECONDS = 900.0

MANIFEST_NAME = "manifest.json"
DICTIONARY_NAME = "dictionary.json"
JOURNAL_DIRECTORY = "journal"
SOURCE_REVISION_JOURNAL = "source_revisions.jsonl"
PARSE_REVISION_JOURNAL = "parse_revisions.jsonl"
REVIEW_EVENT_JOURNAL = "review_events.jsonl"
ARCHIVE_DIRECTORY = "archive"
ARCHIVE_OBJECT_DIRECTORY = "objects"
BUNDLE_DIRECTORY = "bundles"
BACKUP_DIRECTORY = "backups"
LOCK_NAME = "state.lock"
ENTRY_SCHEMA_VERSION = 1

T = TypeVar("T")


class StateError(RuntimeError):
    """Raised when durable state is missing, malformed, or unusable."""


class StateVersionError(StateError):
    """Raised when state was written by a newer state schema.

    The writer stays read-only: a newer manifest may mean fields this build
    would silently drop, so the safe answer is to stop and report the version.
    """


class StateLocked(StateError):
    """Raised when another process holds the durable-state lock."""


def state_directory_for(project_root: Path) -> Path:
    """Where durable state lives for a project, honouring the env override."""

    configured = os.environ.get("GAME_DESIGN_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (project_root / DEFAULT_STATE_DIRECTORY_NAME).resolve()


@dataclass(frozen=True)
class SourceRevisionRecord:
    """One archived byte sequence for one logical document."""

    source_revision_id: str
    document_id: str
    relative_path: str
    content_sha256: str
    byte_size: int
    archived_at: str
    archive_path: str
    note: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "record_type": "source_revision",
            "source_revision_id": self.source_revision_id,
            "document_id": self.document_id,
            "relative_path": self.relative_path,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "archived_at": self.archived_at,
            "archive_path": self.archive_path,
            "note": self.note,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceRevisionRecord":
        return cls(
            source_revision_id=str(payload["source_revision_id"]),
            document_id=str(payload["document_id"]),
            relative_path=str(payload["relative_path"]),
            content_sha256=str(payload["content_sha256"]),
            byte_size=int(payload["byte_size"]),
            archived_at=str(payload["archived_at"]),
            archive_path=str(payload["archive_path"]),
            note=str(payload.get("note") or ""),
        )


@dataclass(frozen=True)
class ParseRevisionRecord:
    """One parse of one source revision under one processing manifest."""

    parse_revision_id: str
    source_revision_id: str
    document_id: str
    database_schema_version: int
    processing_fingerprint: str
    created_at: str
    processing_manifest: Mapping[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "record_type": "parse_revision",
            "parse_revision_id": self.parse_revision_id,
            "source_revision_id": self.source_revision_id,
            "document_id": self.document_id,
            "database_schema_version": self.database_schema_version,
            "processing_fingerprint": self.processing_fingerprint,
            "created_at": self.created_at,
            "processing_manifest": dict(self.processing_manifest),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ParseRevisionRecord":
        return cls(
            parse_revision_id=str(payload["parse_revision_id"]),
            source_revision_id=str(payload["source_revision_id"]),
            document_id=str(payload["document_id"]),
            database_schema_version=int(payload["database_schema_version"]),
            processing_fingerprint=str(payload["processing_fingerprint"]),
            created_at=str(payload["created_at"]),
            processing_manifest=dict(payload.get("processing_manifest") or {}),
        )


@dataclass(frozen=True)
class DocumentRecord:
    """A logical document identity, independent of any index rebuild."""

    document_id: str
    relative_path: str
    created_at: str
    source_revisions: tuple[str, ...]
    parse_revisions: tuple[str, ...]
    latest_source_revision: str | None
    active_parse_revision: str | None


@dataclass(frozen=True)
class ReviewEvent:
    """An append-only human decision. Reversals are new events, never edits."""

    event_id: str
    occurred_at: str
    actor: str
    action: str
    subject_type: str
    subject_id: str
    note: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "record_type": "review_event",
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "action": self.action,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "note": self.note,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReviewEvent":
        return cls(
            event_id=str(payload["event_id"]),
            occurred_at=str(payload["occurred_at"]),
            actor=str(payload.get("actor") or ""),
            action=str(payload["action"]),
            subject_type=str(payload["subject_type"]),
            subject_id=str(payload["subject_id"]),
            note=str(payload.get("note") or ""),
            payload=dict(payload.get("payload") or {}),
        )


@dataclass
class StateManifest:
    """A small index over the append-only journals in ``manifest.json``."""

    state_schema_version: int = STATE_SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "state_schema_version": self.state_schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "documents": self.documents,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StateManifest":
        stored_version = int(payload.get("state_schema_version") or 0)
        if stored_version > STATE_SCHEMA_VERSION:
            raise StateVersionError(
                "Durable state was written by a newer state schema "
                f"({stored_version} > {STATE_SCHEMA_VERSION}); this build will not "
                "rewrite it. Upgrade the game-design-knowledge MCP server first."
            )
        if stored_version < 1:
            raise StateVersionError(
                f"Durable state has no usable state schema version: {stored_version}"
            )
        documents = payload.get("documents") or {}
        if not isinstance(documents, Mapping):
            raise StateError("Durable state manifest has a non-object documents map")
        return cls(
            state_schema_version=stored_version,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            documents={str(key): dict(value) for key, value in documents.items()},
        )


@dataclass
class _Mutation:
    """The outcome of one locked change: what to return, and what to persist."""

    result: Any = None
    changed: bool = False


def empty_dictionary() -> dict[str, Any]:
    return {
        "entry_schema_version": ENTRY_SCHEMA_VERSION,
        "updated_at": None,
        "entries": [],
    }


class DurableState:
    """Read and write the durable project state directory."""

    def __init__(self, project_root: Path, directory: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.directory = (
            Path(directory).resolve()
            if directory is not None
            else state_directory_for(self.project_root)
        )
        self._lock_depth = 0

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_NAME

    @property
    def dictionary_path(self) -> Path:
        return self.directory / DICTIONARY_NAME

    @property
    def notation_path(self) -> Path:
        return self.directory / NOTATION_DICTIONARY_NAME

    @property
    def journal_directory(self) -> Path:
        return self.directory / JOURNAL_DIRECTORY

    @property
    def archive_directory(self) -> Path:
        return self.directory / ARCHIVE_DIRECTORY

    @property
    def bundle_directory(self) -> Path:
        return self.directory / BUNDLE_DIRECTORY

    @property
    def backup_directory(self) -> Path:
        return self.directory / BACKUP_DIRECTORY

    @property
    def lock_path(self) -> Path:
        return self.directory / LOCK_NAME

    def exists(self) -> bool:
        return self.manifest_path.is_file()

    # -- lifecycle -----------------------------------------------------

    def initialize(self, force: bool = False) -> StateManifest:
        """Create an empty state directory, never silently over an existing one."""

        if self.exists():
            if not force:
                raise StateError(
                    f"Durable state already exists at {self.directory}; "
                    "pass force=True only after taking a backup"
                )
            self.backup(label="pre-init")
            for name in (MANIFEST_NAME, DICTIONARY_NAME):
                (self.directory / name).unlink(missing_ok=True)
            for name in (
                SOURCE_REVISION_JOURNAL,
                PARSE_REVISION_JOURNAL,
                REVIEW_EVENT_JOURNAL,
            ):
                (self.journal_directory / name).unlink(missing_ok=True)

        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_directory.mkdir(exist_ok=True)
        self.archive_directory.mkdir(exist_ok=True)
        self.bundle_directory.mkdir(exist_ok=True)

        now = _now()
        manifest = StateManifest(created_at=now, updated_at=now)
        self._atomic_write_json(self.manifest_path, manifest.as_payload())
        if not self.dictionary_path.exists():
            self._atomic_write_json(self.dictionary_path, empty_dictionary())
        for name in (
            SOURCE_REVISION_JOURNAL,
            PARSE_REVISION_JOURNAL,
            REVIEW_EVENT_JOURNAL,
        ):
            (self.journal_directory / name).touch(exist_ok=True)
        return manifest

    def ensure(self) -> StateManifest:
        if not self.exists():
            return self.initialize()
        return self.load()

    def load(self) -> StateManifest:
        if not self.exists():
            raise StateError(f"Durable state is not initialized at {self.directory}")
        payload = self._read_json(self.manifest_path)
        if not isinstance(payload, Mapping):
            raise StateError(f"{self.manifest_path} must contain a JSON object")
        manifest = StateManifest.from_payload(payload)
        return self._reconcile(manifest)

    def backup(self, label: str = "state") -> Path:
        """Copy the whole state directory into ``backups/<label>-<stamp>``."""

        if not self.directory.is_dir():
            raise StateError(f"Durable state directory does not exist: {self.directory}")
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        destination = self.backup_directory / f"{label}-{_stamp()}-{uuid.uuid4().hex[:6]}"
        shutil.copytree(
            self.directory,
            destination,
            ignore=shutil.ignore_patterns(BACKUP_DIRECTORY, LOCK_NAME),
        )
        self._prune_backups()
        return destination

    @contextmanager
    def lock(self, timeout: float = 0.0) -> Iterator[None]:
        """Hold the one state lock, clearing a stale lock left by a killed run."""

        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return

        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(timeout, 0.0)
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                if self._lock_is_stale():
                    self.lock_path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise StateLocked(
                        f"Durable state is locked by another process: {self.lock_path}"
                    ) from None
                time.sleep(0.05)
        try:
            os.write(descriptor, f"{os.getpid()} {_stamp()}\n".encode("utf-8"))
        finally:
            os.close(descriptor)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            self.lock_path.unlink(missing_ok=True)

    # -- documents -----------------------------------------------------

    def register_document(self, relative_path: str) -> tuple[DocumentRecord, bool]:
        """Return the logical document identity, creating it on first sight."""

        normalized = normalize_relative_path(relative_path)

        def mutate(manifest: StateManifest) -> _Mutation:
            created = False
            entry = manifest.documents.get(document_id(normalized))
            if entry is None:
                created = True
                entry = _new_document_entry(normalized)
                manifest.documents[entry["document_id"]] = entry
            else:
                entry["path"] = normalized
            entry.setdefault("source_revisions", [])
            entry.setdefault("parse_revisions", [])
            return _Mutation((_document_record(entry), created), changed=created)

        return self._mutate(mutate)

    # -- designer notation dictionary ------------------------------------

    def read_notation(self) -> dict[str, Any]:
        """The notation meanings a person confirmed, and nothing provisional."""

        if not self.notation_path.is_file():
            return empty_notation_dictionary()
        payload = self._read_json(self.notation_path)
        if not isinstance(payload, Mapping):
            raise StateError(f"{self.notation_path} must contain a JSON object")
        dictionary = dict(payload)
        dictionary.setdefault("entry_schema_version", 1)
        dictionary.setdefault("dictionary_version", None)
        dictionary.setdefault("updated_at", None)
        entries = dictionary.get("entries") or []
        if not isinstance(entries, list):
            raise StateError("The notation dictionary needs an entries list")
        for entry in entries:
            try:
                validate_notation_entry(entry)
            except ValueError as error:
                raise StateError(f"Unusable notation entry: {error}") from error
        dictionary["entries"] = [dict(entry) for entry in entries]
        return dictionary

    def write_notation(self, dictionary: Mapping[str, Any]) -> dict[str, Any]:
        entries = dictionary.get("entries") or []
        if not isinstance(entries, list):
            raise StateError("The notation dictionary needs an entries list")
        for entry in entries:
            try:
                validate_notation_entry(entry)
            except ValueError as error:
                raise StateError(f"Refusing to write a notation entry: {error}") from error
        payload = {
            "entry_schema_version": int(
                dictionary.get("entry_schema_version") or 1
            ),
            "dictionary_version": dictionary.get("dictionary_version")
            or empty_notation_dictionary()["dictionary_version"],
            "updated_at": _now(),
            "entries": [dict(entry) for entry in entries],
        }
        self._atomic_write_json(self.notation_path, payload)
        return payload

    # -- published revision bundles --------------------------------------
    def documents(self) -> dict[str, DocumentRecord]:
        manifest = self.load()
        return {
            key: _document_record(value) for key, value in manifest.documents.items()
        }

    def archive_source(
        self, relative_path: str, content: bytes, note: str = ""
    ) -> SourceRevisionRecord:
        """Archive exact bytes as a new source revision.

        Identical bytes are stored once, but every call still appends a
        revision event, so the archive keeps the history instead of collapsing
        a re-import into nothing.
        """

        normalized = normalize_relative_path(relative_path)
        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(normalized).suffix.lower()
        archive_relative = Path(ARCHIVE_DIRECTORY) / ARCHIVE_OBJECT_DIRECTORY
        object_path = self.directory / archive_relative / f"{digest}{suffix}"
        revision_id = source_revision_id(document_id(normalized), digest)

        def mutate(manifest: StateManifest) -> _Mutation:
            entry = manifest.documents.get(document_id(normalized))
            if entry is None:
                entry = _new_document_entry(normalized)
                manifest.documents[entry["document_id"]] = entry
            entry["path"] = normalized
            entry.setdefault("source_revisions", [])
            entry.setdefault("parse_revisions", [])

            if not object_path.is_file():
                self._atomic_write_bytes(object_path, content)

            record = SourceRevisionRecord(
                source_revision_id=revision_id,
                document_id=entry["document_id"],
                relative_path=normalized,
                content_sha256=digest,
                byte_size=len(content),
                archived_at=_now(),
                archive_path=(archive_relative / f"{digest}{suffix}").as_posix(),
                note=note,
            )
            self._append_journal(SOURCE_REVISION_JOURNAL, record.as_payload())
            if revision_id not in entry["source_revisions"]:
                entry["source_revisions"].append(revision_id)
            return _Mutation(record, changed=True)

        return self._mutate(mutate)

    def source_revision(self, revision_id: str) -> SourceRevisionRecord | None:
        for payload in reversed(self._journal(SOURCE_REVISION_JOURNAL)):
            if payload.get("source_revision_id") == revision_id:
                return SourceRevisionRecord.from_payload(payload)
        return None

    def source_revisions(self, document: str | None = None) -> list[SourceRevisionRecord]:
        records = [
            SourceRevisionRecord.from_payload(payload)
            for payload in self._journal(SOURCE_REVISION_JOURNAL)
        ]
        if document is None:
            return records
        wanted = document if document.startswith("doc-") else document_id(document)
        return [record for record in records if record.document_id == wanted]

    # -- parse revisions ------------------------------------------------

    def record_parse_revision(
        self,
        source_revision: str,
        *,
        database_schema_version: int,
        processing_manifest: ProcessingManifest | Mapping[str, Any],
        activate: bool = True,
    ) -> ParseRevisionRecord:
        """Bind a parse result to its source revision, schema, and pipeline."""

        source_record = self.source_revision(source_revision)
        if source_record is None:
            raise StateError(f"Unknown source revision: {source_revision}")
        manifest_payload = (
            processing_manifest.as_payload()
            if isinstance(processing_manifest, ProcessingManifest)
            else dict(processing_manifest)
        )
        fingerprint = str(
            manifest_payload.get("fingerprint")
            or ProcessingManifest.from_payload(manifest_payload).fingerprint
        )
        revision_id = parse_revision_id(
            source_revision,
            database_schema_version=database_schema_version,
            processing_fingerprint_value=fingerprint,
        )
        record = ParseRevisionRecord(
            parse_revision_id=revision_id,
            source_revision_id=source_revision,
            document_id=source_record.document_id,
            database_schema_version=database_schema_version,
            processing_fingerprint=fingerprint,
            created_at=_now(),
            processing_manifest=manifest_payload,
        )

        def mutate(manifest: StateManifest) -> _Mutation:
            entry = manifest.documents.get(source_record.document_id)
            if entry is None:
                entry = _new_document_entry(source_record.relative_path)
                manifest.documents[entry["document_id"]] = entry
            entry.setdefault("parse_revisions", [])
            self._append_journal(PARSE_REVISION_JOURNAL, record.as_payload())
            if revision_id not in entry["parse_revisions"]:
                entry["parse_revisions"].append(revision_id)
            if activate:
                entry["active_parse_revision"] = revision_id
            return _Mutation(record, changed=True)

        return self._mutate(mutate)

    def parse_revisions(self, document: str | None = None) -> list[ParseRevisionRecord]:
        records = [
            ParseRevisionRecord.from_payload(payload)
            for payload in self._journal(PARSE_REVISION_JOURNAL)
        ]
        if document is None:
            return records
        wanted = document if document.startswith("doc-") else document_id(document)
        return [record for record in records if record.document_id == wanted]

    def parse_revision(self, revision_id: str) -> ParseRevisionRecord | None:
        for payload in reversed(self._journal(PARSE_REVISION_JOURNAL)):
            if payload.get("parse_revision_id") == revision_id:
                return ParseRevisionRecord.from_payload(payload)
        return None

    def active_parse_revision(
        self, document: str | None = None
    ) -> ParseRevisionRecord | dict[str, ParseRevisionRecord] | None:
        """Return the parse revision reads should use, per document or for one."""

        manifest = self.load()
        if document is not None:
            wanted = document if document.startswith("doc-") else document_id(document)
            entry = manifest.documents.get(wanted)
            if entry is None or not entry.get("active_parse_revision"):
                return None
            return self.parse_revision(str(entry["active_parse_revision"]))

        active: dict[str, ParseRevisionRecord] = {}
        for key, entry in manifest.documents.items():
            revision = entry.get("active_parse_revision")
            if not revision:
                continue
            record = self.parse_revision(str(revision))
            if record is not None:
                active[key] = record
        return active

    # -- review events ---------------------------------------------------

    def append_review_event(
        self,
        action: str,
        *,
        subject_type: str,
        subject_id: str,
        payload: Mapping[str, Any] | None = None,
        actor: str = "",
        note: str = "",
        occurred_at: str | None = None,
    ) -> ReviewEvent:
        if not action.strip():
            raise StateError("a review event needs an action")
        if not subject_type.strip() or not subject_id.strip():
            raise StateError("a review event needs a subject")
        event = ReviewEvent(
            event_id=f"rev-{uuid.uuid4().hex[:16]}",
            occurred_at=occurred_at or _now(),
            actor=actor,
            action=action.strip(),
            subject_type=subject_type.strip(),
            subject_id=subject_id.strip(),
            note=note,
            payload=dict(payload or {}),
        )
        self._append_journal(REVIEW_EVENT_JOURNAL, event.as_payload())
        return event

    def review_events(
        self, subject_type: str | None = None, subject_id: str | None = None
    ) -> list[ReviewEvent]:
        events = [
            ReviewEvent.from_payload(payload)
            for payload in self._journal(REVIEW_EVENT_JOURNAL)
        ]
        if subject_type is not None:
            events = [event for event in events if event.subject_type == subject_type]
        if subject_id is not None:
            events = [event for event in events if event.subject_id == subject_id]
        return events

    # -- confirmed dictionary -------------------------------------------

    def read_dictionary(self) -> dict[str, Any]:
        if not self.dictionary_path.is_file():
            return empty_dictionary()
        payload = self._read_json(self.dictionary_path)
        if not isinstance(payload, Mapping):
            raise StateError(f"{self.dictionary_path} must contain a JSON object")
        dictionary = dict(payload)
        dictionary.setdefault("entry_schema_version", ENTRY_SCHEMA_VERSION)
        dictionary.setdefault("updated_at", None)
        entries = dictionary.get("entries") or []
        if not isinstance(entries, list):
            raise StateError("The confirmed dictionary needs an entries list")
        for entry in entries:
            _validate_dictionary_entry(entry)
        dictionary["entries"] = [dict(entry) for entry in entries]
        return dictionary

    def write_dictionary(self, dictionary: Mapping[str, Any]) -> dict[str, Any]:
        entries = dictionary.get("entries") or []
        if not isinstance(entries, list):
            raise StateError("The confirmed dictionary needs an entries list")
        for entry in entries:
            _validate_dictionary_entry(entry)
        payload = {
            "entry_schema_version": ENTRY_SCHEMA_VERSION,
            "updated_at": _now(),
            "entries": [dict(entry) for entry in entries],
        }
        self._atomic_write_json(self.dictionary_path, payload)
        return payload

    def confirm_alias(
        self,
        feature_key: str,
        canonical_name: str,
        alias: str,
        *,
        source: str,
        confirmed_by: str,
        actor: str = "",
        confirmed_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Record one human-confirmed alias and the review event behind it."""

        if not feature_key.strip() or not canonical_name.strip() or not alias.strip():
            raise StateError("feature_key, canonical_name, and alias are required")
        stamp = confirmed_at or _now()

        def mutate(manifest: StateManifest) -> _Mutation:
            dictionary = self.read_dictionary()
            entry = next(
                (
                    item
                    for item in dictionary["entries"]
                    if item.get("feature_key") == feature_key
                ),
                None,
            )
            if entry is None:
                entry = {
                    "feature_key": feature_key,
                    "canonical_name": canonical_name,
                    "source": source,
                    "aliases": [],
                }
                dictionary["entries"].append(entry)
            aliases = entry.setdefault("aliases", [])
            if not any(item["name"] == alias for item in aliases):
                aliases.append(
                    {
                        "name": alias,
                        "source": source,
                        "confirmed_at": stamp,
                        "confirmed_by": confirmed_by,
                    }
                )
            stored = self.write_dictionary(dictionary)
            self.append_review_event(
                "confirm_alias",
                subject_type="alias",
                subject_id=f"{feature_key}:{alias}",
                payload={
                    "feature_key": feature_key,
                    "canonical_name": canonical_name,
                    "alias": alias,
                    "source": source,
                    "confirmed_at": stamp,
                    "confirmed_by": confirmed_by,
                },
                actor=actor or confirmed_by,
                note=note,
            )
            return _Mutation(stored, changed=True)

        return self._mutate(mutate)

    # -- published revision bundles --------------------------------------

    def write_bundle(
        self, parse_revision: str, payload: Mapping[str, Any], note: str = ""
    ) -> dict[str, Any]:
        record = self.parse_revision(parse_revision)
        if record is None:
            raise StateError(f"Unknown parse revision: {parse_revision}")
        identifier = bundle_id(parse_revision, payload)
        directory = self.bundle_directory / identifier
        directory.mkdir(parents=True, exist_ok=True)
        units = payload.get("retrieval_units") or []
        manifest = {
            "bundle_schema_version": 1,
            "bundle_id": identifier,
            "parse_revision_id": parse_revision,
            "source_revision_id": record.source_revision_id,
            "document_id": record.document_id,
            "created_at": _now(),
            "payload_sha256": _canonical_sha256(payload),
            "retrieval_unit_count": len(units),
            "note": note,
        }
        self._atomic_write_json(directory / "payload.json", dict(payload))
        self._atomic_write_json(directory / "manifest.json", manifest)
        return {
            "bundle_id": identifier,
            "directory": str(directory),
            "manifest": manifest,
        }

    def bundle_ids(self) -> list[str]:
        if not self.bundle_directory.is_dir():
            return []
        return sorted(
            path.name for path in self.bundle_directory.iterdir() if path.is_dir()
        )

    def verify_bundle(self, identifier: str) -> dict[str, Any]:
        """Re-check a published bundle all the way back to archived bytes."""

        checks: list[dict[str, Any]] = []
        directory = self.bundle_directory / identifier
        manifest_path = directory / "manifest.json"
        payload_path = directory / "payload.json"
        if not manifest_path.is_file() or not payload_path.is_file():
            checks.append(
                _check("bundle_files", False, f"bundle {identifier} is incomplete")
            )
            return {
                "ok": False,
                "bundle_id": identifier,
                "parse_revision_id": None,
                "checks": checks,
            }

        manifest = self._read_json(manifest_path)
        payload = self._read_json(payload_path)
        checks.append(_check("bundle_files", True, "manifest and payload are present"))
        checks.append(
            _check(
                "manifest_id",
                manifest.get("bundle_id") == identifier,
                f"manifest records {manifest.get('bundle_id')!r}",
            )
        )
        recomputed = bundle_id(str(manifest.get("parse_revision_id")), payload)
        checks.append(
            _check(
                "payload_hash",
                recomputed == identifier
                and manifest.get("payload_sha256") == _canonical_sha256(payload),
                "recomputed bundle id and payload digest",
            )
        )

        parse_record = self.parse_revision(str(manifest.get("parse_revision_id")))
        checks.append(
            _check(
                "parse_revision",
                parse_record is not None,
                f"parse revision {manifest.get('parse_revision_id')!r} is recorded",
            )
        )
        source_record = (
            self.source_revision(parse_record.source_revision_id)
            if parse_record is not None
            else None
        )
        checks.append(
            _check(
                "source_revision",
                source_record is not None,
                "the parse revision points at a recorded source revision",
            )
        )

        if source_record is not None:
            archived = self.directory / source_record.archive_path
            actual = file_sha256(archived) if archived.is_file() else None
            checks.append(
                _check(
                    "archived_bytes",
                    actual == source_record.content_sha256,
                    f"archive {source_record.archive_path} hashes to {actual}",
                )
            )

        units = payload.get("retrieval_units") or []
        missing = [
            index
            for index, unit in enumerate(units)
            if not isinstance(unit, Mapping) or not unit.get("locator")
        ]
        checks.append(
            _check(
                "retrieval_unit_locators",
                not missing and bool(units),
                f"{len(units)} retrieval units, missing locators at {missing}",
            )
        )

        return {
            "ok": all(check["passed"] for check in checks),
            "bundle_id": identifier,
            "parse_revision_id": manifest.get("parse_revision_id"),
            "checks": checks,
        }

    # -- status -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        if not self.exists():
            return {
                "state_schema_version": STATE_SCHEMA_VERSION,
                "directory": str(self.directory),
                "initialized": False,
                "updated_at": None,
                "documents": 0,
                "source_revisions": 0,
                "parse_revisions": 0,
                "active_parse_revisions": 0,
                "review_events": 0,
                "dictionary_entries": 0,
                "dictionary_aliases": 0,
                "notation_entries": 0,
                "notation_confirmed": 0,
                "notation_superseded": 0,
                "notation_rejected": 0,
                "archived_objects": 0,
                "archived_bytes": 0,
                "bundles": 0,
            }

        manifest = self.load()
        dictionary = self.read_dictionary()
        notation = self.read_notation()
        active = manifest.documents.values()
        return {
            "state_schema_version": manifest.state_schema_version,
            "directory": str(self.directory),
            "initialized": True,
            "updated_at": manifest.updated_at,
            "documents": len(manifest.documents),
            "source_revisions": sum(
                len(entry.get("source_revisions") or ()) for entry in active
            ),
            "parse_revisions": sum(
                len(entry.get("parse_revisions") or ()) for entry in active
            ),
            "active_parse_revisions": sum(
                1 for entry in active if entry.get("active_parse_revision")
            ),
            "review_events": len(self.review_events()),
            "dictionary_entries": len(dictionary["entries"]),
            "dictionary_aliases": sum(
                len(entry.get("aliases") or ()) for entry in dictionary["entries"]
            ),
            "notation_entries": len(notation["entries"]),
            "notation_confirmed": _notation_count(notation, NOTATION_CONFIRMED),
            "notation_superseded": _notation_count(notation, NOTATION_SUPERSEDED),
            "notation_rejected": _notation_count(notation, NOTATION_REJECTED),
            "archived_objects": _count_files(self.archive_directory),
            "archived_bytes": _directory_bytes(self.archive_directory),
            "bundles": len(self.bundle_ids()),
        }

    # -- internals ---------------------------------------------------------

    def _mutate(self, mutate: Callable[[StateManifest], _Mutation]) -> T:
        with self.lock():
            manifest = self.load()
            outcome = mutate(manifest)
            if outcome.changed:
                self._write_manifest(manifest)
            return outcome.result

    def _write_manifest(self, manifest: StateManifest) -> None:
        manifest.updated_at = _now()
        if self.manifest_path.is_file():
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                self.manifest_path,
                self.backup_directory / f"manifest-{_stamp()}-{uuid.uuid4().hex[:6]}.json",
            )
            self._prune_backups()
        self._atomic_write_json(self.manifest_path, manifest.as_payload())

    def _prune_backups(self) -> None:
        if not self.backup_directory.is_dir():
            return
        backups = sorted(
            (path for path in self.backup_directory.iterdir()),
            key=lambda path: path.name,
        )
        for path in backups[:-BACKUP_LIMIT] if len(backups) > BACKUP_LIMIT else []:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    def _journal(self, name: str) -> list[dict[str, Any]]:
        path = self.journal_directory / name
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise StateError(f"{path}:{number} is not valid JSON: {error}") from error
            if not isinstance(payload, Mapping):
                raise StateError(f"{path}:{number} must be a JSON object")
            records.append(dict(payload))
        return records

    def _append_journal(self, name: str, payload: Mapping[str, Any]) -> None:
        self.journal_directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        with (self.journal_directory / name).open("a", encoding="utf-8") as journal:
            journal.write(f"{line}\n")
            journal.flush()
            os.fsync(journal.fileno())

    def _reconcile(self, manifest: StateManifest) -> StateManifest:
        """Replay journals into the manifest so a lost index heals, not lies."""

        changed = False
        for record in self.source_revisions():
            entry = manifest.documents.get(record.document_id)
            if entry is None:
                entry = _new_document_entry(record.relative_path)
                entry["created_at"] = record.archived_at
                manifest.documents[record.document_id] = entry
                changed = True
            if record.source_revision_id not in entry.setdefault("source_revisions", []):
                entry["source_revisions"].append(record.source_revision_id)
                changed = True
        for parse_record in self.parse_revisions():
            entry = manifest.documents.get(parse_record.document_id)
            if entry is None:
                continue
            if parse_record.parse_revision_id not in entry.setdefault(
                "parse_revisions", []
            ):
                entry["parse_revisions"].append(parse_record.parse_revision_id)
                changed = True
        if changed:
            self._write_manifest(manifest)
        return manifest

    def _lock_is_stale(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except OSError:
            return False
        return age > LOCK_STALE_SECONDS

    def _read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StateError(f"{path} is not readable JSON: {error}") from error

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self._atomic_write_text(path, f"{text}\n")

    def _atomic_write_text(self, path: Path, text: str) -> None:
        self._atomic_write_bytes(path, text.encode("utf-8"))

    def _atomic_write_bytes(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

def _new_document_entry(relative_path: str) -> dict[str, Any]:
    return {
        "document_id": document_id(relative_path),
        "path": relative_path,
        "created_at": _now(),
        "source_revisions": [],
        "parse_revisions": [],
        "active_parse_revision": None,
    }


def _document_record(entry: Mapping[str, Any]) -> DocumentRecord:
    source_revisions = tuple(str(item) for item in entry.get("source_revisions") or ())
    parse_revisions = tuple(str(item) for item in entry.get("parse_revisions") or ())
    return DocumentRecord(
        document_id=str(entry["document_id"]),
        relative_path=str(entry.get("path") or ""),
        created_at=str(entry.get("created_at") or ""),
        source_revisions=source_revisions,
        parse_revisions=parse_revisions,
        latest_source_revision=source_revisions[-1] if source_revisions else None,
        active_parse_revision=entry.get("active_parse_revision"),
    )


def _validate_dictionary_entry(entry: Any) -> None:
    if not isinstance(entry, Mapping):
        raise StateError("Every confirmed dictionary entry must be a JSON object")
    for key in ("feature_key", "canonical_name"):
        if not str(entry.get(key) or "").strip():
            raise StateError(f"A confirmed dictionary entry needs {key}")
    aliases = entry.get("aliases") or []
    if not isinstance(aliases, list):
        raise StateError("Confirmed dictionary aliases must be a list")
    for alias in aliases:
        if not isinstance(alias, Mapping):
            raise StateError("Every confirmed alias must be a JSON object")
        for key in ("name", "source", "confirmed_at", "confirmed_by"):
            if not str(alias.get(key) or "").strip():
                raise StateError(
                    f"Confirmed alias {alias.get('name')!r} needs {key}; "
                    "the MCP server never confirms an alias by itself"
                )


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _notation_count(dictionary: Mapping[str, Any], status: str) -> int:
    return sum(
        1
        for entry in dictionary.get("entries") or []
        if str(entry.get("status")) == str(status)
    )


def _count_files(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.rglob("*") if path.is_file())


def _directory_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def _canonical_sha256(payload: Any) -> str:
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "ARCHIVE_DIRECTORY",
    "BACKUP_LIMIT",
    "BACKUP_DIRECTORY",
    "BUNDLE_DIRECTORY",
    "DEFAULT_STATE_DIRECTORY_NAME",
    "DICTIONARY_NAME",
    "DocumentRecord",
    "DurableState",
    "ENTRY_SCHEMA_VERSION",
    "JOURNAL_DIRECTORY",
    "LOCK_NAME",
    "LOCK_STALE_SECONDS",
    "MANIFEST_NAME",
    "NOTATION_DICTIONARY_NAME",
    "PARSE_REVISION_JOURNAL",
    "ParseRevisionRecord",
    "REVIEW_EVENT_JOURNAL",
    "ReviewEvent",
    "SOURCE_REVISION_JOURNAL",
    "STATE_SCHEMA_VERSION",
    "SourceRevisionRecord",
    "StateError",
    "StateLocked",
    "StateManifest",
    "StateVersionError",
    "empty_dictionary",
    "empty_notation_dictionary",
    "state_directory_for",
    "validate_notation_entry",
]
