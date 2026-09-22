"""Immutable index snapshots and the one atomic step that makes one active.

Each build writes into ``<index>/snapshots/<build-id>/``. A snapshot only
becomes active after it validates, and the switch itself is a rename of the
database plus a pointer file, so a failed, cancelled, or interrupted build
leaves the previous snapshot readable and active.

Nothing here is authoritative: the active snapshot is a rebuildable projection
of the project's documents, and everything a human decided lives in
``game_design_knowledge.state``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
from typing import Any, Iterator, Mapping

from .revisions import file_sha256


CURRENT_NAME = "CURRENT.json"
DATABASE_NAME = "knowledge.sqlite"
ASSETS_DIRECTORY = "assets"
BUILD_MANIFEST_NAME = "build.json"
PUBLISH_LOCK_NAME = ".publish.lock"
LOCK_STALE_SECONDS = 900.0
SNAPSHOT_LIMIT = 5
POINTER_SCHEMA_VERSION = 1

BUILD_STATES = frozenset(
    {"planned", "running", "validated", "published", "failed", "recovered"}
)
PUBLISHABLE_STATES = frozenset({"validated"})
VERIFIABLE_STATES = frozenset({"validated", "published", "recovered"})

REQUIRED_TABLES = frozenset(
    {
        "documents",
        "document_blocks",
        "evidence",
        "evidence_fts",
        "images",
        "image_fts",
        "workbook_sheets",
        "sheet_cells",
        "cell_fts",
        "catalog_features",
        "catalog_aliases",
        "catalog_metadata",
        "index_builds",
        "schema_migrations",
        "source_revisions",
        "parse_revisions",
        "processing_manifests",
        "stage_attempts",
    }
)


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be built, validated, or published."""


class SnapshotStateError(SnapshotError):
    """Raised when a build is asked to do something its state forbids."""


def new_build_id() -> str:
    """A build id that sorts by creation order and stays unique."""

    now = datetime.now(timezone.utc)
    # Microseconds keep build ids sortable by creation even within one second,
    # which is what recovery and pruning rely on.
    stamp = f"{now.strftime('%Y%m%dT%H%M%S')}{now.microsecond:06d}"
    return f"build-{stamp}-{uuid.uuid4().hex[:6]}"


def snapshot_directory_for(index_directory: Path, build_id: str) -> Path:
    """One snapshot directory: a sibling of the index, never inside it.

    Two properties matter. The published directory keeps exactly the shape V1
    readers expect, and a first build that fails leaves no half-created index
    behind. The snapshot also sits at the same depth as the index, so the
    project-relative source paths stored in the database keep resolving after
    the snapshot is published.
    """

    index_directory = Path(index_directory)
    return index_directory.parent / f".{index_directory.name}.{build_id}"


def verify_snapshot(
    directory: Path, expected_schema_version: int | None = None
) -> dict[str, Any]:
    """Check one snapshot directory without trusting any of its metadata."""

    directory = Path(directory).resolve()
    database_path = directory / DATABASE_NAME
    checks: list[dict[str, Any]] = []
    if not database_path.is_file():
        return {
            "ok": False,
            "status": "missing",
            "schema_version": None,
            "checks": [_check("database_present", False, f"no {DATABASE_NAME}")],
        }
    checks.append(_check("database_present", True, str(database_path)))

    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assets = [
            row[0]
            for row in connection.execute("SELECT DISTINCT asset_path FROM images")
        ]
    except sqlite3.DatabaseError as error:
        checks.append(_check("readable", False, f"{type(error).__name__}: {error}"))
        return {
            "ok": False,
            "status": "corrupt",
            "schema_version": None,
            "checks": checks,
        }
    finally:
        connection.close()

    version_ok = (
        expected_schema_version is None or schema_version == expected_schema_version
    )
    checks.append(
        _check(
            "schema_version",
            version_ok,
            f"snapshot schema {schema_version}, expected {expected_schema_version}",
        )
    )
    checks.append(_check("integrity_check", integrity == "ok", str(integrity)))
    checks.append(
        _check(
            "foreign_keys",
            not foreign_key_problems,
            f"{len(foreign_key_problems)} foreign key violations",
        )
    )
    missing_tables = sorted(REQUIRED_TABLES - tables)
    checks.append(
        _check(
            "required_tables",
            not missing_tables,
            f"missing tables: {missing_tables}",
        )
    )
    missing_assets = [
        asset for asset in assets if not (directory / asset).is_file()
    ]
    checks.append(
        _check(
            "assets_present",
            not missing_assets,
            f"{len(missing_assets)} of {len(assets)} assets are missing",
        )
    )

    ok = all(check["passed"] for check in checks)
    if ok:
        status = "valid"
    elif not version_ok:
        status = "incompatible"
    else:
        status = "corrupt"
    return {
        "ok": ok,
        "status": status,
        "schema_version": schema_version,
        "checks": checks,
    }


