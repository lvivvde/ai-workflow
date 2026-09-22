"""Layer-specific metrics.

Each metric belongs to exactly one evaluation layer. The protocol forbids
blending them into a single accuracy number, so nothing here aggregates across
layers.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence


def _edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    if not reference:
        return len(hypothesis)
    previous = list(range(len(hypothesis) + 1))
    for row, reference_item in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (reference_item != hypothesis_item),
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over characters, normalized by reference length."""

    if not reference:
        return 0.0 if not hypothesis else 1.0
    return _edit_distance(list(reference), list(hypothesis)) / len(reference)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Token error rate; Chinese text is scored per character when whitespace-free."""

    reference_units = reference.split() or list(reference)
    hypothesis_units = hypothesis.split() or list(hypothesis)
    if not reference_units:
        return 0.0 if not hypothesis_units else 1.0
    return _edit_distance(reference_units, hypothesis_units) / len(reference_units)


def critical_token_accuracy(
    expected_tokens: Iterable[str], observed_tokens: Iterable[str]
) -> float | None:
    """Accuracy over digits, units, IDs, operators, arrows, and negations."""

    expected = list(expected_tokens)
    if not expected:
        return None
    observed = list(observed_tokens)
    hits = sum(1 for token in expected if token in observed)
    return hits / len(expected)


def recall_at_k(ranked: Sequence[str], expected: Iterable[str], k: int) -> float | None:
    expected_set = set(expected)
    if not expected_set:
        return None
    return len(expected_set & set(list(ranked)[:k])) / len(expected_set)


def precision_at_k(ranked: Sequence[str], expected: Iterable[str], k: int) -> float | None:
    window = list(ranked)[:k]
    if not window:
        return None
    expected_set = set(expected)
    return sum(1 for item in window if item in expected_set) / len(window)


def reciprocal_rank(ranked: Sequence[str], expected: Iterable[str]) -> float:
    expected_set = set(expected)
    for position, item in enumerate(ranked, start=1):
        if item in expected_set:
            return 1 / position
    return 0.0


def response_state_confusion(
    pairs: Iterable[tuple[str, str]]
) -> dict[str, dict[str, int]]:
    """Confusion matrix keyed by annotated state, then observed state."""

    matrix: dict[str, dict[str, int]] = {}
    for expected, observed in pairs:
        row = matrix.setdefault(expected, {})
        row[observed] = row.get(observed, 0) + 1
    return matrix


def state_precision_recall(
    matrix: Mapping[str, Mapping[str, int]]
) -> dict[str, dict[str, float | int | None]]:
    labels = sorted(
        set(matrix) | {observed for row in matrix.values() for observed in row}
    )
    totals = {label: 0 for label in labels}
    for row in matrix.values():
        for label, count in row.items():
            totals[label] += count

    summary: dict[str, dict[str, float | int | None]] = {}
    for label in labels:
        true_positive = matrix.get(label, {}).get(label, 0)
        expected_total = sum(matrix.get(label, {}).values())
        observed_total = totals[label]
        summary[label] = {
            "expected": expected_total,
            "observed": observed_total,
            "true_positive": true_positive,
            "precision": (true_positive / observed_total) if observed_total else None,
            "recall": (true_positive / expected_total) if expected_total else None,
        }
    return summary


def mean_ignoring_none(values: Iterable[float | None]) -> float | None:
    collected = [value for value in values if value is not None]
    if not collected:
        return None
    return sum(collected) / len(collected)
