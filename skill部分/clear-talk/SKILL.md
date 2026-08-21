---
name: clear-talk
description: Remove AI-sounding structure and wording from substantial technical content while preserving facts, evidence, and useful detail. Use for technical explanations, code analysis, code reviews, incident reports, performance reports, architecture decisions, and long engineering summaries that need to read clearly and naturally. Do not use merely to make short casual answers more formal.
---

# Clear Talk

Make AI-produced technical content clear, natural, and easy to scan without removing the details needed to verify it. Optimize for reading and decisions, not for sounding polished.

## Read the task before choosing a format

Infer these factors from the request and source material:

- What the reader needs to decide or do next.
- Risk if the conclusion is wrong.
- Whether the source is code, logs, metrics, an incident, a design proposal, or a mixed report.
- Whether the user wants a quick answer, a working-level explanation, or a deep review.

Do not announce this classification. Ask one question only when different interpretations would materially change the answer. Otherwise choose a sensible format and proceed.

For specialized output shapes, read [references/modes.md](references/modes.md) only for the relevant mode.

## Information order

Put information in the order an engineer will consume it:

1. Give the answer, decision, or main finding in one to three sentences.
2. State impact and required action when either exists.
3. Present the strongest evidence and important caveats.
4. Put background, alternatives, and exhaustive detail later.

Do not force all four layers into a small answer. For long work, make the opening independently useful so the reader can stop after it.

## Preserve engineering truth

- Preserve names, numbers, units, code behavior, citations, and defined terms.
- Never invent metrics, benchmarks, line numbers, causes, or certainty.
- Separate observed facts from inference when a reader could confuse them. State confidence only when it affects the decision.
- If evidence supports correlation but not causation, say so.
- When summarizing code, explain control flow, state transitions, invariants, failure paths, and side effects. Do not narrate syntax line by line.
- Link to concrete files and lines when available. Quote only the smallest code fragment needed to prove the point.

## Make structure earn its cost

- Use a heading only when it helps the reader jump to a distinct question.
- Prefer short paragraphs for reasoning and lists for parallel facts, actions, or findings.
- Avoid a list in which every bullet contains a bold mini-heading and a paragraph. Use a compact table only for repeated fields or direct comparisons.
- Keep one idea per paragraph. Split dense arguments, but do not turn every sentence into its own paragraph.
- Do not repeat the same conclusion in the opening, body, and closing.
- End on the last useful fact, decision, risk, or action. Do not add a generic summary or offer to continue.

## Game backend lens

When relevant, prioritize the operational variables a game backend engineer needs:

- workload and assumptions: concurrency, throughput, burst shape, payload size, hot paths;
- latency and capacity: p50/p95/p99, CPU, memory, allocation and GC, network, database and cache pressure;
- correctness: state ownership, consistency, ordering, idempotency, retries, timeouts, duplicate delivery and partial failure;
- operations: observability, failure radius, degradation behavior, rollout, compatibility and rollback.

Do not manufacture this checklist in unrelated answers. Mention only factors that change the conclusion or reveal a missing measurement.

## Remove AI residue contextually

Apply [references/anti-slop.md](references/anti-slop.md) when drafting or revising substantial prose. The aim is not detector evasion. It is direct, source-safe technical writing.

Preserve deliberate terminology, necessary passive voice, legal or protocol precision, and useful repetition. A single stylistic pattern is not a defect by itself. Fix clusters that add friction or hide weak reasoning.

## Final pass

Before returning a substantial answer, check:

- Can the reader identify the conclusion and next action without reading everything?
- Does each important claim point to evidence, code, metrics, or an explicitly labeled inference?
- Did the answer omit any condition that could reverse the conclusion?
- Is any section repeating, announcing, decorating, or softening instead of informing?
- Are code locations, units, severities, and confidence labels accurate and consistent?

Revise failed items once. Do not expose the checklist or add a ceremonial change summary unless the user asks.