@dataclass
class IndexBuild:
    """One planned, running, validated, and finally published snapshot."""

    store: "IndexSnapshotStore"
    build_id: str
    note: str = ""
    state: str = "planned"
    started_at: str = field(default_factory=lambda: _now())
    finished_at: str | None = None
    error: str | None = None
    parse_revisions: dict[str, str] = field(default_factory=dict)
    validation: dict[str, Any] | None = None
    report: dict[str, Any] | None = None

    @property
    def directory(self) -> Path:
        return self.store.snapshot_directory(self.build_id)

    @property
    def database_path(self) -> Path:
        return self.directory / DATABASE_NAME

    @property
    def assets_directory(self) -> Path:
        return self.directory / ASSETS_DIRECTORY

    @property
    def manifest_path(self) -> Path:
        return self.directory / BUILD_MANIFEST_NAME

    def __enter__(self) -> "IndexBuild":
        self.store._prepare_snapshot(self)
        self.state = "running"
        self._write()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if exc_value is not None and self.state in {"planned", "running"}:
            self.fail(f"{type(exc_value).__name__}: {exc_value}")

    def validate(self) -> dict[str, Any]:
        result = verify_snapshot(
            self.directory, expected_schema_version=self.store.expected_schema_version
        )
        self.validation = result
        if not result["ok"]:
            self.state = "failed"
            self.finished_at = _now()
            self.error = f"snapshot failed validation: {result['status']}"
            self._write()
            raise SnapshotStateError(
                f"Snapshot {self.build_id} is {result['status']} and cannot be "
                f"published: {_failed_checks(result)}"
            )
        self.state = "validated"
        self._write()
        return result

    def publish(self) -> dict[str, Any]:
        if self.state not in PUBLISHABLE_STATES:
            raise SnapshotStateError(
                f"Snapshot {self.build_id} is {self.state}; only a validated "
                "snapshot may become active"
            )
        record = self.store._publish_snapshot(self.build_id)
        self.state = "published"
        self.finished_at = _now()
        self._write()
        return record

    def fail(self, reason: str) -> None:
        self.state = "failed"
        self.error = reason
        self.finished_at = _now()
        self._write()

    def record_parse_revisions(self, revisions: Mapping[str, str]) -> None:
        self.parse_revisions = {str(key): str(value) for key, value in revisions.items()}
        self._write()

    def as_payload(self) -> dict[str, Any]:
        return {
            "build_schema_version": 1,
            "build_id": self.build_id,
            "state": self.state,
            "note": self.note,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "parse_revisions": self.parse_revisions,
            "validation": self.validation,
            "report": self.report,
        }

    def _write(self) -> None:
        self.store._atomic_write_json(self.manifest_path, self.as_payload())


