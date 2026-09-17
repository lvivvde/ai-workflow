# Revision accounting

**Maturity:** the semantic rules are required; exact client output and command behavior remain provisional until validated on the target Windows SVN distribution.

Revision accounting asks: **has every relevant effect of this committed source revision been deliberately settled for this target path scope?** It is path-scoped and source-path-scoped, not a bare revision-number checklist.

## Preconditions

Account only a committed, fixed source identified by repository UUID, source URL, peg revision, and revision set. Uncommitted source changes may be reconciled as content but have no honest mergeinfo identity.

Before classification, capture recursively relevant explicit, inherited, non-inheritable, and subtree mergeinfo. Treat `svn mergeinfo --show-revs merged|eligible` as evidence about operative revisions, not as a complete audit ledger.

## Statuses

| Status | Meaning | Default `--record-only` eligibility |
|---|---|---:|
| `integrated` | Every relevant source effect is present in the target candidate. | Yes |
| `superseded` | A proven equivalent or newer target implementation settles every relevant source effect. | Yes |
| `rejected` | The source effect is deliberately excluded. | No |
| `partial` | At least one relevant path, property, tree action, or hunk is deferred. | No |
| `unknown` | Evidence is incomplete or ambiguous. | No |

`superseded` requires the source intent, corresponding target implementation, equivalence rationale, and supporting test or human confirmation. Similar-looking code is insufficient.

`rejected` becomes eligible only through an explicit permanent-blocking exception. The user must approve the exact source path, revision, target scope, and reason, and name a durable versioned or issue-tracked ledger. SVN mergeinfo cannot distinguish a block from a real integration.

## Completeness rule

A revision is complete at a proposed mergeinfo scope only when every relevant content change, tree action, property change, and artifact effect in that scope is `integrated`, evidenced `superseded`, or an approved permanent block.

- One `partial` or `unknown` effect makes the revision ineligible at that scope.
- Mergeinfo cannot express “some hunks of this revision on this path.” Keep such a revision incomplete.
- Default to branch-root accounting. Use subtree accounting only when the repository already accepts it, the path mapping is stable, and the entire subtree scope is settled.
- Do not create file-level mergeinfo to simulate hunk selection.
- Use `svn merge --record-only`; do not hand-edit `svn:mergeinfo`.

## Record-only protocol

`--record-only` changes mergeinfo while leaving content application to the semantic merge. Because Candidate already contains local changes, treat this as a controlled exception:

1. Freeze Candidate status, content/tree/property diff, hashes where useful, and recursive mergeinfo state.
2. Present the precise source URL, peg revision, revision set, target scope, expected property delta, and ledger evidence.
3. Obtain the dedicated mergeinfo approval.
4. Run only the approved record-only operation.
5. Prove that file content, tree scheduling, and non-mergeinfo properties did not change.
6. Recursively compare explicit and inherited mergeinfo before and after; review any elision or subtree change.
7. Re-run merged/eligible queries and compare them with the revision ledger.

Any unexpected property or eligibility result stops the workflow. Do not automatically normalize or repair mergeinfo.

## Atomicity and ledger

Prefer the reconciled content, tree operations, properties, generated outputs, and approved mergeinfo in one SVN commit. If project policy requires split commits, keep accounting `pending` until every part is committed and cross-referenced.

For a permanent block, the durable ledger must record:

- repository UUID, source URL and revision;
- target path scope;
- rejected effect and reason;
- approving user or project decision;
- date and related commit, issue, or audit-file identifier.

Suggested commit-message trailers for settled revisions:

```text
SVN-Integration-Source: ^/branches/feature-a@<peg>
SVN-Integration-Method: semantic-merge
SVN-Integrated: r101
SVN-Superseded: r102 (equivalent target implementation: <evidence>)
SVN-Partial: r103 (deferred: <effect>)
SVN-Permanent-Block: r104 (<durable-ledger-id>)
```

Do not list a normal `rejected` revision as merged. A later run should be able to reconstruct why every revision was integrated, superseded, left eligible, or permanently blocked.
