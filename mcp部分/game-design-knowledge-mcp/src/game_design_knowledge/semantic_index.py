"""A disposable vector sidecar: derived, rebuildable, and never an answer.

The vector index stores one thing that matters -- a unit id, a content hash and
a vector bound to the model that produced it -- and nothing that may be quoted
to a caller. Text from a vector record is search input only; every hit is read
back from the facts table before it becomes a candidate, so deleting this whole
file costs recall and nothing else.

The sidecar is deliberately separate from the published facts index. It is
written to a temporary file and moved into place, so a failed build leaves the
previous sidecar (and the facts index) exactly as they were.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
from typing import Any, Mapping, Sequence

from .embeddings import (
    EmbeddingUnavailable,
    ProviderIdentity,
    cosine_similarity,
    embed_texts,
    identity_of,
)


SEMANTIC_SCHEMA_VERSION = "semantic-v1"
SEMANTIC_INDEX_FILENAME = "semantic.sqlite"

#: Similarity below this is reported as "possibly related" instead of being
#: presented as an answer. The value is per model version in the end; this is
#: the conservative default the reference provider is evaluated at.
DEFAULT_SIMILARITY_THRESHOLD = 0.35

#: Bounds, so a sidecar can never grow into a second full search.
SEMANTIC_VECTOR_LIMIT = 20000
SEMANTIC_SEARCH_LIMIT = 200

#: The V2 first release vectors text statements only. Raw image embedding is
#: explicitly out of scope; images reach retrieval through their transcription.
SEMANTIC_NAMESPACES: tuple[str, ...] = ("source_facts",)
IMAGE_EMBEDDING_SUPPORTED = False

STATUS_MISSING = "missing"
STATUS_READY = "ready"
STATUS_STALE = "stale"
STATUS_INCOMPATIBLE = "incompatible"
SEMANTIC_INDEX_STATES = (
    STATUS_MISSING,
    STATUS_READY,
    STATUS_STALE,
    STATUS_INCOMPATIBLE,
)

INDEX_MISSING = "semantic_index_missing"
INDEX_STALE = "semantic_index_stale"
INDEX_INCOMPATIBLE = "semantic_index_incompatible"
MODEL_CHANGED = "semantic_model_changed"

SEMANTIC_INDEX_REASONS = (
    INDEX_MISSING,
    INDEX_STALE,
    INDEX_INCOMPATIBLE,
    MODEL_CHANGED,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_vectors (
    unit_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    source_document TEXT NOT NULL,
    document_type TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_vectors_model
    ON semantic_vectors (model_id, model_version, dimension);
CREATE TABLE IF NOT EXISTS semantic_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SemanticIndexError(RuntimeError):
    """A sidecar operation this build refuses instead of guessing."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class SemanticIndex:
    """The vector sidecar at one path. Deleting the file costs no fact."""

    def __init__(
        self,
        path: Path,
        *,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        namespaces: Sequence[str] = SEMANTIC_NAMESPACES,
    ) -> None:
        self.path = Path(path)
        self.threshold = float(threshold)
        self.namespaces = tuple(namespaces)

    # -- lifecycle --------------------------------------------------------

    def exists(self) -> bool:
        return self.path.is_file()

    def meta(self) -> dict[str, str]:
        if not self.exists():
            return {}
        connection = self._connect(read_only=True)
        try:
            try:
                rows = connection.execute(
                    "SELECT key, value FROM semantic_meta"
                ).fetchall()
            except sqlite3.DatabaseError:
                return {}
        finally:
            connection.close()
        return {str(row[0]): str(row[1]) for row in rows}

    def describe(
        self, facts: Any = None, identity: ProviderIdentity | None = None
    ) -> dict[str, Any]:
        """What this sidecar is, and whether it still matches the facts.

        Passing the current provider identity also checks the space the
        vectors live in: a sidecar built by another model or another dimension
        is reported as incompatible instead of being searched as if the
        numbers meant the same thing.
        """

        if not self.exists():
            return {
                "status": STATUS_MISSING,
                "reason": INDEX_MISSING,
                "detail": f"没有向量索引：{self.path}",
                "path": str(self.path),
                "index_version": None,
                "built_at": None,
                "vectors": 0,
                "model_id": None,
                "model_version": None,
                "dimension": None,
                "threshold": self.threshold,
                "namespaces": list(self.namespaces),
                "stale_vectors": 0,
                "image_embedding": IMAGE_EMBEDDING_SUPPORTED,
            }
        try:
            meta = self.meta()
            vectors = self._count()
        except sqlite3.DatabaseError as error:
            return {
                "status": STATUS_INCOMPATIBLE,
                "reason": INDEX_INCOMPATIBLE,
                "detail": f"向量索引无法读取（{error}），本次不参与召回。",
                "path": str(self.path),
                "index_version": None,
                "built_at": None,
                "vectors": 0,
                "model_id": None,
                "model_version": None,
                "dimension": None,
                "threshold": self.threshold,
                "namespaces": list(self.namespaces),
                "stale_vectors": 0,
                "image_embedding": IMAGE_EMBEDDING_SUPPORTED,
            }

        stale = self._stale_units(facts) if facts is not None else 0
        index_version = meta.get("index_version")
        model_changed = bool(
            identity is not None
            and (
                meta.get("model_id") != identity.model_id
                or meta.get("model_version") != identity.model_version
                or _optional_int(meta.get("dimension")) != identity.dimension
            )
        )
        if index_version != SEMANTIC_SCHEMA_VERSION:
            status, reason, detail = (
                STATUS_INCOMPATIBLE,
                INDEX_INCOMPATIBLE,
                f"向量索引版本是 {index_version!r}，本构建需要 "
                f"{SEMANTIC_SCHEMA_VERSION!r}：不混搜，建议重建。",
            )
        elif model_changed:
            status, reason, detail = (
                STATUS_INCOMPATIBLE,
                MODEL_CHANGED,
                "向量索引由另一个模型或另一维度生成：不同空间的向量不混搜，"
                "建议用当前模型重建。",
            )
        elif stale:
            status, reason, detail = (
                STATUS_STALE,
                INDEX_STALE,
                f"{stale} 条向量对应的来源已变化或已不存在：不参与召回，建议重建。",
            )
        else:
            status, reason, detail = (
                STATUS_READY,
                "",
                f"{vectors} 条向量与当前事实库一致。",
            )
        return {
            "status": status,
            "reason": reason,
            "detail": detail,
            "path": str(self.path),
            "index_version": index_version,
            "built_at": meta.get("built_at"),
            "vectors": vectors,
            "model_id": meta.get("model_id"),
            "model_version": meta.get("model_version"),
            "dimension": _optional_int(meta.get("dimension")),
            "threshold": self.threshold,
            "namespaces": list(self.namespaces),
            "stale_vectors": stale,
            "image_embedding": IMAGE_EMBEDDING_SUPPORTED,
        }

    def build(
        self, facts: Any, provider: Any, *, batch_size: int = 32
    ) -> dict[str, Any]:
        """Embed every statement unit and publish the sidecar atomically."""

        identity = identity_of(provider)
        rows = facts.fetchall(
            """
            SELECT e.id AS evidence_id, e.evidence_type, e.text, e.section_path,
                   d.path AS source_document, d.document_type,
                   d.source_sha256 AS source_sha256
            FROM evidence AS e
            JOIN documents AS d ON d.id = e.document_id
            ORDER BY e.id
            LIMIT ?
            """,
            (SEMANTIC_VECTOR_LIMIT,),
        )
        units = [row for row in rows if str(row["text"] or "").strip()]
        inputs = [_embedding_input(row) for row in units]
        try:
            vectors = embed_texts(provider, inputs, batch_size=batch_size)
        except EmbeddingUnavailable:
            raise

        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_name(f"{self.path.name}.building")
        created_at = _now()
        try:
            if staging.exists():
                staging.unlink()
            connection = sqlite3.connect(staging)
            try:
                connection.executescript(_SCHEMA)
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO semantic_vectors (
                        unit_id, namespace, unit_type, source_document,
                        document_type, source_sha256, input_sha256, model_id,
                        model_version, dimension, vector, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            f"evidence:{int(row['evidence_id'])}",
                            self.namespaces[0],
                            str(row["evidence_type"]),
                            str(row["source_document"]),
                            str(row["document_type"]),
                            str(row["source_sha256"]),
                            _sha256(text),
                            identity.model_id,
                            identity.model_version,
                            identity.dimension,
                            _pack(vector),
                            created_at,
                        )
                        for row, text, vector in zip(units, inputs, vectors)
                    ],
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO semantic_meta (key, value) VALUES (?, ?)",
                    [
                        ("index_version", SEMANTIC_SCHEMA_VERSION),
                        ("model_id", identity.model_id),
                        ("model_version", identity.model_version),
                        ("dimension", str(identity.dimension)),
                        ("threshold", str(self.threshold)),
                        ("built_at", created_at),
                        ("namespaces", ",".join(self.namespaces)),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            os.replace(staging, self.path)
        except BaseException:
            if staging.exists():
                staging.unlink()
            raise
        return {
            "status": "built",
            "index_version": SEMANTIC_SCHEMA_VERSION,
            "built_at": created_at,
            "vectors": len(units),
            "skipped": len(rows) - len(units),
            "model": identity.as_payload(),
            "threshold": self.threshold,
            "namespaces": list(self.namespaces),
            "path": str(self.path),
            "considered": len(rows),
            "bound": SEMANTIC_VECTOR_LIMIT,
        }

    def drop(self) -> dict[str, Any]:
        """Delete the sidecar. Facts, FTS5 and every core tool keep working."""

        staging = self.path.with_name(f"{self.path.name}.building")
        removed = []
        for candidate in (self.path, staging):
            if candidate.exists():
                candidate.unlink()
                removed.append(str(candidate))
        return {
            "status": "dropped",
            "removed": removed,
            "path": str(self.path),
            "facts_unchanged": True,
        }

    # -- search -----------------------------------------------------------

    def search(
        self,
        facts: Any,
        provider: Any,
        query: str,
        *,
        limit: int = SEMANTIC_SEARCH_LIMIT,
        document_type: str | None = None,
        evidence_type: str | None = None,
    ) -> dict[str, Any]:
        """Rank stored vectors against one query vector.

        The query is embedded on demand; the stored vectors are never re-read
        as text, and a hit is only a pointer back to a retrieval unit.
        """

        if not self.exists():
            return _search_result(
                "missing",
                INDEX_MISSING,
                f"没有向量索引：{self.path}",
            )
        identity = identity_of(provider)
        meta = self.meta()
        if (
            meta.get("model_id") != identity.model_id
            or meta.get("model_version") != identity.model_version
            or _optional_int(meta.get("dimension")) != identity.dimension
        ):
            return _search_result(
                "incompatible",
                MODEL_CHANGED,
                "向量索引由另一个模型或另一维度生成：不同空间的向量不混搜，"
                "建议用当前模型重建。",
            )
        if meta.get("index_version") != SEMANTIC_SCHEMA_VERSION:
            return _search_result(
                "incompatible",
                INDEX_INCOMPATIBLE,
                f"向量索引版本是 {meta.get('index_version')!r}，本构建需要 "
                f"{SEMANTIC_SCHEMA_VERSION!r}。",
            )
        stale = self._stale_units(facts)
        if stale:
            return _search_result(
                "stale",
                INDEX_STALE,
                f"{stale} 条向量对应的来源已变化或已不存在：本次不参与召回，"
                "建议重建。",
            )

        query_vector = embed_texts(provider, [str(query)])[0]
        rows = self._vectors(document_type=document_type, evidence_type=evidence_type)
        scored: list[dict[str, Any]] = []
        for row in rows:
            stored = _unpack(row["vector"], identity.dimension)
            if stored is None:
                continue
            score = cosine_similarity(query_vector, stored)
            if score <= 0:
                continue
            scored.append(
                {
                    "unit_id": str(row["unit_id"]),
                    "namespace": str(row["namespace"]),
                    "unit_type": str(row["unit_type"]),
                    "source_document": str(row["source_document"]),
                    "document_type": str(row["document_type"]),
                    "source_sha256": str(row["source_sha256"]),
                    "score": round(float(score), 6),
                }
            )
        scored.sort(key=lambda item: (-item["score"], item["unit_id"]))
        hits = scored[: max(1, int(limit))]
        return {
            "status": "matched" if hits else "no_match",
            "reason": "",
            "detail": (
                f"本地向量召回 {len(hits)} 条候选，"
                f"相似度门槛 {self.threshold}。"
            ),
            "hits": hits,
            "scanned": len(rows),
            "threshold": self.threshold,
            "model": identity.as_payload(),
            "index_version": SEMANTIC_SCHEMA_VERSION,
        }

    # -- internals --------------------------------------------------------

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _count(self) -> int:
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM semantic_vectors"
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def _vectors(
        self, *, document_type: str | None, evidence_type: str | None
    ) -> list[sqlite3.Row]:
        connection = self._connect(read_only=True)
        try:
            return connection.execute(
                """
                SELECT unit_id, namespace, unit_type, source_document,
                       document_type, source_sha256, vector
                FROM semantic_vectors
                WHERE (? IS NULL OR document_type = ?)
                  AND (? IS NULL OR unit_type = ?)
                ORDER BY unit_id
                LIMIT ?
                """,
                (
                    document_type,
                    document_type,
                    evidence_type,
                    evidence_type,
                    SEMANTIC_SEARCH_LIMIT,
                ),
            ).fetchall()
        finally:
            connection.close()

    def _stale_units(self, facts: Any) -> int:
        """Vectors whose source moved on, or whose unit is gone.

        A stale sidecar is reported and skipped rather than trusted: the
        caller rebuilds it, or keeps answering from the deterministic channels.
        """

        try:
            rows = facts.fetchall(
                """
                SELECT e.id AS evidence_id, d.source_sha256 AS source_sha256
                FROM evidence AS e
                JOIN documents AS d ON d.id = e.document_id
                """
            )
        except sqlite3.DatabaseError:
            return 0
        current = {
            f"evidence:{int(row['evidence_id'])}": str(row["source_sha256"])
            for row in rows
        }
        connection = self._connect(read_only=True)
        try:
            stored = connection.execute(
                "SELECT unit_id, source_sha256 FROM semantic_vectors"
            ).fetchall()
        except sqlite3.DatabaseError:
            return 0
        finally:
            connection.close()
        return sum(
            1
            for row in stored
            if current.get(str(row["unit_id"])) != str(row["source_sha256"])
        )


class SemanticChannel:
    """What retrieval talks to: availability plus search, both read-only.

    The channel owns the provider and the sidecar so a caller only has to hand
    retrieval one object. Nothing here decides whether an answer is allowed --
    availability is a report, and a hit is still a pointer that has to be read
    back from the facts before it is returned.
    """

    def __init__(
        self,
        provider: Any,
        index: SemanticIndex,
    ) -> None:
        self.provider = provider
        self.index = index
        self.identity = identity_of(provider)

    def availability(self, facts: Any) -> dict[str, Any]:
        described = self.index.describe(facts, identity=self.identity)
        return {
            "available": described["status"] == STATUS_READY,
            "status": described["status"],
            "reason": described["reason"],
            "detail": described["detail"],
            "identity": self.identity.as_payload(),
            "index_version": described["index_version"],
            "built_at": described["built_at"],
            "vectors": described["vectors"],
            "threshold": described["threshold"],
            "stale_vectors": described["stale_vectors"],
            "namespaces": described["namespaces"],
            "image_embedding": described["image_embedding"],
            "path": described["path"],
            # A sidecar that is absent, stale or unreadable is rebuilt, not
            # trusted; a provider that failed is a different matter.
            "rebuild_recommended": described["status"]
            in {STATUS_MISSING, STATUS_STALE, STATUS_INCOMPATIBLE},
        }

    def search(
        self,
        facts: Any,
        *,
        query: str,
        limit: int = SEMANTIC_SEARCH_LIMIT,
        document_type: str | None = None,
        evidence_type: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.index.search(
                facts,
                self.provider,
                query,
                limit=limit,
                document_type=document_type,
                evidence_type=evidence_type,
            )
        except EmbeddingUnavailable as error:
            return _search_result("unavailable", error.reason, error.detail)


def _embedding_input(row: Mapping[str, Any]) -> str:
    """The exact bytes a document vector is computed from.

    The section path is part of the input on purpose -- it is how a question
    phrased as a heading finds the sentence underneath it -- while the returned
    candidate still points at the unit and its own locator.
    """

    sections = str(row["section_path"] or "")
    text = str(row["text"] or "")
    return f"{sections}\n{text}".strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack(vector: Sequence[float]) -> bytes:
    values = [float(value) for value in vector]
    return struct.pack(f"<{len(values)}f", *values)


def _unpack(blob: bytes, dimension: int) -> list[float] | None:
    if len(blob) != 4 * dimension:
        return None
    return list(struct.unpack(f"<{dimension}f", blob))


def _optional_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _search_result(status: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "detail": detail,
        "hits": [],
        "scanned": 0,
        "threshold": None,
        "model": None,
        "index_version": None,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "IMAGE_EMBEDDING_SUPPORTED",
    "INDEX_INCOMPATIBLE",
    "INDEX_MISSING",
    "INDEX_STALE",
    "MODEL_CHANGED",
    "SEMANTIC_INDEX_FILENAME",
    "SEMANTIC_INDEX_REASONS",
    "SEMANTIC_INDEX_STATES",
    "SEMANTIC_NAMESPACES",
    "SEMANTIC_SCHEMA_VERSION",
    "SEMANTIC_SEARCH_LIMIT",
    "SEMANTIC_VECTOR_LIMIT",
    "STATUS_INCOMPATIBLE",
    "STATUS_MISSING",
    "STATUS_READY",
    "STATUS_STALE",
    "SemanticChannel",
    "SemanticIndex",
    "SemanticIndexError",
]
