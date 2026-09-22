"""Layered, stratum-aware reporting.

Reports never publish a single blended accuracy number: every layer keeps its
own status and metrics, and unavailable capabilities are reported as
``unavailable`` instead of being folded into a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .claims import jsonable
from .schema import EVALUATION_LAYERS


@dataclass(frozen=True)
class LayerResult:
    layer: str
    mode: str
    status: str
    metrics: Mapping[str, Any] = field(default_factory=dict)
    sample_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "mode": self.mode,
            "status": self.status,
            "metrics": jsonable(self.metrics),
            "sample_count": len(self.sample_ids),
            "sample_ids": list(self.sample_ids),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class StratumResult:
    label: str
    strata: Mapping[str, str]
    sample_count: int
    response_states: Mapping[str, Mapping[str, int]]
    matrices: Mapping[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "strata": dict(self.strata),
            "sample_count": self.sample_count,
            "response_states": jsonable(self.response_states),
            "matrices": jsonable(self.matrices),
        }


def ordered_layers(layers: Iterable[str]) -> list[str]:
    """Keep the protocol's layer order rather than discovery order."""

    known = [layer for layer in EVALUATION_LAYERS if layer in set(layers)]
    extras = sorted(set(layers) - set(EVALUATION_LAYERS))
    return known + extras


def group_layers(results: Sequence[LayerResult]) -> dict[str, list[LayerResult]]:
    grouped: dict[str, list[LayerResult]] = {}
    for result in results:
        grouped.setdefault(result.layer, []).append(result)
    return grouped


def render_markdown(
    *,
    run_id: str,
    corpus_descriptions: Sequence[Mapping[str, Any]],
    layer_results: Sequence[LayerResult],
    stratum_results: Sequence[StratumResult],
    response_states: Mapping[str, Mapping[str, Mapping[str, int]]],
    invariant_payloads: Sequence[Mapping[str, Any]],
    environment: Mapping[str, Any],
    extra_notes: Sequence[str] = (),
) -> str:
    lines: list[str] = [f"# Evaluation report {run_id}", ""]

    lines.append("## Corpora")
    lines.append("")
    lines.append("| corpus | split | version | frozen | samples | fingerprint |")
    lines.append("|---|---|---|---|---|---|")
    for corpus in corpus_descriptions:
        lines.append(
            "| {name} | {split} | {version} | {frozen} | {sample_count} | "
            "`{fingerprint}` |".format(**corpus)
        )
    lines.append("")

    lines.append("## Environment")
    lines.append("")
    for key, value in sorted(environment.items()):
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("## Release-blocking invariants")
    lines.append("")
    lines.append("| invariant | result | violations |")
    lines.append("|---|---|---|")
    for payload in invariant_payloads:
        lines.append(
            f"| {payload['name']} | "
            f"{'pass' if payload['ok'] else 'FAIL'} | "
            f"{payload['violation_count']} |"
        )
    lines.append("")

    lines.append("## Layers")
    lines.append("")
    lines.append("No layer is blended into a single score.")
    lines.append("")
    grouped = group_layers(layer_results)
    for layer in ordered_layers(grouped):
        results = grouped[layer]
        lines.append(f"### {layer}")
        lines.append("")
        lines.append("| mode | status | samples | metrics |")
        lines.append("|---|---|---|---|")
        for result in results:
            metrics = ", ".join(
                f"{key}={_format(value)}"
                for key, value in sorted(result.metrics.items())
            )
            lines.append(
                f"| {result.mode} | {result.status} | {len(result.sample_ids)} | "
                f"{metrics or '—'} |"
            )
        for result in results:
            for note in result.notes:
                lines.append(f"- ({result.mode}) {note}")
        lines.append("")

    lines.append("## Response states")
    lines.append("")
    for mode, matrix in response_states.items():
        lines.append(f"### {mode}")
        lines.append("")
        lines.append(_matrix_table(matrix))
        lines.append("")

    lines.append("## Strata")
    lines.append("")
    lines.append("| stratum | samples | expected → observed |")
    lines.append("|---|---|---|")
    for result in stratum_results:
        observed = ", ".join(
            f"{expected}→{observed}:{count}"
            for expected, row in sorted(result.response_states.items())
            for observed, count in sorted(row.items())
        )
        lines.append(f"| {result.label} | {result.sample_count} | {observed or '—'} |")
    lines.append("")

    if extra_notes:
        lines.append("## Notes")
        lines.append("")
        for note in extra_notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _matrix_table(matrix: Mapping[str, Mapping[str, int]]) -> str:
    if not matrix:
        return "_no samples_"
    observed_labels = sorted(
        {label for row in matrix.values() for label in row}
    )
    lines = ["| expected \\\\ observed | " + " | ".join(observed_labels) + " |"]
    lines.append("|---" * (len(observed_labels) + 1) + "|")
    for expected in sorted(matrix):
        row = matrix[expected]
        cells = [str(row.get(label, 0)) for label in observed_labels]
        lines.append(f"| {expected} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _format(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