class IndexSnapshotStore:
    """Own the snapshot directory, the active files, and the pointer."""

    def __init__(
        self,
        index_directory: Path,
        *,
        snapshot_parent: Path | None = None,
        expected_schema_version: int | None = None,
        snapshot_limit: int = SNAPSHOT_LIMIT,
    ) -> None:
        self.index_directory = Path(index_directory).resolve()
        self.snapshot_parent = (
            Path(snapshot_parent).resolve()
            if snapshot_parent is not None
            else self.index_directory.parent
        )
        self.expected_schema_version = expected_schema_version
        self.snapshot_limit = snapshot_limit

    @property
    def current_path(self) -> Path:
        return self.index_directory / CURRENT_NAME

    @property
    def database_path(self) -> Path:
        return self.index_directory / DATABASE_NAME

    @property
    def assets_directory(self) -> Path:
        return self.index_directory / ASSETS_DIRECTORY

    @property
    def lock_path(self) -> Path:
        # The lock lives beside the snapshots, so a killed publish cannot leave
        # a stray file inside the published index.
        return self.snapshot_parent / f".{self.index_directory.name}.{PUBLISH_LOCK_NAME}"

    def snapshot_directory(self, build_id: str) -> Path:
        return snapshot_directory_for(self.index_directory, build_id)

    def new_build(self, note: str = "") -> IndexBuild:
        return IndexBuild(store=self, build_id=new_build_id(), note=note)

    def read_pointer(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return the whole pointer payload and, if unreadable, why."""

        if not self.current_path.is_file():
            return None, f"{CURRENT_NAME} does not exist"
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return None, f"{CURRENT_NAME} is unreadable: {error}"
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("current"), Mapping
        ):
            return None, f"{CURRENT_NAME} has no current snapshot record"
        return dict(payload), None

    def current_snapshot(self) -> dict[str, Any] | None:
        """The record of the snapshot that is currently active."""

        pointer = self.read_pointer()[0]
        if pointer is None:
            return None
        entry = pointer.get("current")
        return dict(entry) if isinstance(entry, Mapping) else None

    def last_known_good(self) -> dict[str, Any] | None:
        pointer = self.read_pointer()[0]
        if pointer is None:
            return None
        entry = pointer.get("last_known_good")
        return dict(entry) if isinstance(entry, Mapping) else None

    def build_record(self, build_id: str) -> dict[str, Any] | None:
        manifest_path = self.snapshot_directory(build_id) / BUILD_MANIFEST_NAME
        if not manifest_path.is_file():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, Mapping) else None

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        records = [
            record
            for record in (
                self.build_record(self.snapshot_build_id(path))
                for path in self._snapshot_directories()
            )
            if record is not None
        ]
        records.sort(
            key=lambda record: (
                str(record.get("started_at") or ""),
                str(record.get("build_id") or ""),
            ),
            reverse=True,
        )
        return records[:limit]

    def verify(self, build_id: str) -> dict[str, Any]:
        return verify_snapshot(
            self.snapshot_directory(build_id),
            expected_schema_version=self.expected_schema_version,
        )

    def promote(self, build_id: str, note: str = "recovery") -> dict[str, Any]:
        """Make an already validated snapshot active again."""

        record = self.build_record(build_id)
        if record is None:
            raise SnapshotError(f"Unknown snapshot: {build_id}")
        state = str(record.get("state") or "")
        if state not in VERIFIABLE_STATES:
            raise SnapshotStateError(
                f"Snapshot {build_id} is {state!r}; only a validated snapshot may "
                "become active"
            )
        verification = self.verify(build_id)
        if not verification["ok"]:
            raise SnapshotStateError(
                f"Snapshot {build_id} no longer verifies: {_failed_checks(verification)}"
            )
        published = self._publish_snapshot(build_id)
        record.update({"state": "recovered", "note": note, "finished_at": _now()})
        self._atomic_write_json(
            self.snapshot_directory(build_id) / BUILD_MANIFEST_NAME, record
        )
        return published

    def recovery_plan(self) -> dict[str, Any]:
        """Say what could become active when the pointer cannot be trusted."""

        payload, error = self.read_pointer()
        if payload is not None:
            return {
                "status": "pointer_readable",
                "current": payload.get("current"),
                "last_known_good": payload.get("last_known_good"),
                "error": None,
                "candidate": None,
                "scanned": [],
            }

        scanned: list[dict[str, Any]] = []
        candidate: dict[str, Any] | None = None
        for path in sorted(self._snapshot_directories(), reverse=True):
            build_id = self.snapshot_build_id(path)
            record = self.build_record(build_id)
            state = str(record.get("state") or "unknown") if record else "unknown"
            entry: dict[str, Any] = {"build_id": build_id, "state": state}
            if record is not None and state in VERIFIABLE_STATES:
                verification = self.verify(build_id)
                entry["verification"] = verification
                entry["started_at"] = record.get("started_at")
                if verification["ok"] and candidate is None:
                    candidate = {
                        "build_id": build_id,
                        "started_at": record.get("started_at"),
                        "parse_revisions": record.get("parse_revisions") or {},
                    }
            scanned.append(entry)

        return {
            "status": "recovery_available" if candidate else "no_recovery_source",
            "current": None,
            "last_known_good": None,
            "error": error,
            "candidate": candidate,
            "scanned": scanned,
        }

    def recover(self) -> dict[str, Any]:
        plan = self.recovery_plan()
        if plan["status"] != "recovery_available":
            return {"status": plan["status"], "plan": plan, "published": None}
        candidate = plan["candidate"]
        published = self.promote(candidate["build_id"], note="recovery")
        return {
            "status": "recovered",
            "plan": plan,
            "published": published,
        }

    def prune(self) -> list[str]:
        """Keep the active snapshot, the last known good one, and recent builds."""

        directories = sorted(self._snapshot_directories(), reverse=True)
        keep = {
            str(entry["build_id"])
            for entry in (self.current_snapshot(), self.last_known_good())
            if isinstance(entry, Mapping) and entry.get("build_id")
        }
        keep.update(
            self.snapshot_build_id(path) for path in directories[: self.snapshot_limit]
        )
        removed: list[str] = []
        for path in directories:
            build_id = self.snapshot_build_id(path)
            if build_id in keep:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed.append(build_id)
        return removed

    # -- internals ---------------------------------------------------------

    def _snapshot_directories(self) -> list[Path]:
        if not self.snapshot_parent.is_dir():
            return []
        prefix = self._snapshot_prefix()
        return [
            path
            for path in self.snapshot_parent.iterdir()
            if path.is_dir() and path.name.startswith(f"{prefix}build-")
        ]

    def snapshot_build_id(self, path: Path) -> str:
        """The build id a snapshot directory name encodes."""

        name = Path(path).name
        prefix = self._snapshot_prefix()
        return name[len(prefix) :] if name.startswith(prefix) else name

    def _snapshot_prefix(self) -> str:
        """The directory-name prefix that precedes a build id."""

        return f".{self.index_directory.name}."

    def _prepare_snapshot(self, build: IndexBuild) -> None:
        """Seed the snapshot from the active index so increments stay increments."""

        build.directory.mkdir(parents=True, exist_ok=False)
        if self.database_path.is_file():
            shutil.copy2(self.database_path, build.database_path)
        if self.assets_directory.is_dir():
            shutil.copytree(self.assets_directory, build.assets_directory)

    def _publish_snapshot(self, build_id: str) -> dict[str, Any]:
        directory = self.snapshot_directory(build_id)
        database_path = directory / DATABASE_NAME
        if not database_path.is_file():
            raise SnapshotError(f"Snapshot {build_id} has no database to publish")
        record = self.build_record(build_id) or {}

        with self._lock():
            assets = directory / ASSETS_DIRECTORY
            if assets.is_dir():
                self._replace_directory(assets, self.assets_directory)
            try:
                _publish_file(database_path, self.database_path)
            except OSError as error:
                raise SnapshotError(
                    "Could not publish the snapshot database while another process "
                    f"holds {self.database_path}: {error}"
                ) from error

            previous = self.current_snapshot()
            published = {
                "build_id": build_id,
                "snapshot_directory": str(directory),
                "published_at": _now(),
                "database_sha256": file_sha256(database_path),
                "database_size": database_path.stat().st_size,
                "parse_revisions": record.get("parse_revisions") or {},
                "note": record.get("note") or "",
            }
            pointer = {
                "pointer_schema_version": POINTER_SCHEMA_VERSION,
                "updated_at": _now(),
                "current": published,
                "last_known_good": previous,
            }
            self._atomic_write_json(self.current_path, pointer)
            self.prune()
            return published

    def _replace_directory(self, source: Path, destination: Path) -> None:
        """Swap a rebuilt directory in without ever leaving it half-written."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(f".{destination.name}.staging-{uuid.uuid4().hex[:8]}")
        retired = destination.with_name(f".{destination.name}.retired-{uuid.uuid4().hex[:8]}")
        try:
            shutil.copytree(source, staging)
            if destination.exists():
                os.replace(destination, retired)
            os.replace(staging, destination)
        except OSError as error:
            if not destination.exists() and retired.exists():
                os.replace(retired, destination)
            raise SnapshotError(
                f"Could not publish the snapshot assets to {destination}: {error}"
            ) from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)

    @contextmanager
    def _lock(self, timeout: float = 30.0) -> Iterator[None]:
        self.index_directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
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
                    raise SnapshotError(
                        f"Another process is publishing this index: {self.lock_path}"
                    ) from None
                time.sleep(0.05)
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("utf-8"))
        finally:
            os.close(descriptor)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _lock_is_stale(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except OSError:
            return False
        return age > LOCK_STALE_SECONDS

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(f"{text}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def _publish_file(source: Path, destination: Path) -> None:
    temporary = destination.with_name(
        f".{destination.name}.publish-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _failed_checks(result: Mapping[str, Any]) -> str:
    return "; ".join(
        f"{check['check']}: {check['detail']}"
        for check in result.get("checks") or ()
        if not check.get("passed")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "ASSETS_DIRECTORY",
    "BUILD_MANIFEST_NAME",
    "BUILD_STATES",
    "CURRENT_NAME",
    "DATABASE_NAME",
    "IndexBuild",
    "IndexSnapshotStore",
    "POINTER_SCHEMA_VERSION",
    "PUBLISH_LOCK_NAME",
    "REQUIRED_TABLES",
    "SNAPSHOT_LIMIT",
    "SnapshotError",
    "SnapshotStateError",
    "new_build_id",
    "snapshot_directory_for",
    "verify_snapshot",
]
