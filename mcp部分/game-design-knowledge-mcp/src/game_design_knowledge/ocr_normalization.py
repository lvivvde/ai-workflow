"""Normalized transcription *suggestions* and Critical Transcription Tokens.

Nothing here rewrites a transcription. ``normalize_transcription`` returns the
raw text untouched plus an ordered, replayable edit script, so a reader can
always answer "what did the engine actually see?" and "what was proposed
instead?" separately (spec #11 item 5.1).

The second half extracts the Critical Transcription Tokens a numeric game
design document lives on -- numbers, percentages, identifiers, operators,
arrows, and negations -- and scores them per kind against a reference. The
scoring is deliberately a tuple of per-kind results: a single blended number
would hide "all the numbers were right but the negation was dropped", which is
the one error that flips the meaning of a rule.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable, Sequence


NORMALIZATION_RULESET_VERSION = "transcription-normalization-v1"

CRITICAL_TOKEN_KINDS = (
    "number",
    "percent",
    "identifier",
    "operator",
    "arrow",
    "negation",
)


class NormalizationError(ValueError):
    """Raised when an edit script does not replay onto its own raw text."""


@dataclass(frozen=True)
class SpanChange:
    """One proposed edit: ``before`` at ``[start, end)`` becomes ``after``."""

    rule: str
    start: int
    end: int
    before: str
    after: str
    reason: str
    confidence: float
    evidence: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "start": self.start,
            "end": self.end,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "rule_confidence": self.confidence,
            "evidence": self.evidence,
        }


def apply_changes(raw: str, changes: Iterable[SpanChange]) -> str:
    """Replay an edit script onto raw text, validating every span.

    The script is the audit trail, so a span that does not match the text it
    claims to describe is an error rather than something to paper over.
    """

    text = raw
    for change in changes:
        if text[change.start : change.end] != change.before:
            raise NormalizationError(
                f"edit {change.rule}@{change.start}:{change.end} expects "
                f"{change.before!r} but the text holds "
                f"{text[change.start : change.end]!r}"
            )
        text = text[: change.start] + change.after + text[change.end :]
    return text


class _EditScript:
    """Accumulates edits against the text each rule actually saw."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.text = raw
        self.changes: list[SpanChange] = []

    def plan(
        self,
        rule: str,
        replacements: Sequence[tuple[int, int, str]],
        *,
        reason: str,
        confidence: float,
        evidence: str,
    ) -> None:
        group: list[SpanChange] = []
        for start, end, after in sorted(replacements, key=lambda item: item[0], reverse=True):
            before = self.text[start:end]
            if before == after:
                continue
            group.append(
                SpanChange(
                    rule=rule,
                    start=start,
                    end=end,
                    before=before,
                    after=after,
                    reason=reason,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        if not group:
            return
        # Apply right-to-left so every recorded span still points at the text it
        # was computed against, then store them left-to-right for replay.
        for change in group:
            self.text = (
                self.text[: change.start] + change.after + self.text[change.end :]
            )
        self.changes.extend(sorted(group, key=lambda change: change.start))


_FULLWIDTH = re.compile(r"[\uff01-\uff5e\u3000]+")
_PERCENT_SPACING = re.compile(r"(\d)\s*([%\uff05])")
_SPACE_BETWEEN_CJK = re.compile(
    r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
    r"[ \t\u3000]+"
    r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
)
_SPACE_BEFORE_CJK_PUNCTUATION = re.compile(
    r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
    r"[ \t\u3000]+"
    r"([，。、；：！？）」』》”’%])"
)
_WHITESPACE_RUN = re.compile(r"[ \t\u3000]+")
_LINE_EDGE_SPACE = re.compile(r"(?m)^[ \t\u3000]+|[ \t\u3000]+$")


def _to_ascii(match: re.Match[str]) -> str:
    return "".join(
        " " if character == "\u3000" else chr(ord(character) - 0xFEE0)
        for character in match.group(0)
    )


def _rule_fullwidth_ascii(script: _EditScript) -> None:
    script.plan(
        "fullwidth_ascii",
        [(match.start(), match.end(), _to_ascii(match)) for match in _FULLWIDTH.finditer(script.text)],
        reason="OCR emitted full-width ASCII forms of digits, letters, or punctuation",
        confidence=0.95,
        evidence="the replacement is the same codepoint in the ASCII block",
    )


def _rule_percent_spacing(script: _EditScript) -> None:
    script.plan(
        "percent_spacing",
        [
            (match.start(), match.end(), f"{match.group(1)}%")
            for match in _PERCENT_SPACING.finditer(script.text)
        ],
        reason="a percentage was split by whitespace",
        confidence=0.9,
        evidence="the digit and the percent sign are adjacent in the source image",
    )


def _rule_cjk_spacing(script: _EditScript) -> None:
    replacements = [
        (match.start(), match.end(), f"{match.group(1)}{match.group(2)}")
        for match in _SPACE_BETWEEN_CJK.finditer(script.text)
    ]
    replacements.extend(
        (match.start(), match.end(), f"{match.group(1)}{match.group(2)}")
        for match in _SPACE_BEFORE_CJK_PUNCTUATION.finditer(script.text)
    )
    script.plan(
        "cjk_spacing",
        replacements,
        reason="OCR inserted a space inside a Chinese word or before its punctuation",
        confidence=0.8,
        evidence="CJK text is not word-separated by spaces",
    )


def _rule_whitespace_collapse(script: _EditScript) -> None:
    script.plan(
        "whitespace_collapse",
        [
            (match.start(), match.end(), " ")
            for match in _WHITESPACE_RUN.finditer(script.text)
        ],
        reason="runs of whitespace inside a region",
        confidence=0.9,
        evidence="the region is one line of transcription",
    )


def _rule_line_edge_trim(script: _EditScript) -> None:
    script.plan(
        "line_edge_trim",
        [(match.start(), match.end(), "") for match in _LINE_EDGE_SPACE.finditer(script.text)],
        reason="leading or trailing whitespace on a transcribed line",
        confidence=0.9,
        evidence="the region boundary is the line boundary",
    )


RULES: tuple[tuple[str, Any], ...] = (
    ("fullwidth_ascii", _rule_fullwidth_ascii),
    ("percent_spacing", _rule_percent_spacing),
    ("cjk_spacing", _rule_cjk_spacing),
    ("whitespace_collapse", _rule_whitespace_collapse),
    ("line_edge_trim", _rule_line_edge_trim),
)
RULE_NAMES = tuple(name for name, _ in RULES)


@dataclass(frozen=True)
class NormalizationSuggestion:
    """A proposed reading of the raw transcription, with the edits that made it."""

    raw: str
    normalized: str
    changes: tuple[SpanChange, ...] = ()
    ruleset_version: str = NORMALIZATION_RULESET_VERSION

    @property
    def changed(self) -> bool:
        return self.normalized != self.raw

    def as_payload(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "normalized": self.normalized,
            "changed": self.changed,
            "ruleset_version": self.ruleset_version,
            "suggestion_only": True,
            "changes": [change.as_payload() for change in self.changes],
        }


def normalize_transcription(
    raw: str, *, rules: Sequence[str] | None = None
) -> NormalizationSuggestion:
    """Propose a normalized reading without touching the raw transcription."""

    script = _EditScript(raw)
    selected = set(rules) if rules is not None else None
    for name, rule in RULES:
        if selected is not None and name not in selected:
            continue
        rule(script)
    suggestion = NormalizationSuggestion(
        raw=raw,
        normalized=script.text,
        changes=tuple(script.changes),
    )
    if apply_changes(suggestion.raw, suggestion.changes) != suggestion.normalized:
        raise NormalizationError(
            "the edit script does not reproduce the normalized transcription"
        )
    return suggestion


# -- Critical Transcription Tokens ----------------------------------------


_ARROW_ALIASES = {
    "->": "→",
    "-->": "→",
    "<-": "←",
    "<--": "←",
    "=>": "⇒",
    "==>": "⇒",
}

_ARROWS = re.compile(r"-->|<--|->|<-|==>|=>|→|←|↑|↓|↔|⇒|⇐|⇔|↦")
_PERCENTS = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*[%\uff05]")
_IDENTIFIERS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[-_.][A-Za-z0-9_]+)*")
_NUMBERS = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_OPERATORS = re.compile(r"[+\-*/=<>±≠≈≤≥×÷^]+")
_NEGATIONS = re.compile(
    r"禁止|不得|不能|不可|永不|排除|取消|不存在|不|无|未|非|没|否|勿"
    r"|(?<![A-Za-z])(?:no|not|never|none|without|false)(?![A-Za-z])",
    re.IGNORECASE,
)

_MATCHERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("arrow", _ARROWS),
    ("percent", _PERCENTS),
    ("identifier", _IDENTIFIERS),
    ("number", _NUMBERS),
    ("operator", _OPERATORS),
    ("negation", _NEGATIONS),
)


