# Windows implementation contract

**Maturity:** provisional. These requirements have not been exercised against a Windows client or live SVN repository. Use [validation-matrix.md](validation-matrix.md) before promoting behavior to verified.

## Supported operating envelope

- Start with a dedicated, fully clean target working copy.
- Record `svn --version --quiet`, client distribution, repository URL/UUID, working-copy format, and observed command capabilities.
- Use a currently supported 1.14 LTS client as the initial validation baseline, but feature-check the installed client instead of hard-coding a patch version as permanent policy.
- First-version mutations require a complete, single-revision, non-switched target without externals in scope. Detect and stop on mixed, sparse, switched, or external boundaries.

## Process boundary

- Resolve `svn.exe` from explicit configuration before `PATH`; a GUI-only TortoiseSVN installation is insufficient.
- Invoke executables through a process API with an argument array and `shell=false` semantics.
- Resolve the system `cmd.exe` to a fully qualified path from the Windows system directory rather than the current directory or repository. Run BAT files through that executable with `/c` and a pinned working directory.
- Validate BAT paths and arguments as paths or enumerated values. Do not invent a universal quoting routine for untrusted free-form text.
- Capture XML as bytes and parse its declared encoding. Decode non-XML diagnostics with the active Windows locale and preserve undecoded bytes when uncertain.
- Canonicalize drive-letter, UNC, long, spaced, case-variant, and non-ASCII paths. Reject any mutation target outside the selected working-copy root or approved generator-output roots.

## Credentials and trust

- Reuse approved SVN credential storage or Windows credential facilities.
- Keep passwords and tokens out of prompts, plans, logs, and command-line arguments.
- Stop on certificate/trust prompts, interactive authentication, or changed repository identity.
- Redact repository URLs containing embedded credentials.

## Plan fingerprint

Store run artifacts outside the working copy under a unique run ID. Record:

- canonical working-copy roots, repository UUID, URLs, and peg revisions;
- Baseline, source revision set, target revision, and working-copy revision range;
- depth, switched paths, externals, conflicts, full local-status digest, and relevant remote-freshness evidence;
- recursive mergeinfo digest;
- source, target-history, and candidate manifests;
- tool/client/generator versions and authoritative-input hashes;
- approved path and generator-output allowlists;
- creation time and approval state.

Recompute the fingerprint before every mutation phase and before ready-to-commit. A relevant local or remote difference makes the plan stale. Preserve its evidence, then rebuild from updated states. Updating the existing Candidate does not restore validity because update produces a new candidate requiring reconciliation.

## Failure handling

On command, generator, validation, or mergeinfo failure:

1. stop the affected phase;
2. retain exit code, stdout/stderr, before/after status, hashes, and changed paths;
3. identify successful and incomplete operations;
4. report recommended recovery steps;
5. wait for user approval before revert, deletion, cleanup, or retry.

The implementation must not automatically run recursive revert or remove unexpected outputs.

## Validation routing

Run the disposable scenarios and collect the evidence in [validation-matrix.md](validation-matrix.md). Observed behavior becomes verified only for the recorded client, repository, path, and generator conditions. Keep all other behavior marked provisional.
