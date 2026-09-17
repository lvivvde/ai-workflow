# Disposable validation matrix

**Maturity:** provisional. No scenario in this file is currently recorded as passed.

Use disposable local repositories and dedicated working copies. Never validate mutation behavior against a production repository. Record the exact Windows version, SVN client/distribution, repository-access method, working-copy format, commands, outputs, and final repository state for every case.

## Evidence required per case

Capture:

- frozen Baseline, Source final, Target current, and Candidate coordinates;
- local status including ignored items and remote-freshness evidence;
- the three manifests and path-decision ledger;
- content, tree, property, binary, and recursive mergeinfo before/after state;
- generator inputs, environment, logs, output allowlist, and hashes when applicable;
- validation results and merged/eligible queries;
- whether the expected stop, approval, or ready-to-commit condition occurred.

A passing shell exit code is not a passing scenario. The observed content and metadata must satisfy every stated invariant.

## Core scenarios

| ID | Scenario | Expected invariant |
|---|---|---|
| T01 | Source-only text edits | Source intent appears in Candidate with no unexplained path. |
| T02 | Target has independent committed edits | Target behavior survives; whole-file fast path is refused unless history proves it safe. |
| T03 | Concurrent same-file edits | Every accepted/deleted hunk is attributed and required semantic decisions are approved. |
| T04 | Add, delete, copy, move, replace | Candidate tree schedule and copy history match the approved path decisions. |
| T05 | Property-only revision | Properties are detected, reconciled, and accounted independently of text. |
| T06 | Partial revision | The revision remains eligible and is not record-only at an incomplete scope. |
| T07 | Root, inherited, and subtree mergeinfo | Recursive before/after state and merged/eligible results match the ledger. |
| T08 | Raw binary source asset | MIME/lock policy, independent target history, approval, and hashes are captured. |
| T09 | Configuration input plus BAT outputs | Inputs are integrated first; only allowlisted outputs change and provenance is complete. |
| T10 | Generator failure or unexpected output | Execution stops, evidence is preserved, and no automatic cleanup occurs. |
| T11 | Relevant remote commit after planning | Fingerprint becomes stale; the existing Candidate is not updated and reused. |
| T12 | Mixed, sparse, switched, or external target | First-version mutation stops with the detected boundary reported. |
| T13 | BAT nonzero exit and partial writes | Failure state is retained and no revision becomes complete. |
| T14 | Spaces, non-ASCII, long, case-only, and UNC paths | Process/path handling remains inside approved roots without command reinterpretation. |
| T15 | Unversioned and ignored inputs/outputs | Status evidence includes them and each is explicitly included, excluded, or unresolved. |
| T16 | Permanent rejection request | Record-only is refused until permanent-block approval and durable-ledger identity exist. |
| T17 | Record-only over an existing Candidate | Only expected mergeinfo changes; content, tree, and other properties remain frozen. |
| T18 | Split-commit project policy | Ledger remains pending until all content and mergeinfo commits are cross-referenced. |

## Promotion criteria

Promote a provisional behavior only when:

1. its relevant scenarios pass in a fresh disposable repository;
2. evidence identifies the exact supported environment and command form;
3. negative cases stop at the intended boundary without commit or silent cleanup;
4. target-only behavior, ancestry, properties, artifacts, and mergeinfo all match the path and revision ledgers;
5. rerunning the inspection phase reports the completed state consistently;
6. the result is recorded in the Windows validation notes with known limitations.

Passing one client or repository layout does not establish universal compatibility. Expand support only from observed cases, and keep unsupported topologies as explicit stops.
