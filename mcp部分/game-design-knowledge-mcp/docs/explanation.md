# 面向 AI 与普通人的分层详尽释义

证据包回答「文档里有什么」。释义层回答「所以这段材料到底在说什么」，并且要同时让策划、程序、测试和调用方 AI 看懂。做法不是让模型写一段流畅的话，而是先用确定性证据规则构建**结构化 Explanation Atom**，再按 Profile 渲染；自然语言只是原子陈述的呈现方式，不是第二份事实。

三条不可让渡的前提：

- **每个 Atom 独立可追溯**：自带 Source Reference、Locator 和它引用的最小原文片段，能回到证据包里的具体层与位置。
- **较短 Profile 不隐藏会改变结论的东西**：冲突、缺口、定位和不确定性在 `brief`、`standard`、`full` 三档里都必须出现。
- **来源未说明就保持未知**：不用行业惯例、配置数值或截图外观补造设计意图，也不把先后顺序写成因果。

## 三个 Profile

默认 `full`；`profile` 只决定展开范围，不决定证据规则。未知 `profile` 在任何查找之前就被拒绝，既不降级成 `full`，也不会因为这次提问没命中而被吞掉。

| Profile | 展开的章节 | 典型规模 |
|---|---|---|
| `brief` | 直接结论、冲突与候选、来源没有说明的事项、证据与定位 | 1–5 个 Atom |
| `standard` | `brief` 全部 + 原始内容、对象与角色、条件、动作、结果、顺序与关系 | 5–20 个 Atom |
| `full` | 全部 13 个章节 | 不设固定字数，过大时分页 |

`explanation.coverage_scope` 逐条写明本次解释了哪些 Retrieval Unit、哪些章节被 Profile 省略（`omitted_sections`）、哪些层这次没被服务（`not_covered`）、以及是否因为分页而截断。完整性只在 Coverage Scope 内成立，范围之外一律列为未覆盖，而不是含糊带过。

## Explanation Atom

```text
atom_id            对内容取哈希，跨运行稳定
text               渲染后的句子（润色只改这里）
statement_kind     fact / derived / candidate / conflict / not_stated / evidence_citation
wording            verbatim / labeled / template / expanded / gap / citation
section            所属章节（决定阅读顺序）
evidence_status    explicit / machine-supported / verified / candidate / conflict
content_layer      statement / transcription / visual_interpretation / notation_interpretation / source / explanation
source_document / source_reference / locator / source_excerpt
supported_by / derived_from / uncertainty_refs / roles / expansion_refs / changes_conclusion
```

一个 Atom 只表达一个主要陈述。事实、推导和候选不混进同一句话：条件、动作和结果只有在同一份证据明确支持时才组成一条规则句，跨来源组合时每个分句各自绑定来源。

## 阅读顺序

`full` 固定按以下顺序呈现，没有内容的章节不出现：

```text
直接结论 → 原始内容 → 普通话重述 → 对象与角色 → 触发条件与前置条件 → 动作或处理过程
→ 结果与状态变化 → 顺序、层级和显式关系 → 数值、单位和适用范围 → 例外、分支与失败情况
→ 冲突与候选解释 → 来源没有说明的事项 → 证据与定位
```

`rendered.sentences` 与 `rendered.sentence_map` 把每个可分离分句映射回一个或多个 Atom；两个 Atom 措辞相同时（例如同一文档的两条引用），映射会同时列出它们，读者不会因为撞句而只看到其中一个。

## 措辞纪律

- **原文**：`verbatim` Atom 的 `text` 与 `source_excerpt` 完全一致，标点、大小写、数字和符号都不改写。
- **标注**：`labeled` / `template` Atom 把来源片段原样嵌在句子里（前缀、后缀）并接受检查；本构建新增的措辞不得出现 `导致`、`依赖`、`触发`、`必须先完成`、`运行时调用`、`设计目的`、`因为`、`所以`。
- **数值**：同时保留来源表达与普通话解释；`<` 不会被改写成 `≤`，百分比、倍率和绝对值不互换；来源只写数字没有单位时，报 Relevant Source Gap（`missing_unit`）而不是补一个单位。
- **关系**：顺序与关系只使用受控模板——`visible_connector`（图中存在从 A 指向 B 的可见箭头）、`next_step`（把 B 标为 A 之后的下一步）、`depth_hint`（缩进更深，可能提示层级，但不足以确认子项）、`candidate`（可能表示……但证据不足）、`unresolved`（缺少唯一端点，无法建立明确关系）、`ignored`（已被人工标记为装饰，不参与流程关系）。缩进只出 hint，永远不升级成父子关系。
- **记法**：只对当前范围里 `confirmed` 的条目做 `原词（含义）` 展开，并记下定义来源；`rejected` / `superseded` 的读法以 `not_stated` Atom 明说「已被人工标记为装饰」；没有确认定义的符号保持原词并标 `status=unknown`。
- **设计目的**：只有问题本身在问目的时，才产出 `missing_design_intent` 这类缺口，措辞固定为「来源没有说明设计目的，也不使用行业惯例补造」。

