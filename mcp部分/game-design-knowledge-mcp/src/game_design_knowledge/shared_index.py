from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import TracebackType
from typing import Any

from .indexer import catalog_fingerprint


_FRESHNESS_HASH_CACHE: dict[tuple[str, int, int], str] = {}


class SharedIndexRead:
    """Own one shared-index read and its freshness snapshot."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._connection: sqlite3.Connection | None = None
        self._status: dict[str, object] | None = None

    def __enter__(self) -> SharedIndexRead:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def fetchone(
        self, sql: str, parameters: tuple[object, ...] | list[object] = ()
    ) -> sqlite3.Row | None:
        return self._active_connection().execute(sql, parameters).fetchone()

    def fetchall(
        self, sql: str, parameters: tuple[object, ...] | list[object] = ()
    ) -> list[sqlite3.Row]:
        return self._active_connection().execute(sql, parameters).fetchall()

    def status(self) -> dict[str, object]:
        if self._status is not None:
            return self._status

        connection = self._active_connection()
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM documents) AS documents_indexed,
                COUNT(*) AS images_indexed,
                COALESCE(SUM(ocr_status = 'succeeded'), 0) AS ocr_succeeded,
                COALESCE(SUM(ocr_status = 'failed'), 0) AS ocr_failed,
                COALESCE(SUM(ocr_status = 'unavailable'), 0) AS ocr_unavailable
            FROM images
            """
        ).fetchone()
        documents = connection.execute(
            """
            SELECT path, source_size, source_mtime_ns, source_sha256, indexed_at
            FROM documents
            """
        ).fetchall()
        catalog = connection.execute(
            """
            SELECT path, source_size, source_mtime_ns, source_sha256, indexed_at
            FROM catalog_metadata WHERE id = 1
            """
        ).fetchone()

        stale_documents = sum(
            not self._source_matches_index(
                self.resolve_source_path(document["path"]), document
            )
            for document in documents
        )
        indexed_at = max(
            (document["indexed_at"] for document in documents), default=None
        )
        catalog_is_stale = False
        if catalog is not None:
            catalog_is_stale = not self._catalog_matches_index(
                self.resolve_source_path(catalog["path"]), catalog
            )
            if indexed_at is None or catalog["indexed_at"] > indexed_at:
                indexed_at = catalog["indexed_at"]

        self._status = {
            "schema_version": schema_version,
            "database_path": str(self.database_path),
            "indexed_at": indexed_at,
            "documents_indexed": counts["documents_indexed"],
            "images_indexed": counts["images_indexed"],
            "ocr_succeeded": counts["ocr_succeeded"],
            "ocr_failed": counts["ocr_failed"],
            "ocr_unavailable": counts["ocr_unavailable"],
            "stale_documents": stale_documents,
            "catalog_configured": catalog is not None,
            "catalog_is_stale": catalog_is_stale,
            "is_stale": stale_documents > 0 or catalog_is_stale,
        }
        return self._status

    def complete(self, result: dict[str, Any]) -> dict[str, Any]:
        """Attach one consistent status snapshot to every read result."""
        status = self.status()
        result.setdefault("status", "found")
        if status["is_stale"]:
            result["status"] = "stale"
        result["index_status"] = status
        return result

    def resolve_source_path(self, stored_path: str) -> Path:
        source_path = Path(stored_path)
        if source_path.is_absolute():
            return source_path
        return (self.database_path.parent / source_path).resolve()

    def resolve_result_source(self, item: dict[str, Any]) -> None:
        source_document = item.get("source_document")
        if source_document is not None:
            item["source_document"] = str(
                self.resolve_source_path(str(source_document))
            )

    def stored_path_selector(self, value: str | None) -> str | None:
        if value is None:
            return None
        requested_path = Path(value)
        if not requested_path.is_absolute():
            return value.replace("\\", "/")
        try:
            return Path(
                os.path.relpath(requested_path, self.database_path.parent)
            ).as_posix()
        except ValueError:
            return str(requested_path)

    def _active_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SharedIndexRead must be used as a context manager")
        return self._connection

    @staticmethod
    def _source_matches_index(source_path: Path, record: sqlite3.Row) -> bool:
        try:
            source_stat = source_path.stat()
        except OSError:
            return False
        if source_stat.st_size != record["source_size"]:
            return False

        cache_key = (str(source_path), source_stat.st_size, source_stat.st_mtime_ns)
        actual_hash = _FRESHNESS_HASH_CACHE.get(cache_key)
        if actual_hash is None:
            digest = hashlib.sha256()
            with source_path.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual_hash = digest.hexdigest()
            _FRESHNESS_HASH_CACHE[cache_key] = actual_hash
        return actual_hash == record["source_sha256"]

    @staticmethod
    def _catalog_matches_index(catalog_path: Path, record: sqlite3.Row) -> bool:
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return catalog_fingerprint(catalog) == record["source_sha256"]


def index_status_for_database(database_path: Path) -> dict[str, object]:
    with SharedIndexRead(database_path) as index:
        return index.status()
