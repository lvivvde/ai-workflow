# Output modes

Read only the mode that matches the current task. Combine modes when the source genuinely mixes them.

## Quick technical answer

Use for a focused question with a bounded answer.

1. Direct answer.
2. One reason or constraint.
3. Minimal example only if it resolves ambiguity.

Do not add background the user did not need.

## Code explanation

Orient the reader before details:

- purpose and boundary of the code;
- entry point and main execution path;
- state read or mutated;
- external calls and side effects;
- invariants and failure behavior;
- surprising or risky parts.

Use a small flow only when at least three components or state transitions are hard to follow in prose. Do not paste large code blocks already present in the source.

## Code review

Put actionable findings first, ordered by severity. For each finding include:

- severity and concise title;
- file and tight line location;
- triggering condition;
- concrete consequence;
- evidence or reasoning;
- smallest safe fix, when clear.

Do not bury findings under a general summary. If no actionable finding exists, say so and name any untested or uncertain area. Keep praise and style commentary out unless they affect correctness or maintainability.

## Incident or bug diagnosis

Use this reading order:

1. Current best explanation and confidence.
2. User or system impact.
3. Evidence chain from symptom to cause.
4. Competing explanations and what would distinguish them.
5. Containment, fix, and verification.
6. Prevention only when supported by the diagnosis.

Distinguish root cause, trigger, contributing conditions, and detection gap. Do not label the first plausible explanation as root cause.

## Performance report

Start with whether the result is better, worse, or inconclusive under the tested workload. Then state:

- workload, baseline, environment, sample size and measurement window;
- before/after values with units and percentage or ratio only when derivable;
- tail latency, throughput and resource cost together;
- bottleneck evidence rather than a generic optimization story;
- regression risk, missing measurements and next experiment.

Never compare numbers collected under materially different workloads without highlighting the mismatch.

## Architecture or design decision

Lead with the recommended decision and the constraint that drives it. Then cover:

- goals and non-goals;
- current constraints and assumptions;
- viable options in a repeated-field comparison;
- trade-offs, failure modes and operational cost;
- migration, compatibility, observability and rollback;
- unresolved questions that block commitment.

Do not invent a third option to make the comparison look complete. Avoid repeating the recommendation after every section.

## Long report or source synthesis

The first screen should contain:

- conclusion or status;
- three to seven findings, only if there are that many meaningful findings;
- decisions or actions with owner and deadline when the source provides them;
- the largest uncertainty or risk.

Organize the remainder by the reader's questions, not by the source document's chapter order. Preserve traceability to sources.

