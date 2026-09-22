"""Quality gates for one evaluation run.

The protocol refuses a single blended accuracy number, so a release gate is a
list of per-layer conditions instead of one threshold. Each condition names a
layer, a metric, an absolute bound, and the smallest number of annotated
samples it may be judged on; a layer that is *unavailable* fails its gate,
because "not measured" is not a pass. Every failure is classified into one
error class so the follow-up work can be split by cause rather than by symptom.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .claims import jsonable
from .schema import SchemaError


GATE_VERSION = "quality-gates-0.1"

#: Which error class a failing layer belongs to when the gate does not say.
DEFAULT_ERROR_CLASSES: Mapping[str, str] = {
    "source_import": "data",
    "ocr_transcription": "ocr",
    "layout_regions": "layout",
    "reading_order_relations": "relation",
    "notation_resolution": "notation",
    "statement_fidelity": "fact",
    "retrieval": "retrieval",
    "conflict_and_response_state": "fact",
    "explanation": "explanation",
    "performance_and_degradation": "performance",
}

#: Failures that are about the harness rather than about the component.
ANNOTATION_CLASS = "annotation"
ENVIRONMENT_CLASS = "environment"
DATA_CLASS = "data"

ERROR_CLASSES: tuple[str, ...] = (
    DATA_CLASS,
    ANNOTATION_CLASS,
    "ocr",
    "layout",
    "relation",
    "notation",
    "retrieval",
    "fact",
    "explanation",
    "performance",
    ENVIRONMENT_CLASS,
)

DEFAULT_GATES_PATH = (
    Path(__file__).resolve().parents[3] / "evaluation" / "quality-gates.json"
)


@dataclass(frozen=True)
class Gate:
    """One absolute floor or ceiling on one metric of one layer."""

    layer: str
    metric: str
    floor: float | None = None
    ceiling: float | None = None
    samples_at_least: int = 1
    mode: str | None = None
    error_class: str = ""
    unavailable_class: str = ENVIRONMENT_CLASS
    note: str = ""

    @property
    def name(self) -> str:
        suffix = f"@{self.mode}" if self.mode else ""
        return f"{self.layer}{suffix}.{self.metric}"

    def resolved_error_class(self) -> str:
        return self.error_class or DEFAULT_ERROR_CLASSES.get(self.layer, DATA_CLASS)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], where: str) -> Gate:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: every gate must be an object")
        layer = str(payload.get("layer") or "").strip()
        metric = str(payload.get("metric") or "").strip()
        if not layer or not metric:
            raise SchemaError(f"{where}: a gate needs layer and metric")
        floor = _optional_number(payload.get("floor"), where, "floor")
        ceiling = _optional_number(payload.get("ceiling"), where, "ceiling")
        if floor is None and ceiling is None:
            raise SchemaError(f"{where}: a gate needs a floor or a ceiling")
        error_class = str(payload.get("error_class") or "").strip()
        if error_class and error_class not in ERROR_CLASSES:
            raise SchemaError(
                f"{where}: error_class must be one of {list(ERROR_CLASSES)}"
            )
        mode = str(payload.get("mode") or "").strip() or None
        return cls(
            layer=layer,
            metric=metric,
            floor=floor,
            ceiling=ceiling,
            samples_at_least=max(int(payload.get("samples_at_least") or 1), 1),
            mode=mode,
            error_class=error_class,
            unavailable_class=str(
                payload.get("unavailable_class") or ENVIRONMENT_CLASS
            ),
            note=str(payload.get("note") or ""),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "metric": self.metric,
            "mode": self.mode,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "samples_at_least": self.samples_at_least,
            "error_class": self.resolved_error_class(),
            "note": self.note,
        }


@dataclass(frozen=True)
class RegressionLimit:
    """How far one metric may fall compared with a recorded baseline run."""

    layer: str
    metric: str
    max_drop: float
    mode: str | None = None
    error_class: str = ""

    @property
    def name(self) -> str:
        return f"{self.layer}.{self.metric}"

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], where: str
    ) -> RegressionLimit:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: every regression limit must be an object")
        layer = str(payload.get("layer") or "").strip()
        metric = str(payload.get("metric") or "").strip()
        if not layer or not metric:
            raise SchemaError(f"{where}: a regression limit needs layer and metric")
        return cls(
            layer=layer,
            metric=metric,
            max_drop=float(payload.get("max_drop") or 0.0),
            mode=str(payload.get("mode") or "").strip() or None,
            error_class=str(payload.get("error_class") or "").strip(),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "metric": self.metric,
            "mode": self.mode,
            "max_drop": self.max_drop,
        }


@dataclass(frozen=True)
class GateOutcome:
    """One judged condition, with the reason written out in full."""

    gate: str
    layer: str
    metric: str
    ok: bool
    detail: str
    error_class: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "layer": self.layer,
            "metric": self.metric,
            "ok": self.ok,
            "detail": self.detail,
            "error_class": self.error_class,
        }


@dataclass(frozen=True)
class GateReport:
    """The gate verdict for one run, plus the follow-up work it implies."""

    version: str
    outcomes: tuple[GateOutcome, ...]
    baseline: Mapping[str, Any] | None = None
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def failures(self) -> tuple[GateOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.ok)

    def followups(self) -> list[dict[str, str]]:
        return [
            {
                "error_class": outcome.error_class,
                "layer": outcome.layer,
                "metric": outcome.metric,
                "detail": outcome.detail,
                "task": (
                    f"{outcome.error_class}: {outcome.layer}.{outcome.metric} — "
                    f"{outcome.detail}"
                ),
            }
            for outcome in self.failures
        ]

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ok": self.ok,
            "outcomes": [outcome.as_payload() for outcome in self.outcomes],
            "failures": [outcome.as_payload() for outcome in self.failures],
            "followups": jsonable(self.followups()),
            "baseline": dict(self.baseline) if self.baseline else None,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class GateSet:
    """The conditions a build has to meet, and how far it may fall behind."""

    version: str
    gates: tuple[Gate, ...]
    regression_limits: tuple[RegressionLimit, ...] = ()
    description: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], where: str) -> GateSet:
        if not isinstance(payload, Mapping):
            raise SchemaError(f"{where}: a gate set must be an object")
        gates = payload.get("gates")
        if not isinstance(gates, list) or not gates:
            raise SchemaError(f"{where}: a gate set needs a non-empty gates list")
        limits = payload.get("regression_limits") or []
        if not isinstance(limits, list):
            raise SchemaError(f"{where}: regression_limits must be a list")
        return cls(
            version=str(payload.get("version") or GATE_VERSION),
            gates=tuple(
                Gate.from_payload(item, f"{where}#{position}")
                for position, item in enumerate(gates, start=1)
            ),
            regression_limits=tuple(
                RegressionLimit.from_payload(item, f"{where}#limit{position}")
                for position, item in enumerate(limits, start=1)
            ),
            description=str(payload.get("description") or ""),
            notes=tuple(str(note) for note in payload.get("notes") or ()),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "description": self.description,
            "gates": [gate.as_payload() for gate in self.gates],
            "regression_limits": [
                limit.as_payload() for limit in self.regression_limits
            ],
            "notes": list(self.notes),
        }


def load_gates(path: Path) -> GateSet:
    """Read a gate set from disk, and refuse an unreadable or partial one."""

    path = Path(path)
    if not path.is_file():
        raise SchemaError(f"Gate set is missing: {path}")
    return GateSet.from_payload(json.loads(path.read_text(encoding="utf-8")), str(path))


def metric_value(metrics: Mapping[str, Any], key: str) -> float | None:
    """Read one comparable number out of a layer's metrics.

    A rate is reported as counts plus a rate plus an interval, precision and
    recall arrive as a triple, and coverage arrives as matched over expected.
    A gate names the metric, not the shape, so the number is read the same way
    a reviewer would read it off the report.
    """

    if key not in metrics:
        return None
    value = metrics[key]
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        for candidate in ("rate", "f1", "value", "precision", "recall"):
            inner = value.get(candidate)
            if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                return float(inner)
        matched = value.get("matched")
        expected = value.get("expected")
        if isinstance(matched, int) and isinstance(expected, int) and expected > 0:
            return matched / expected
    return None


def evaluate_gates(
    gate_set: GateSet,
    layers: Sequence[Mapping[str, Any]],
    *,
    baseline: Mapping[str, Any] | None = None,
) -> GateReport:
    """Judge one run against its gate set, and against a recorded baseline.

    ``layers`` is the run's layer list in payload form; ``baseline`` is an
    earlier run's payload, so the relative limits compare like with like.
    """

    outcomes = [
        *_absolute_outcomes(gate_set.gates, layers),
        *_regression_outcomes(gate_set.regression_limits, layers, baseline),
    ]
    notes: list[str] = []
    unavailable = [
        outcome.gate
        for outcome in outcomes
        if not outcome.ok and outcome.error_class == ENVIRONMENT_CLASS
    ]
    if unavailable:
        notes.append(
            "Unavailable layers are environment failures until a machine with the "
            f"required capability runs them: {', '.join(sorted(unavailable))}"
        )
    if gate_set.regression_limits and baseline is None:
        notes.append(
            "No baseline run was given, so the relative limits were not judged; "
            "pass an earlier run.json to compare against a recorded baseline."
        )
    return GateReport(
        version=gate_set.version,
        outcomes=tuple(outcomes),
        baseline=_baseline_summary(baseline),
        notes=tuple(notes),
    )


def _baseline_summary(baseline: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep the identity of the baseline run, not a second copy of its report."""

    if baseline is None:
        return None
    manifest = baseline.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    environment = manifest.get("environment") or baseline.get("environment")
    environment = environment if isinstance(environment, Mapping) else {}
    return {
        "run_id": str(manifest.get("run_id") or baseline.get("run_id") or ""),
        "ok": bool(baseline.get("ok", False)),
        "started_at": str(manifest.get("started_at") or ""),
        "hardware_profile": str(environment.get("hardware_profile") or ""),
    }


