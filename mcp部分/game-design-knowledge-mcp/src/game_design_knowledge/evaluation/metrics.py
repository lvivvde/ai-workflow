"""Layer-specific metrics.

Each metric belongs to exactly one evaluation layer. The protocol forbids
blending them into a single accuracy number, so nothing here aggregates across
layers.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence


#: Two-sided z for a 95% interval; the corpora are small, so the interval is
#: reported next to every rate rather than the rate alone.
WILSON_Z_95 = 1.959963984540054


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


def wilson_interval(
    successes: int, total: int, *, z: float = WILSON_Z_95
) -> tuple[float, float] | None:
    """A 95% Wilson score interval for a proportion, or None without samples.

    Wilson rather than the normal approximation: the corpora are small and
    rates sit near 0 or 1, where the textbook interval would report impossible
    bounds such as a negative rate.
    """

    if total <= 0:
        return None
    successes = min(max(int(successes), 0), int(total))
    proportion = successes / total
    denominator = 1 + (z**2) / total
    centre = (proportion + (z**2) / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + (z**2) / (4 * total**2))
        / denominator
    )
    return (max(centre - spread, 0.0), min(centre + spread, 1.0))


def rate_with_interval(successes: int, total: int) -> dict[str, Any]:
    """A rate with its raw counts and its interval, which is what a report needs."""

    interval = wilson_interval(successes, total)
    return {
        "count": int(successes),
        "total": int(total),
        "rate": (successes / total) if total > 0 else None,
        "interval_95": list(interval) if interval is not None else None,
    }


def prf1(true_positive: int, false_positive: int, false_negative: int) -> dict[str, Any]:
    """Precision, recall, and F1 from raw counts, with the counts kept."""

    predicted = true_positive + false_positive
    actual = true_positive + false_negative
    precision = (true_positive / predicted) if predicted else None
    recall = (true_positive / actual) if actual else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "true_positive": int(true_positive),
        "false_positive": int(false_positive),
        "false_negative": int(false_negative),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def prf1_from_sets(
    expected: Iterable[str], observed: Iterable[str]
) -> dict[str, Any]:
    """Precision/recall/F1 for a set of expected against a set of observed items."""

    expected_set = set(expected)
    observed_set = set(observed)
    return prf1(
        len(expected_set & observed_set),
        len(observed_set - expected_set),
        len(expected_set - observed_set),
    )


def coverage(expected: Iterable[str], observed: Iterable[str]) -> dict[str, Any]:
    """How much of an annotated set the response actually carried.

    Used for annotations that must all be present rather than merely ranked:
    critical tokens, annotated regions, and required explanation atoms.
    """

    expected_set = set(expected)
    observed_set = set(observed)
    matched = expected_set & observed_set
    return {
        "expected": len(expected_set),
        "observed": len(observed_set),
        "matched": len(matched),
        "missing": sorted(expected_set - observed_set),
        "extra": sorted(observed_set - expected_set),
        "rate": (len(matched) / len(expected_set)) if expected_set else None,
    }


def region_completeness(
    expected: Iterable[str], observed: Iterable[str]
) -> dict[str, Any]:
    """Region-level completeness: how many annotated regions were transcribed.

    Reported with the observed side, because a build that transcribes every
    annotated region while inventing ten more is not the same result as one
    that finds exactly the annotated set.
    """

    report = coverage(expected, observed)
    report["precision"] = (
        report["matched"] / report["observed"] if report["observed"] else None
    )
    return report


def coverage_by_contains(
    expected: Iterable[str], observed: Iterable[str]
) -> dict[str, Any]:
    """Coverage where an annotated label counts as found inside any observation.

    Transcribed regions and critical tokens are annotated as the text a human
    read in the picture, while a build reports the text it recognised; the
    annotated label is therefore a fragment of the observation rather than an
    equal string.
    """

    wanted = list(dict.fromkeys(str(item) for item in expected))
    seen = [str(item) for item in observed]
    matched = [item for item in wanted if any(item in text for text in seen)]
    return {
        "expected": len(wanted),
        "observed": len(seen),
        "matched": len(matched),
        "missing": [item for item in wanted if item not in matched],
        "rate": (len(matched) / len(wanted)) if wanted else None,
    }


def prf1_by_contains(
    expected: Iterable[str], observed: Iterable[str]
) -> dict[str, Any]:
    """Precision/recall/F1 for annotated items matched inside observations.

    Used where the observation is a longer string than the annotation (a
    transcribed region, a rendered relation): an expected item is a true
    positive when it appears inside a reported item, and a reported item is a
    false positive when it contains none of the annotated items.
    """

    report = coverage_by_contains(expected, observed)
    wanted = [str(item) for item in dict.fromkeys(expected)]
    seen = [str(item) for item in observed]
    matched_observed = [
        text for text in seen if any(item in text for item in wanted)
    ]
    counts = prf1(
        report["matched"],
        len(seen) - len(matched_observed),
        report["expected"] - report["matched"],
    )
    return {
        **counts,
        "expected_labels": wanted,
        "matched_labels": [
            item for item in wanted if any(item in text for text in seen)
        ],
        "unmatched_observed": [text for text in seen if text not in matched_observed],
    }
