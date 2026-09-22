"""Offline evaluation harness for the game design knowledge index.

The harness reports component, end-to-end, stratum, and response-state results
separately. It never publishes a single blended accuracy number, and it fails
the run when any release-blocking invariant is violated.
"""

from .corpus import EvaluationCorpus, load_corpus
from .harness import EvaluationRun, run_evaluation
from .invariants import (
    RELEASE_BLOCKING_INVARIANTS,
    InvariantResult,
    SampleExecution,
    evaluate_invariants,
    violation_summary,
)
from .schema import SchemaError

__all__ = [
    "EvaluationCorpus",
    "EvaluationRun",
    "InvariantResult",
    "RELEASE_BLOCKING_INVARIANTS",
    "SampleExecution",
    "SchemaError",
    "evaluate_invariants",
    "load_corpus",
    "run_evaluation",
    "violation_summary",
]