def _absolute_outcomes(
    gates: Sequence[Gate], layers: Sequence[Mapping[str, Any]]
) -> list[GateOutcome]:
    return [outcome for gate in gates for outcome in _gate_outcomes(gate, layers)]


def _gate_outcomes(
    gate: Gate, layers: Sequence[Mapping[str, Any]]
) -> list[GateOutcome]:
    candidates = [
        layer
        for layer in layers
        if str(layer.get("layer")) == gate.layer
        and (gate.mode is None or str(layer.get("mode")) == gate.mode)
    ]
    if not candidates:
        return [
            GateOutcome(
                gate=gate.name,
                layer=gate.layer,
                metric=gate.metric,
                ok=False,
                detail="the run does not report this layer at all",
                error_class=ENVIRONMENT_CLASS,
            )
        ]
    outcomes: list[GateOutcome] = []
    for layer in candidates:
        name = f"{gate.layer}@{layer.get('mode')}.{gate.metric}"
        status = str(layer.get("status") or "")
        if status != "measured":
            outcomes.append(
                GateOutcome(
                    gate=name,
                    layer=gate.layer,
                    metric=gate.metric,
                    ok=False,
                    detail=(
                        f"the layer is {status or 'unreported'}: "
                        + "; ".join(str(note) for note in layer.get("notes") or ())
                    ),
                    error_class=str(gate.unavailable_class or ENVIRONMENT_CLASS),
                )
            )
            continue
        samples = int(layer.get("sample_count") or 0)
        if samples < gate.samples_at_least:
            outcomes.append(
                GateOutcome(
                    gate=name,
                    layer=gate.layer,
                    metric=gate.metric,
                    ok=False,
                    detail=(
                        f"only {samples} annotated sample(s) back this layer; the "
                        f"gate needs at least {gate.samples_at_least}"
                    ),
                    error_class=ANNOTATION_CLASS,
                )
            )
            continue
        value = metric_value(dict(layer.get("metrics") or {}), gate.metric)
        if value is None:
            outcomes.append(
                GateOutcome(
                    gate=name,
                    layer=gate.layer,
                    metric=gate.metric,
                    ok=False,
                    detail=(
                        f"the layer is measured but reports no {gate.metric}; the "
                        "metric has to be published before it can be gated"
                    ),
                    error_class=ANNOTATION_CLASS,
                )
            )
            continue
        outcomes.append(_judge(gate, name, value, samples))
    return outcomes


