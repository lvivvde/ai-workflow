# Anti-slop checks for technical writing

Use these as diagnostic checks, not as proof that text is AI-generated and not as universal bans.

## Remove first

- Chatbot residue: "当然可以", "希望这对你有帮助", "如果你愿意我还可以".
- Empty framing: "下面我将", "首先我们需要明确", "值得注意的是", "综上所述" when the sentence can start with the content.
- Unsupported importance: "关键", "核心", "显著", "深远", "robust", "crucial" without a named impact or measurement.
- Generic endings: "未来可期", "这将为后续奠定基础", "这是迈向成功的重要一步".
- Decorative rhetoric: forced slogans, fake quotations, staged questions, punch-line fragments, and "不是 X，而是 Y" when Y can stand alone.
- Vague attribution: "数据显示", "研究表明", "业内认为" without a source.
- Fabricated precision: plausible-looking percentages, latency, throughput, dates, line numbers, or severity that do not come from evidence.

## Rewrite when clustered

- Repeated 总分总 structure or a mechanical 首先/其次/最后 sequence.
- Forced groups of three and identical sentence rhythm.
- Abstract actors such as "系统赋能", "技术驱动", "市场选择" when a real component, team, or process performed the action.
- Nominalized action such as "进行优化", "实现提升", "提供保障" when a direct verb is clearer.
- Every bullet using a bold label, every paragraph ending with a one-line conclusion, or every section restating its heading.
- Long background before the decision, symptom, finding, or requested answer.
- Synonym cycling for the same component. Use its stable technical name.

## Keep when precision needs it

- Passive voice when the actor is unknown, irrelevant, or intentionally abstracted by a protocol.
- Adverbs that change technical meaning, including ordering, timing, probability, or scope.
- Repeated terms when consistency is safer than stylistic variation.
- Necessary caveats, security and legal notices, protocol wording, and explicit uncertainty.
- Common transitions, punctuation, or one short emphasis sentence used naturally.

## Evidence boundary

For each important claim, ask what the cited code, log, metric, experiment, or document actually establishes. Narrow the claim instead of strengthening the wording. Missing evidence should become a stated unknown or a verification step, not a confident explanation.
