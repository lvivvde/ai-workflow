"""Optional local query rewriting: variants discover, they never decide.

A generated variant is a second way to ask the *same* question. It may reword
a phrase; it may not move a number, a unit, a version, a time, a scope word or
a negation, because those are exactly the parts of a question whose change
would turn the answer into an answer to a different question.

Three things follow from that, and all three are visible in the response:
the original query is kept as written, every variant keeps the variant that
produced each hit, and a rejected variant is reported with the reason it was
rejected instead of being silently dropped. Nothing here writes to the
Designer Notation Dictionary: a reworded question is not a confirmed meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
import re
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


QUERY_REWRITER_ENV = "GAME_DESIGN_QUERY_REWRITER"

#: A rewrite this build refuses, and why. Every refused variant is reported.
VARIANT_EMPTY = "empty_variant"
VARIANT_IDENTICAL = "identical_to_original"
VARIANT_TOO_LONG = "variant_too_long"
VARIANT_LIMIT_REACHED = "variant_limit_reached"
VARIANT_CHANGES_VALUE = "changes_value"
VARIANT_CHANGES_UNIT = "changes_unit"
VARIANT_CHANGES_VERSION = "changes_version"
VARIANT_CHANGES_TIME = "changes_time"
VARIANT_CHANGES_NEGATION = "changes_negation"
VARIANT_CHANGES_SCOPE = "changes_scope"

VARIANT_GUARD_REASONS = (
    VARIANT_EMPTY,
    VARIANT_IDENTICAL,
    VARIANT_TOO_LONG,
    VARIANT_LIMIT_REACHED,
    VARIANT_CHANGES_VALUE,
    VARIANT_CHANGES_UNIT,
    VARIANT_CHANGES_VERSION,
    VARIANT_CHANGES_TIME,
    VARIANT_CHANGES_NEGATION,
    VARIANT_CHANGES_SCOPE,
)

REWRITER_NOT_CONFIGURED = "query_rewrite_not_configured"
REWRITER_NOT_IMPORTABLE = "query_rewriter_not_importable"
REWRITER_INVALID = "query_rewriter_invalid"
REWRITER_FAILED = "query_rewriter_failed"

REWRITER_REASONS = (
    REWRITER_NOT_CONFIGURED,
    REWRITER_NOT_IMPORTABLE,
    REWRITER_INVALID,
    REWRITER_FAILED,
)

#: How many variants one query may be searched with, and how long one may be.
MAX_GENERATED_VARIANTS = 4
MAX_VARIANT_CHARACTERS = 200

#: A number together with the unit written next to it. ``30秒`` and ``30分钟``
#: are two different claims even though both contain ``30``.
NUMBER_UNIT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"([%％秒分时天日月年次回合倍级点个张条位米人档层局把份](?![0-9A-Za-z]))?"
)
VERSION_PATTERN = re.compile(
    r"[vV]\s?\d+(?:\.\d+)*|版本\s?\d+(?:\.\d+)*|第\s?\d+\s?版"
)
TIME_PATTERN = re.compile(
    r"\d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日"
)

#: Words that flip or narrow what a question is about. A variant may not add,
#: drop or swap one of these, because the answer would no longer be to the same
#: question.
NEGATION_WORDS = (
    "不",
    "没有",
    "未",
    "禁止",
    "无法",
    "取消",
    "避免",
    "非",
    "not",
    "never",
    "without",
    "cannot",
    "can't",
)
SCOPE_WORDS = (
    "仅",
    "只",
    "全服",
    "全区",
    "所有",
    "全部",
    "默认",
    "仅限",
    "每一",
    "每个",
    "任意",
    "至少",
    "最多",
)


@runtime_checkable
class QueryRewriter(Protocol):
    """A local model that proposes other phrasings of one question."""

    name: str
    version: str

    def variants(self, query: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class VariantDecision:
    """One proposed variant and whether the guard let it through."""

    variant: str
    accepted: bool
    reason: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "accepted": self.accepted,
            "reason": self.reason,
        }


def variant_guard(original: str, variant: str) -> tuple[bool, str]:
    """Whether a variant asks the same question, and why not when it does not."""

    first = str(original or "")
    second = str(variant or "").strip()
    if not second:
        return False, VARIANT_EMPTY
    if second == first.strip():
        return False, VARIANT_IDENTICAL
    if len(second) > MAX_VARIANT_CHARACTERS:
        return False, VARIANT_TOO_LONG
    if _counts(_versions(first)) != _counts(_versions(second)):
        return False, VARIANT_CHANGES_VERSION
    if _counts(_times(first)) != _counts(_times(second)):
        return False, VARIANT_CHANGES_TIME
    if _counts(_values(first)) != _counts(_values(second)):
        return False, VARIANT_CHANGES_VALUE
    if _counts(_units(first)) != _counts(_units(second)):
        return False, VARIANT_CHANGES_UNIT
    if _counts(_words(first, NEGATION_WORDS)) != _counts(
        _words(second, NEGATION_WORDS)
    ):
        return False, VARIANT_CHANGES_NEGATION
    if _counts(_words(first, SCOPE_WORDS)) != _counts(
        _words(second, SCOPE_WORDS)
    ):
        return False, VARIANT_CHANGES_SCOPE
    return True, ""


def generated_variants(
    rewriter: Any, query: str
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """The variants a rewriter may be searched with, plus every refusal.

    The third value is the report for ``response_meta``: what ran, how many
    variants were accepted, and which ones the guard refused and why.
    """

    original = str(query or "")
    try:
        proposed = list(rewriter.variants(original) or ())
    except Exception as error:  # noqa: BLE001 - a local model may fail
        return [], [], {
            "status": "failed",
            "reason": REWRITER_FAILED,
            "detail": f"本地查询改写失败：{error}",
            "name": str(getattr(rewriter, "name", "") or ""),
            "version": str(getattr(rewriter, "version", "") or ""),
            "accepted": 0,
            "rejected": [],
        }

    accepted: list[str] = []
    rejected: list[dict[str, Any]] = []
    for candidate in proposed:
        if len(accepted) >= MAX_GENERATED_VARIANTS:
            rejected.append(
                VariantDecision(
                    variant=str(candidate),
                    accepted=False,
                    reason=VARIANT_LIMIT_REACHED,
                ).as_payload()
            )
            continue
        ok, reason = variant_guard(original, str(candidate))
        if not ok:
            rejected.append(
                VariantDecision(
                    variant=str(candidate), accepted=False, reason=reason
                ).as_payload()
            )
            continue
        accepted.append(str(candidate).strip())
    report = {
        "status": "applied" if accepted else ("refused" if rejected else "no_variants"),
        "reason": "" if accepted else (REWRITER_INVALID if rejected else ""),
        "detail": (
            f"本地改写提出 {len(accepted)} 个可用变体，"
            f"{len(rejected)} 个未通过质量门槛。"
        ),
        "name": str(getattr(rewriter, "name", "") or ""),
        "version": str(getattr(rewriter, "version", "") or ""),
        "accepted": len(accepted),
        "rejected": rejected,
    }
    return accepted, rejected, report


def not_configured_report() -> dict[str, Any]:
    """The report a response carries when nothing offered to rewrite."""

    return {
        "status": "not_configured",
        "reason": REWRITER_NOT_CONFIGURED,
        "detail": (
            f"没有配置本地查询改写（{QUERY_REWRITER_ENV} 未设置），"
            "本次只用原始查询与已确认扩展。"
        ),
        "name": "",
        "version": "",
        "accepted": 0,
        "rejected": [],
    }


def load_rewriter(
    environ: Mapping[str, str] | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    """The local rewriter this environment configured, or why there is none."""

    source = environ if environ is not None else os.environ
    configured = str(source.get(QUERY_REWRITER_ENV, "") or "").strip()
    if not configured:
        return None, not_configured_report()
    module_name, separator, attribute = configured.partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        return None, _rewriter_report(
            REWRITER_INVALID,
            f"本地查询改写必须写成 module:attribute，收到 {configured!r}。",
        )
    try:
        module = importlib.import_module(module_name.strip())
        rewriter = getattr(module, attribute.strip())
    except Exception as error:  # noqa: BLE001 - import errors are the report
        return None, _rewriter_report(
            REWRITER_NOT_IMPORTABLE,
            f"无法加载本地查询改写 {configured!r}：{error}",
        )
    if callable(rewriter) and not hasattr(rewriter, "variants"):
        try:
            rewriter = rewriter()
        except Exception as error:  # noqa: BLE001 - a factory may fail to load
            return None, _rewriter_report(
                REWRITER_FAILED, f"本地查询改写 {configured!r} 加载失败：{error}"
            )
    if not callable(getattr(rewriter, "variants", None)):
        return None, _rewriter_report(
            REWRITER_INVALID,
            f"本地查询改写 {configured!r} 必须实现 variants(query)。",
        )
    return rewriter, _rewriter_report("", "")


def _rewriter_report(reason: str, detail: str) -> dict[str, Any]:
    return {
        "status": "unavailable" if reason else "available",
        "reason": reason,
        "detail": detail,
        "name": "",
        "version": "",
        "accepted": 0,
        "rejected": [],
    }


def _values(text: str) -> list[str]:
    return [match.group(1) for match in NUMBER_UNIT_PATTERN.finditer(text)]


def _units(text: str) -> list[str]:
    return [
        match.group(2) or ""
        for match in NUMBER_UNIT_PATTERN.finditer(text)
    ]


def _versions(text: str) -> list[str]:
    return [match.group(0) for match in VERSION_PATTERN.finditer(text)]


def _times(text: str) -> list[str]:
    return [match.group(0) for match in TIME_PATTERN.finditer(text)]


def _words(text: str, vocabulary: Sequence[str]) -> list[str]:
    lowered = text.lower()
    return [word for word in vocabulary if word in lowered]


def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


__all__ = [
    "MAX_GENERATED_VARIANTS",
    "MAX_VARIANT_CHARACTERS",
    "NEGATION_WORDS",
    "QUERY_REWRITER_ENV",
    "QueryRewriter",
    "REWRITER_FAILED",
    "REWRITER_INVALID",
    "REWRITER_NOT_CONFIGURED",
    "REWRITER_NOT_IMPORTABLE",
    "REWRITER_REASONS",
    "SCOPE_WORDS",
    "VARIANT_GUARD_REASONS",
    "VariantDecision",
    "generated_variants",
    "load_rewriter",
    "not_configured_report",
    "variant_guard",
]