def _judge(gate: Gate, name: str, value: float, samples: int) -> GateOutcome:
    error_class = gate.resolved_error_class()
    if gate.floor is not None and value < gate.floor:
        return GateOutcome(
            gate=name,
            layer=gate.layer,
            metric=gate.metric,
            ok=False,
            detail=(
                f"{gate.metric}={value:.4f} over {samples} sample(s) is below the "
                f"floor {gate.floor}"
                + (f" ({gate.note})" if gate.note else "")
            ),
            error_class=error_class,
        )
    if gate.ceiling is not None and value > gate.ceiling:
        return GateOutcome(
            gate=name,
            layer=gate.layer,
            metric=gate.metric,
            ok=False,
            detail=(
                f"{gate.metric}={value:.4f} over {samples} sample(s) is above the "
                f"ceiling {gate.ceiling}"
                + (f" ({gate.note})" if gate.note else "")
            ),
            error_class=error_class,
        )
    bounds = []
    if gate.floor is not None:
        bounds.append(f"floor {gate.floor}")
    if gate.ceiling is not None:
        bounds.append(f"ceiling {gate.ceiling}")
    return GateOutcome(
        gate=name,
        layer=gate.layer,
        metric=gate.metric,
        ok=True,
        detail=(
            f"{gate.metric}={value:.4f} over {samples} sample(s) meets "
            + " and ".join(bounds)
        ),
        error_class="",
    )


