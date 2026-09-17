---
name: svn-semantic-merge
description: Plan and perform a controlled final-state SVN integration while preserving target changes and trustworthy mergeinfo.
---

# SVN Semantic Merge

Perform a **semantic merge**: reconstruct the source task's final intent in a dedicated target working copy, validate the resulting candidate, then account for source revisions without replaying every intermediate commit.

This skill is advisory and approval-gated. It has no verified automation scripts and must not claim Windows or live-repository validation. It finishes at **ready to commit** and never runs `svn commit`.

## Rule maturity

- **Required**: SVN semantic or safety invariant. Apply it unless the user changes the task boundary.
- **Provisional**: implementation behavior that still needs disposable Windows/SVN validation. Label it as unverified.
- **Project-specific**: repository layout, generator, test, ledger, or release convention. Obtain it from repository documentation or the user; do not infer it from filenames.

## Load references by branch

- Read [candidate-reconciliation.md](references/candidate-reconciliation.md) whenever planning or applying content, tree, or property changes.
- Read [generated-artifacts.md](references/generated-artifacts.md) when any binary, configuration-generated output, BAT file, or third-party generator is involved.
- Read [revision-accounting.md](references/revision-accounting.md) before classifying revisions or proposing `--record-only`.
- Read [windows-validation.md](references/windows-validation.md) when implementing or operating the workflow on Windows.
- Read [validation-matrix.md](references/validation-matrix.md) only when validating or promoting provisional behavior.

## State model

Reason from four fixed states:

- **Baseline**: the last source state already represented or accounted for in the target.
- **Source final**: the source task at an explicit URL, peg revision, and end revision.
- **Target current**: the target immediately before this integration.
- **Candidate**: the proposed target state after semantic reconciliation.

The result must reproduce the relevant `Baseline -> Source final` intent while preserving `Baseline -> Target current` changes. `Target current -> Candidate` is the only change set this run may prepare for submission.

## Eligibility gate

Use a dedicated target working copy with no unrelated local modifications. Before planning mutations, establish and freeze:

1. source and target repository URLs, repository UUID, working-copy roots, and actual SVN client version;
2. source revision set, peg revision, target revision, and a defensible Baseline;
3. working-copy revision range, depth, switched paths, externals, conflicts, local status including ignored items, and remote freshness;
4. existing explicit, inherited, non-inheritable, and subtree mergeinfo relevant to the requested scope;
5. repository-specific generators, validations, and durable-ledger policy.

Resolve “latest” to explicit revisions for both sides. An update changes a working copy: obtain approval before updating from the declared roots, require conflict-free results, then freeze the resolved coordinates instead of continuing against floating `HEAD`.

First-version support is limited to complete, single-revision, non-switched target working copies without externals in scope. Detect mixed-revision, sparse, switched, or external boundaries and stop for a separate plan.

If the source contains uncommitted changes, content planning is allowed, but revision accounting and `--record-only` are disabled. If Baseline is unreliable, allow conservative manual reconciliation, but disable whole-file fast paths and complete mergeinfo accounting.

## Controlled workflow

### 1. Build the evidence set

Collect read-only, structured SVN evidence. Build the three comparisons and account for every source, target, and candidate path as defined in [candidate-reconciliation.md](references/candidate-reconciliation.md). Include unversioned and ignored paths, tree actions, copy history, and property changes; a text patch alone is incomplete evidence.

Classify every binary before proposing its result. Integrate authoritative inputs before regenerating derived outputs. Unknown provenance remains unresolved.

### 2. Present the plan

The plan must name:

- the four frozen states and exact revision scope;
- every path decision and unresolved item;
- planned content, tree, property, binary, and generator operations;
- semantic decisions that need human judgment;
- validations and expected changed-path allowlist;
- proposed revision ledger and mergeinfo scope;
- plan fingerprint and conditions that make it stale.

Obtain **approval 1: plan approval** before any working-copy mutation.

### 3. Create the candidate

Obtain **approval 2: working-copy writes**, then apply only the approved content, tree, and property decisions. Use SVN operations for add, delete, copy, move, and replace semantics. A whole-file replacement is a fast path only when the reconciliation reference proves it safe.

AI may propose semantic resolutions, but obtain a new decision before deleting target-only behavior, substituting a different implementation for source intent, rejecting source behavior, or changing a public interface, configuration meaning, or data format without adequate validation.

For each generator group, present its exact inputs, executable or BAT file, tool version, working directory, arguments, expected outputs, and external effects. Obtain **approval 3: generator execution**. Stop on failure, unexpected output, modified authoritative input, or an unapproved side effect. Preserve the failure state and evidence; do not automatically revert it.

### 4. Validate and account

Rebuild `Target current -> Candidate` evidence and require every important path and hunk to have one recorded cause: source intent, preserved target behavior, approved adaptation, approved generated output, explicit rejection, or unresolved. Run project-specific validation. Any unexplained or unresolved effect prevents mergeinfo accounting for its revision scope.

Classify every source revision using [revision-accounting.md](references/revision-accounting.md). `partial` and `unknown` are never record-only candidates. `rejected` is not a candidate unless the user separately approves permanent blocking and names a durable ledger.

Present the exact expected mergeinfo delta and obtain **approval 4: mergeinfo mutation**. Freeze the candidate before `svn merge --record-only`; afterward prove that content, tree scheduling, and non-mergeinfo properties are unchanged and recursively review the actual mergeinfo delta plus merged/eligible results.

### 5. Recheck freshness and finish

Before declaring readiness, repeat remote and local freshness checks for all relevant source, target, mergeinfo, and generator-input paths. A relevant change makes the plan stale. Preserve the audit material and rebuild the plan; do not update the existing candidate and continue without repeating reconciliation.

Prefer content, tree operations, properties, generated artifacts, and approved mergeinfo in one atomic SVN commit. If project policy requires split commits, mark the ledger pending until all pieces are committed and reconciled.

## Hard stops

Stop the affected phase when any of these is true:

- source, target, revision scope, repository identity, or Baseline cannot be fixed sufficiently for that phase;
- the target working copy is not dedicated and clean, or has an unsupported topology;
- a plan fingerprint changes or relevant remote state advances;
- a changed path, property, tree action, binary provenance, or generator side effect is unexplained;
- a semantic deletion or substitution lacks the required user decision;
- an authentication, certificate, permission, or external-system prompt appears;
- the expected and actual mergeinfo deltas differ;
- a revision remains `partial` or `unknown` at the proposed accounting scope.

Keep credentials out of commands, plans, logs, and reports. Do not hand-edit `svn:mergeinfo`, perform URL mutations, make conflict-wide mine/theirs choices, recursively revert, or commit.

## Output contract

Finish with a ready-to-commit report containing:

- frozen source, target, Baseline, Candidate, client, and repository identity;
- source-intent, target-history, and candidate-delta evidence;
- path decisions for content, tree operations, properties, and unresolved items;
- semantic decisions approved by the user;
- generated-artifact provenance, hashes or domain checks, logs, and unexpected effects;
- project validation commands and results;
- revision ledger with evidence for every status;
- mergeinfo before/after and exact record-only scope, or the reason accounting is unsafe;
- final freshness result, remaining risks, and suggested commit-message trailers.

Store run artifacts outside the target working copy by default. Place a screened summary in version control or an issue system only when project policy requires it; permanent blocking of a rejected revision always requires such a durable record.
