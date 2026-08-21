# Sources and adaptation notes

This skill synthesizes mechanisms from the following public projects. It does not install or depend on them.

- [blader/humanizer at `e2e92e7`](https://github.com/blader/humanizer/tree/e2e92e7b4b8229253ed5c8e81dc65463fdeddda5), MIT: preserving claims, avoiding invented facts, matching register, detecting clusters instead of single stylistic tells, and using a final meaning-preservation audit.
- [hardikpandya/stop-slop at `8da1f03`](https://github.com/hardikpandya/stop-slop/tree/8da1f030185bdfe8471220585162991eaeb970e9), MIT: directness, removal of throat-clearing and formulaic rhetoric, rhythm checks, and a compact quality score.
- [y10reo/stop-slop-zh at `62896a1`](https://github.com/y10reo/stop-slop-zh/tree/62896a15200874bb809c52eb3f46bdc856d71d1d), MIT: Chinese-specific structural residue, genre gates, evidence-to-claim boundaries, nominalization and abstract-actor checks.
- [Leonxlnx/taste-skill at `843c8dd`](https://github.com/Leonxlnx/taste-skill/tree/843c8dd4d18ccff0d5a9cd4b0b71d7dbf7278293), MIT: infer the brief before applying rules, tune density to audience and context, resist default templates, declare out-of-scope cases, and finish with a pre-flight check.
- [alchaincyf/nuwa-skill at `27642f5`](https://github.com/alchaincyf/nuwa-skill/tree/27642f5bfed2dc1bbf8ee59a2c1ee602a626bbd7), MIT: turn source material into operational mental models, decision heuristics, expression DNA, anti-patterns and honest boundaries; retain only principles that transfer to new cases.

## Deliberate departures

- No blanket ban on adverbs, passive voice, em dashes, questions, or three-item lists. Such rules create false positives and can damage technical precision.
- No detector-evasion promise. Readability, traceability, and decision quality are the targets.
- No frontend design rules from Taste Skill and no personality imitation workflow from NUWA. Only their transferable control mechanisms are used.
- No mandatory template for every answer. Output shape follows task type, risk and source volume.