@dataclass(frozen=True)
class CriticalToken:
    """One token whose misreading changes what a rule means."""

    kind: str
    text: str
    start: int
    end: int

    def canonical(self) -> str:
        return canonical_token(self.text)

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "canonical": self.canonical(),
        }


def canonical_token(text: str) -> str:
    """Compare tokens in one spelling: NFKC, no spaces, arrows folded."""

    folded = unicodedata.normalize("NFKC", text).strip()
    folded = re.sub(r"\s+", "", folded)
    return _ARROW_ALIASES.get(folded, folded)


def _is_identifier(text: str) -> bool:
    if "_" in text or any(character.isdigit() for character in text):
        return True
    return bool(re.search(r"[A-Z]{2,}", text))


def extract_critical_tokens(text: str) -> tuple[CriticalToken, ...]:
    """Every critical token in ``text``, in reading order, without overlaps.

    Patterns are tried in the order above and the first claimant keeps the
    span, which is what stops ``-3.5`` from also reporting a stray operator and
    ``ID_12`` from splitting into ``ID_`` and ``12``.
    """

    claimed: list[tuple[int, int]] = []
    tokens: list[CriticalToken] = []
    for kind, pattern in _MATCHERS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if start == end:
                continue
            if any(start < other_end and end > other_start for other_start, other_end in claimed):
                continue
            value = match.group(0)
            if kind == "identifier" and not _is_identifier(value):
                continue
            claimed.append((start, end))
            tokens.append(CriticalToken(kind=kind, text=value, start=start, end=end))
    return tuple(sorted(tokens, key=lambda token: token.start))


