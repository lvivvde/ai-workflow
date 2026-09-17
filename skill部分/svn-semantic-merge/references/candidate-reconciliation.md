# Candidate reconciliation

**Maturity:** the reconciliation invariants are required; command spelling and XML parsing are provisional until validated with the installed client.

The candidate is valid only when three fixed comparisons explain it:

| Evidence | Question answered |
|---|---|
| `Baseline -> Source final` | What intent did the source task introduce? |
| `Baseline -> Target current` | What independent target behavior must survive? |
| `Target current -> Candidate` | What will this integration actually submit? |

Do not infer safety from a clean working copy. Local cleanliness and absence of committed target changes since Baseline are separate facts.

## Build the manifests

For source and target evidence, collect the installed client's structured changed-path summary where supported, the full content/property diff, and verbose history for copy-from and tree ancestry. Scan local state including unversioned and ignored paths. Record at least:

- repository-relative path and node kind;
- content and property status;
- add, delete, copy, move, replace, or type change;
- copy-from path/revision when present;
- binary/MIME classification;
- source revision and intended behavior;
- target history since Baseline;
- candidate decision and validation evidence.

`svn diff` alone is not a complete manifest: it does not turn unversioned or ignored inputs into versioned changes, and ordinary text review does not settle ancestry or properties.

## Whole-file fast path

Whole-file replacement is allowed only when every condition is proven:

```text
target path has no local modification
AND Baseline -> Target current has no independent content change
AND it has no tree or property change that must survive
AND source and target node kind and ancestry are compatible
AND the path is not a derived artifact that should be regenerated
```

Otherwise reconcile the three states explicitly. If Baseline is unreliable, preserve Target current as potentially intentional and disable the fast path.

## Path decisions

Use SVN-aware operations rather than filesystem appearance:

| Source action | Candidate requirement |
|---|---|
| content change | Reproduce intent while preserving target-only behavior. |
| add | Decide whether the target node is absent, unversioned, or a different history; schedule the approved add or history-preserving copy. |
| delete | Inspect target changes, children, and properties before scheduling deletion. |
| copy | Preserve justified copy-from history with an SVN copy. |
| move | Preserve copy-plus-delete history with an SVN move when the mapping is valid. |
| replace/type change | Record the ancestry or node-kind change explicitly. |
| property change | Review and apply separately from file content. |
| binary change | Follow the provenance decision in `generated-artifacts.md`. |

Do not assume repository paths named `trunk`, `branches`, or `tags` have a particular semantic role. Use the explicit URLs and revisions in the integration envelope.

## Candidate attribution

Every important `Target current -> Candidate` path and hunk must have exactly one documented cause:

- source intent;
- preserved or restored target behavior;
- user-approved adaptation or equivalent implementation;
- approved generated output;
- explicit rejection;
- unresolved.

Any unexplained change is unresolved. An unresolved effect prevents record-only accounting for its revision scope.

AI may draft a resolution and its rationale. Obtain a user decision before deleting independent target behavior, substituting a different implementation for source intent, rejecting source behavior, or changing a public interface, configuration meaning, or data format without adequate validation.

## Candidate invariants

Before revision accounting, prove:

1. every source-manifest path has a target decision;
2. every target change since Baseline is preserved, superseded with evidence, or explicitly rejected by the user;
3. every candidate path is explained by the path ledger or generated-artifact ledger;
4. content, tree scheduling, and property diffs agree with the planned node history;
5. project tests or domain checks support the semantic result;
6. no relevant remote or local state changed since the plan fingerprint.

Tests support semantic judgment but do not prove mergeinfo completeness. A source with uncommitted changes can satisfy content reconciliation, but it cannot enter revision accounting until it is committed and the evidence is rebuilt against fixed revisions.
