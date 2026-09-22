"""Optional local text embeddings, behind an explicit switch.

The vector channel is a declared capability of the V2 retrieval contract, not
a dependency of it. This module is the seam: a provider is anything that can
name its own model identity and turn text into fixed-length vectors. Nothing in
the deterministic path imports a provider, and a build without one answers
exactly as it did before this module existed.

Two rules shape everything here. The provider is **local**: it is either a
model directory the caller installed, or an object the caller imported, and
this code never downloads anything. And the identity is **comparable**: model
id, model version and dimension travel with every vector, so vectors written by
one model are never searched with another.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import math
import os
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


EMBEDDING_PROVIDER_ENV = "GAME_DESIGN_EMBEDDING_PROVIDER"

#: Why a provider could not be used. Every one of these is reported instead of
#: being turned into an empty result: an answer without a vector channel must
#: say that the channel was missing, not that nothing was found.
PROVIDER_NOT_CONFIGURED = "provider_not_configured"
PROVIDER_NOT_IMPORTABLE = "provider_not_importable"
PROVIDER_INVALID = "provider_invalid"
PROVIDER_FAILED = "provider_failed"
DIMENSION_MISMATCH = "dimension_mismatch"

EMBEDDING_REASONS = (
    PROVIDER_NOT_CONFIGURED,
    PROVIDER_NOT_IMPORTABLE,
    PROVIDER_INVALID,
    PROVIDER_FAILED,
    DIMENSION_MISMATCH,
)

#: The reference provider's tokeniser: ASCII words and CJK characters, with CJK
#: bigrams so two nearby phrasings of one idea still share something.
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")

DEFAULT_HASHING_DIMENSION = 256


class EmbeddingUnavailable(RuntimeError):
    """A local embedding provider this build cannot use, and why."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ProviderIdentity:
    """What a vector record is bound to, so two models never mix."""

    model_id: str
    model_version: str
    dimension: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "dimension": self.dimension,
        }


@runtime_checkable
class EmbeddingProvider(Protocol):
    """A local text embedding model.

    ``model_id`` and ``model_version`` identify the model; ``dimension`` is the
    length of every vector it returns. ``embed`` receives a batch and returns
    one vector per input, in order.
    """

    model_id: str
    model_version: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    """The reference tokeniser: lowercase ASCII words plus CJK uni/bi-grams."""

    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(str(text or "")):
        chunk = match.group(0)
        if chunk[0].isascii():
            for part in chunk.split("_"):
                if part:
                    tokens.append(part.lower())
            continue
        tokens.extend(chunk)
        tokens.extend(
            chunk[index : index + 2] for index in range(len(chunk) - 1)
        )
    return tokens


class HashingTextEmbedding:
    """A deterministic, dependency-free local provider.

    This is the reference implementation the contract can be tested against: it
    needs no model files, no network and no numeric library, and the same text
    always produces the same vector. It is deliberately *not* a trained model,
    so it is reported as such and only ever ran when a caller switches it on.
    """

    model_id = "local-hashing-text"
    model_version = "1"
    kind = "reference"

    def __init__(self, dimension: int = DEFAULT_HASHING_DIMENSION) -> None:
        if dimension < 8:
            raise EmbeddingUnavailable(
                PROVIDER_INVALID,
                f"参考 embedding 的维度必须不小于 8，收到 {dimension!r}。",
            )
        self.dimension = int(dimension)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(str(text)) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            return vector
        return [value / norm for value in vector]


def identity_of(provider: Any) -> ProviderIdentity:
    """Read a provider's identity, or say exactly what is wrong with it."""

    model_id = str(getattr(provider, "model_id", "") or "").strip()
    model_version = str(getattr(provider, "model_version", "") or "").strip()
    dimension = getattr(provider, "dimension", None)
    if not model_id or not model_version:
        raise EmbeddingUnavailable(
            PROVIDER_INVALID,
            "本地 embedding provider 必须声明 model_id 与 model_version。",
        )
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise EmbeddingUnavailable(
            PROVIDER_INVALID,
            f"本地 embedding provider 的 dimension 必须是正整数，收到 {dimension!r}。",
        )
    if not callable(getattr(provider, "embed", None)):
        raise EmbeddingUnavailable(
            PROVIDER_INVALID,
            "本地 embedding provider 必须实现 embed(texts) -> list[list[float]]。",
        )
    return ProviderIdentity(
        model_id=model_id, model_version=model_version, dimension=dimension
    )


