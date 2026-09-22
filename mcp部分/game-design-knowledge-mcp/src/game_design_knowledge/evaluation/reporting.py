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


@dataclass(frozen=True)
class CapabilityResult:
    """One capability pack's own slice of a run.

    A pack that was declared by the corpus but never exercised is reported with
    its sample count rather than being dropped, so "not measured" and "measured
    and green" stay distinguishable.
    """

    capability: str
    sample_count: int
    modes: tuple[str, ...]
    response_states: Mapping[str, Mapping[str, int]]

    def as_payload(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "sample_count": self.sample_count,
            "modes": list(self.modes),
            "response_states": jsonable(self.response_states),
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
    capability_results: Sequence[CapabilityResult] = (),
    hardware_profile: str = "",
) -> str:
    lines: list[str] = [f"# Evaluation report {run_id}", ""]

    lines.append("## Run")
    lines.append("")
    lines.append(f"- hardware profile: {hardware_profile or 'unspecified'}")
    lines.append(f"- corpora: {len(corpus_descriptions)}")
    lines.append(
        "- samples: "
        f"{sum(int(corpus.get('sample_count') or 0) for corpus in corpus_descriptions)}"
    )
    lines.append(
        "- scoring: every layer keeps its own raw counts, sample counts and "
        "95% Wilson intervals; no layer is blended into a single score."
    )
    lines.append("")

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

    if capability_results:
        lines.append("## Capability packs")
        lines.append("")
        lines.append(
            "A pack's numbers cover only the samples that declare it, so a pack "
            "that measured badly cannot hide behind a pack that measured well."
        )
        lines.append("")
        lines.append("| capability pack | samples | modes | expected → observed |")
        lines.append("|---|---|---|---|")
        for capability in capability_results:
            observed = ", ".join(
                f"{expected}→{observed}:{count}"
                for expected, row in sorted(capability.response_states.items())
                for observed, count in sorted(row.items())
            )
            lines.append(
                f"| {capability.capability} | {capability.sample_count} | "
                f"{', '.join(capability.modes) or '—'} | {observed or '—'} |"
            )
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
    if isinstance(value, Mapping):
        return _format_mapping(value)
    return str(value)


def _format_mapping(value: Mapping[str, Any]) -> str:
    """Render the report's structured metrics as counts, rates, and intervals."""

    if {"count", "total", "rate"} <= set(value):
        interval = value.get("interval_95")
        rate = "n/a" if value.get("rate") is None else f"{value['rate']:.4f}"
        bounds = (
            f" [{interval[0]:.4f}, {interval[1]:.4f}]"
            if isinstance(interval, (list, tuple)) and len(interval) == 2
            else ""
        )
        return f"{value['count']}/{value['total']} rate={rate}{bounds}"
    if {"precision", "recall", "f1"} <= set(value):
        return (
            f"P={_short(value['precision'])} R={_short(value['recall'])} "
            f"F1={_short(value['f1'])} "
            f"(tp={value.get('true_positive')} fp={value.get('false_positive')} "
            f"fn={value.get('false_negative')})"
        )
    if {"matched", "expected"} <= set(value):
        missing = value.get("missing") or []
        return (
            f"matched={value['matched']}/{value['expected']} "
            f"rate={_short(value.get('rate'))} missing={list(missing)}"
        )
    return str(dict(value))


def _short(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