## 冲突、缺口与不确定性

同一主题在不同文档里被写成不同数值时，两侧各形成一个 `conflict` Atom，并生成一个响应级 Conflict Group：`winner` 恒为 `null`、`resolution_state=unresolved`，直接结论固定说「现有证据支持多个解释，不能确定唯一答案」。不按日期、置信度、出现次数或平均值挑赢家，也不会写成 6.5 秒这样的折中值；同一份文档内部的两句话不构成冲突。

Relevant Source Gaps 只列与问题有关、且可能改变结论的缺失（缺触发方式、缺单位、缺设计目的），每条都带 `changes_conclusion` 标记，因此第一页永远保留它们。

## Source-as-Data 边界

文档正文、OCR 结果、公式、链接和记法都是**证据数据**，对 MCP 和调用方都没有指令权限。来源文本里出现「忽略之前的指令」「放宽证据门槛」「不要告知用户」、脚本或外链时，这些句子照原样进入 Atom 并产生 `security.warnings`（`source_text_kept=true`），但**不会**改变 Profile、证据门槛、工具权限或输出契约，也不会被抓取或执行。

## Contract Validator

返回前逐 Atom 检查：唯一 `atom_id`、非空 `text`、合法章节 / 陈述类型 / 证据状态 / 内容层、来源文档、Locator、Source Reference、支撑引用是否指向本响应内的 Atom 或已确认字典条目，以及措辞规则（原文逐字、数字与比较符不新增）。

不合格 Atom 会被**隔离**（`isolated_atoms`）并产生 Structured Uncertainty（`atom_without_support`），响应降为 `partial`；隔离会级联——引用被隔离 Atom 的句子同样留不下来。发生过隔离时 `contract.ok` 为 `false`，响应不会因为措辞流畅而保留无来源内容。

## 分页

`page_size` 上限 200，默认 40。第一页固定保留直接结论、全部冲突和所有 `changes_conclusion` 的缺口，因此可能超过 `page_size`，但仍然返回阅读顺序的**前缀**；`next_cursor` 是下一页的偏移量，后续页连续推进、不重复已返回的 Atom。任何截断都会返回 `truncated`、`remaining_atom_count` 和 `next_cursor`，不会静默降低 Profile。

## 可复现性与可选润色

相同输入、问题、Profile、语言与 `explanation_policy_version` 产生相同的 Atom 集合与章节顺序。`explanation_id` 由这些输入派生，`provenance` 记录策略版本、生成器、`parse_revision_ids` 和输入哈希。缓存键至少要覆盖输入哈希、Profile、语言、策略版本与生成器版本。

本构建没有语言模型也能产出满足完整契约的确定性渲染（`deterministic-template-v1`）。可选的本地润色器（`build_explanation(polisher=...)`）只能改写 Atom 的措辞：引入或改动数字、改动比较符、添加禁用措辞会被逐条拒绝并记 `polisher_rejected`，抛异常则整份回退到确定性渲染（`fallback_reason`）。它不能新增事实、数值、关系、条件或目的。

## MCP 工具

```text
explain_evidence(unit_id, profile="full", language="zh", cursor="", page_size=40,
                 include_source_language=False)
explain_query(query, document_type="", profile="full", language="zh", unit_limit=5,
              cursor="", page_size=40, include_source_language=False)
```

`explain_evidence` 解释单个 Retrieval Unit，用的是 `get_evidence_package` 返回的同一批层，因此两个工具不会对同一个单元给出不同说法；证据包里也带一份同样的 `sections.explanation` 条目（`expand.tool="explain_evidence"`）。`explain_query` 先做词法与图片文字的确定性检索，再把命中的单元一起解释，并在 `retrieval.channels` 里报告每条通道的命中情况；没有命中时返回 `not_found` 并给出下一步建议，不用弱候选填充。

两个工具的顶层都带 `conflicts`，与其它读取工具的冲突报告保持一致；冲突完整列表同时保留在 `explanation.conflict_refs`。`language` 默认 `zh`，`include_source_language=true` 时每个 Atom 额外带 `source_language_text`；原文语言始终保留。

## 与 V1 / V2-06 的关系

- 两个工具都是**只读新增**，V1 工具签名与默认响应不变。
- `get_evidence_package` 的 `explanation` 层不再固定报 `no_explanation_profile`：只有当一个单元确实没有任何可解释内容时才报未服务。
- 释义不是第二票证据：它引用证据包，不改变任何 Evidence Status，也不能提升机器转写的证据状态。