def embed_texts(
    provider: Any, texts: Sequence[str], *, batch_size: int = 32
) -> list[list[float]]:
    """Embed every text, checking the shape the identity promised."""

    identity = identity_of(provider)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), max(1, batch_size)):
        batch = list(texts[start : start + max(1, batch_size)])
        try:
            produced = provider.embed(batch)
        except Exception as error:  # noqa: BLE001 - the model's own failure mode
            raise EmbeddingUnavailable(
                PROVIDER_FAILED, f"本地 embedding 计算失败：{error}"
            ) from error
        rows = [list(vector) for vector in produced]
        if len(rows) != len(batch):
            raise EmbeddingUnavailable(
                PROVIDER_FAILED,
                f"本地 embedding 返回了 {len(rows)} 条向量，输入是 {len(batch)} 条。",
            )
        for row in rows:
            if len(row) != identity.dimension:
                raise EmbeddingUnavailable(
                    DIMENSION_MISMATCH,
                    f"本地 embedding 返回 {len(row)} 维向量，声明的是 "
                    f"{identity.dimension} 维。",
                )
            vectors.append([float(value) for value in row])
    return vectors


def load_provider(environ: Mapping[str, str] | None = None) -> EmbeddingProvider:
    """The local provider this environment configured, or why there is none.

    The switch is explicit: nothing is loaded, imported or downloaded until
    ``GAME_DESIGN_EMBEDDING_PROVIDER`` names a provider. ``hashing`` (with an
    optional ``hashing:dimension``) selects the bundled reference provider; any
    other value is an import path of the form ``module:attribute``.
    """

    source = environ if environ is not None else os.environ
    configured = str(source.get(EMBEDDING_PROVIDER_ENV, "") or "").strip()
    if not configured:
        raise EmbeddingUnavailable(
            PROVIDER_NOT_CONFIGURED,
            f"没有配置本地 embedding provider（{EMBEDDING_PROVIDER_ENV} 未设置），"
            "本次没有向量召回；确定性通道不受影响。",
        )
    if configured == "hashing" or configured.startswith("hashing:"):
        _, _, raw_dimension = configured.partition(":")
        raw_dimension = raw_dimension.strip()
        dimension = DEFAULT_HASHING_DIMENSION
        if raw_dimension:
            try:
                dimension = int(raw_dimension)
            except ValueError as error:
                raise EmbeddingUnavailable(
                    PROVIDER_INVALID,
                    f"参考 embedding 的维度必须是整数，收到 {raw_dimension!r}。",
                ) from error
        provider = HashingTextEmbedding(dimension)
        identity_of(provider)
        return provider
    return _imported_provider(configured)


def _imported_provider(configured: str) -> EmbeddingProvider:
    module_name, separator, attribute = configured.partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        raise EmbeddingUnavailable(
            PROVIDER_INVALID,
            f"本地 embedding provider 必须写成 module:attribute，收到 {configured!r}。",
        )
    try:
        module = importlib.import_module(module_name.strip())
    except Exception as error:  # noqa: BLE001 - import errors are the report
        raise EmbeddingUnavailable(
            PROVIDER_NOT_IMPORTABLE,
            f"无法导入本地 embedding provider {module_name!r}：{error}",
        ) from error
    try:
        provider = getattr(module, attribute.strip())
    except AttributeError as error:
        raise EmbeddingUnavailable(
            PROVIDER_NOT_IMPORTABLE,
            f"{module_name!r} 没有属性 {attribute.strip()!r}。",
        ) from error
    if callable(provider) and not _has_identity(provider):
        try:
            provider = provider()
        except Exception as error:  # noqa: BLE001 - a factory may fail to load
            raise EmbeddingUnavailable(
                PROVIDER_FAILED,
                f"本地 embedding provider {configured!r} 加载失败：{error}",
            ) from error
    identity_of(provider)
    return provider


def _has_identity(provider: Any) -> bool:
    """Whether this object already is a provider, rather than one to build."""

    if callable(getattr(provider, "embed", None)) is False:
        return False
    dimension = getattr(provider, "dimension", None)
    return (
        bool(str(getattr(provider, "model_id", "") or ""))
        and bool(str(getattr(provider, "model_version", "") or ""))
        and isinstance(dimension, int)
        and not isinstance(dimension, bool)
    )


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, computed only when the two lengths agree."""

    if len(left) != len(right):
        raise EmbeddingUnavailable(
            DIMENSION_MISMATCH,
            f"向量维度不一致：{len(left)} 与 {len(right)} 维不能在同一空间比较。",
        )
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(
        float(a) * float(b) for a, b in zip(left, right)
    ) / (left_norm * right_norm)


__all__ = [
    "DEFAULT_HASHING_DIMENSION",
    "DIMENSION_MISMATCH",
    "EMBEDDING_PROVIDER_ENV",
    "EMBEDDING_REASONS",
    "EmbeddingProvider",
    "EmbeddingUnavailable",
    "HashingTextEmbedding",
    "PROVIDER_FAILED",
    "PROVIDER_INVALID",
    "PROVIDER_NOT_CONFIGURED",
    "PROVIDER_NOT_IMPORTABLE",
    "ProviderIdentity",
    "cosine_similarity",
    "embed_texts",
    "identity_of",
    "load_provider",
    "tokenize",
]
