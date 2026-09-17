# Generated artifacts and binary files

**Maturity:** provenance and approval rules are required; generator commands, deterministic behavior, and domain checks are project-specific and initially provisional.

Binary differences do not expose mergeable intent. Classify provenance before deciding what belongs in Candidate.

## Classification

| Class | Source of truth | Integration rule |
|---|---|---|
| Configuration-generated artifact | Versioned or approved configuration/data inputs | Integrate inputs, then run the approved converter. |
| Third-party generated artifact | Project inputs plus a named tool and version | Integrate inputs, then regenerate with that exact toolchain. |
| Binary source asset | The binary itself, such as an authored workbook or design asset | Use a domain merge tool or an explicitly approved whole-artifact choice. |
| Vendor or opaque artifact | External delivery with no reproducible local source | Require provenance and an explicit replacement decision. |
| Unknown | Insufficient evidence | Leave unchanged and report unresolved. |

Do not infer provenance from an extension, nearby BAT filename, or whether SVN displays a textual diff. Obtain project rules from repository documentation, verified team convention, or the user.

For a binary source asset, byte replacement is allowed only after confirming it is authoritative, the target has no independent version to preserve, MIME/lock policy is understood, and the user approves the before/after hashes.

## Reproducibility level

Record one level instead of calling every generator reproducible:

- **deterministic**: fixed inputs, tool version, and declared environment reproduce identical bytes;
- **normalized**: documented timestamp, path, or ordering noise can be normalized before comparison;
- **regenerable**: the output can be rebuilt but requires a domain-specific semantic check;
- **opaque**: the input/tool/output relationship cannot be established and remains unresolved.

A second-run no-change check is optional and applies only to generators known to be local and side-effect-free. Do not repeat commands that publish, upload, mutate databases, consume scarce licenses, or affect external systems.

## Regeneration protocol

1. Identify authoritative inputs, generator or BAT path, tool version, working directory, validated arguments, environment dependencies, expected outputs, and possible external effects.
2. Finish and review input integration before generation.
3. Capture full target status including ignored items, input and existing-output hashes, and the expected changed-path allowlist.
4. Present the exact invocation and obtain generator approval. A new network, credential, registry, service, installation, repository-write, or working-copy-external effect requires a separate authorization boundary.
5. Execute once. Capture exit code, stdout, stderr, duration, tool version, and observed file-system/SVN changes.
6. Stop on a nonzero exit, missing output, output outside the allowlist, modified authoritative input, or unapproved external effect. Preserve the failure state and evidence; do not automatically revert or delete it.
7. Review generated changes separately from hand-edited content. Use a domain inspector where available; otherwise report byte replacement and hashes without claiming semantic equivalence.
8. Attribute every accepted output to its authoritative inputs and generator.

An exit code of zero proves only that the process reported success. It does not prove output completeness, provenance, determinism, or business correctness.

## Windows BAT boundary

Treat a BAT file as executable code. Before first use, inspect its known call chain and identify external effects. Invoke it through the fully qualified system `cmd.exe` with `/c`, a pinned working directory, and validated enumerable arguments. Do not interpolate repository paths, revision text, or free-form user content into an ambient command string.

Follow `windows-validation.md` for process, credential, path, and encoding rules. Quoting behavior remains provisional until validated with the exact Windows runtime and paths in use.

## Provenance report

Report accepted and unresolved artifacts:

| Output | Class | Reproducibility | Authoritative inputs | Generator/version | Result | Evidence |
|---|---|---|---|---|---|---|

Also list source/configuration files integrated, target-only code restored, binaries regenerated, raw binary replacements, untouched or unresolved artifacts, generator failures, and every unexpected path or external effect.

A revision containing an unresolved artifact remains `partial` or `unknown` at that path scope.