def _regression_outcomes(
    limits: Sequence[RegressionLimit],
    layers: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any] | None,
) -> list[GateOutcome]:
    if not limits:
        return []
    if baseline is None:
        # Without a recorded baseline there is nothing to compare against; the
        # absolute floors still bind, and the report says the drop was not
        # judged rather than pretending the limit passed.
        return []
    baseline_layers = [
        layer
        for layer in baseline.get("layers") or []
        if isinstance(layer, Mapping)
    ]
    outcomes: list[GateOutcome] = []
    for limit in limits:
        current = _layer_values(layers, limit)
        previous = _layer_values(baseline_layers, limit)
        if not current or not previous:
            # A layer that this run reports as anything other than measured was
            # not compared: the reason it has no numbers is the absolute gate's
            # business, and it is an environment problem rather than bad data.
            candidates = [
                entry
                for entry in layers
                if str(entry.get("layer")) == limit.layer
                and (limit.mode is None or str(entry.get("mode")) == limit.mode)
            ]
            unavailable = bool(candidates) and all(
                str(entry.get("status") or "") != "measured" for entry in candidates
            )
            outcomes.append(
                GateOutcome(
                    gate=f"{limit.name} (relative)",
                    layer=limit.layer,
                    metric=limit.metric,
                    ok=False,
                    detail=(
                        "this run reports the layer as "
                        f"{candidates[0].get('status') or 'unreported'}, so the drop "
                        "cannot be judged"
                        if unavailable
                        else
                        "this run and the baseline do not both report the metric "
                        "for the same mode"
                    ),
                    error_class=ENVIRONMENT_CLASS if unavailable else DATA_CLASS,
                )
            )
            continue
        for mode, (value, samples) in sorted(current.items()):
            previous_entry = previous.get(mode)
            if previous_entry is None:
                outcomes.append(
                    GateOutcome(
                        gate=f"{limit.layer}@{mode}.{limit.metric} (relative)",
                        layer=limit.layer,
                        metric=limit.metric,
                        ok=False,
                        detail="the baseline never measured this mode",
                        error_class=DATA_CLASS,
                    )
                )
                continue
            before, before_samples = previous_entry
            drop = before - value
            ok = drop <= limit.max_drop + 1e-12
            outcomes.append(
                GateOutcome(
                    gate=f"{limit.layer}@{mode}.{limit.metric} (relative)",
                    layer=limit.layer,
                    metric=limit.metric,
                    ok=ok,
                    detail=(
                        f"{limit.metric} moved from {before:.4f} "
                        f"({before_samples} sample(s)) to {value:.4f} "
                        f"({samples} sample(s)); the allowed drop is "
                        f"{limit.max_drop}"
                    ),
                    error_class=(
                        ""
                        if ok
                        else limit.error_class
                        or DEFAULT_ERROR_CLASSES.get(limit.layer, DATA_CLASS)
                    ),
                )
            )
    return outcomes


def _layer_values(
    layers: Sequence[Mapping[str, Any]], limit: RegressionLimit
) -> dict[str, tuple[float, int]]:
    values: dict[str, tuple[float, int]] = {}
    for layer in layers:
        if str(layer.get("layer")) != limit.layer:
            continue
        mode = str(layer.get("mode") or "")
        if limit.mode is not None and mode != limit.mode:
            continue
        if str(layer.get("status") or "") != "measured":
            continue
        value = metric_value(dict(layer.get("metrics") or {}), limit.metric)
        if value is None:
            continue
        values[mode] = (value, int(layer.get("sample_count") or 0))
    return values


def _optional_number(value: Any, where: str, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{where}: {field_name} must be a number")
    return float(value)


__all__ = [
    "ANNOTATION_CLASS",
    "DEFAULT_ERROR_CLASSES",
    "DEFAULT_GATES_PATH",
    "ENVIRONMENT_CLASS",
    "ERROR_CLASSES",
    "GATE_VERSION",
    "Gate",
    "GateOutcome",
    "GateReport",
    "GateSet",
    "RegressionLimit",
    "evaluate_gates",
    "load_gates",
    "metric_value",
]