@dataclass(frozen=True)
class CriticalTokenScore:
    """One token kind's accuracy. Never merged with the other kinds."""

    kind: str
    reference: int
    matched: int
    transcribed: int
    missed: tuple[str, ...]
    spurious: tuple[str, ...]

    @property
    def accuracy(self) -> float | None:
        """Recall for this kind; ``None`` when the reference has no such token."""

        if self.reference == 0:
            return None
        return self.matched / self.reference

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "matched": self.matched,
            "transcribed": self.transcribed,
            "accuracy": self.accuracy,
            "missed": list(self.missed),
            "spurious": list(self.spurious),
        }


def score_critical_tokens(
    transcribed: str, reference: str
) -> tuple[CriticalTokenScore, ...]:
    """Per-kind accuracy for the tokens that carry a rule's meaning.

    Reports recall against the reference, so a dropped ``不`` or ``≤`` shows up
    as a miss on its own kind rather than hiding inside one accuracy number.
    """

    transcribed_counts = Counter(
        (token.kind, token.canonical()) for token in extract_critical_tokens(transcribed)
    )
    reference_counts = Counter(
        (token.kind, token.canonical()) for token in extract_critical_tokens(reference)
    )
    scores: list[CriticalTokenScore] = []
    for kind in CRITICAL_TOKEN_KINDS:
        expected = Counter(
            {
                token: count
                for (token_kind, token), count in reference_counts.items()
                if token_kind == kind
            }
        )
        seen = Counter(
            {
                token: count
                for (token_kind, token), count in transcribed_counts.items()
                if token_kind == kind
            }
        )
        matched = sum((expected & seen).values())
        missed = sorted((expected - seen).elements())
        spurious = sorted((seen - expected).elements())
        scores.append(
            CriticalTokenScore(
                kind=kind,
                reference=sum(expected.values()),
                matched=matched,
                transcribed=sum(seen.values()),
                missed=tuple(missed),
                spurious=tuple(spurious),
            )
        )
    return tuple(scores)


def critical_token_payload(
    transcribed: str, reference: str
) -> dict[str, Any]:
    """The report an evaluation layer serializes; per-kind results only."""

    scores = score_critical_tokens(transcribed, reference)
    return {
        "by_kind": [score.as_payload() for score in scores],
        "kinds": list(CRITICAL_TOKEN_KINDS),
        "aggregate": False,
    }


__all__ = [
    "CRITICAL_TOKEN_KINDS",
    "CriticalToken",
    "CriticalTokenScore",
    "NORMALIZATION_RULESET_VERSION",
    "NormalizationError",
    "NormalizationSuggestion",
    "RULE_NAMES",
    "RULES",
    "SpanChange",
    "apply_changes",
    "canonical_token",
    "critical_token_payload",
    "extract_critical_tokens",
    "normalize_transcription",
    "score_critical_tokens",
]
