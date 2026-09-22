# 确定性混合检索与证据回读

证据包回答「文档里有什么」，释义层回答「这段材料在说什么」，检索层回答**「这个问题该读哪些单元、按什么顺序，以及什么不许被藏起来」**。

做法不是排序一个分数列表，而是：保留原查询、只做确定性的字典扩展，用命名空间隔离的词法通道找候选，把每个候选**回读当前索引**并带上稳定定位，再把同一 scoped claim 上互相矛盾的两侧结构化成冲突组。没有向量模型时它是完整的检索底座，而不是残缺版本。

三条不可让渡的前提：

- **原查询永不改写，扩展必须可解释**：命中记录里保留查询时用的那个词、通道、排名和原始分数，扩展来自哪里（记法规则或目录别名）逐条写明。
- **排名只决定读的顺序，不决定谁是对的**：排序不会改变任何 Evidence Status，也不会让高分证据遮住同一 claim 的另一个值。
- **冲突与降级不隐藏**：能力缺失时显式降级并说清哪条通道没跑；两侧矛盾时答案是 `ambiguous` 或一个 `winner=null` 的 Conflict Group，绝不挑赢家。

## Retrieval Unit 与命名空间

检索以 Retrieval Unit 为单位（正文陈述 `evidence:<id>`、图片单元 `image:<id>`），并按命名空间隔离，避免用图片转写或候选读法回答文档事实：

| 命名空间 | 内容 | 可以作为项目事实 |
|---|---|---|
| `source_facts` | 文档正文陈述 | 是 |
| `image_transcription` | 图片区域转写（须过质量门槛） | 是（machine-supported） |
| `visual_interpretation` | 视觉解读（阅读顺序、关系） | 是（verified 才成立） |
| `explanation` | 分层释义，只补匹配理由 | 否 |
| `unconfirmed_candidates` | 未确认候选读法 | 否，仅探索模式 |

`response.namespaces` 逐个报告每个命名空间本次是否被服务、返回几条、是否属于事实命名空间；`untraceable_candidates` 单独一项报告回读失败的候选。`explanation` 命名空间的状态固定是 `assist_only`（`creates_candidates=false`）：它只给已经找到的候选补一条匹配理由，不会新增候选。

图片转写只有在通过自己的质量门槛（`quality=accepted` 且 `machine-supported`）后才进入事实查询，未过门槛的转写不作为事实命中。

## 查询计划：原文 + 确定性扩展

`retrieve` 从不高改写原查询。`expansions` 只来自两处确定性来源：

1. **已确认记法字典**：先按 `scope_covers` 判断条目范围是否覆盖本次查询的文档 / 类型，命中的条目双向扩展（记号 ↔ 含义）。
2. **玩法目录别名**：`knowledge/catalog.json` 里 `confirmed` 的 canonical 名与别名双向扩展。

两类扩展共用上限 `MAX_ALIAS_EXPANSIONS=8`，`confirmed_alias.expansions` 报告实际用了几个，`response_meta.expansions` 报告总数。范围不覆盖的字典条目不会扩展，也不会因此漏掉原查询本身的命中。

## 通道

通道按 `CHANNELS` 固定顺序报告（冲突扫描最后跑，因此也最后报）：

| 通道 | 做法 | 说明 |
|---|---|---|
| `exact` | `TRIM(text)` 大小写不敏感全等 | 精确命中优先于其它通道；命中的是别名变体时同样记 `exact` |
| `lexical` | SQLite FTS5（bm25） | 短于 3 个字符的查询若 FTS5 无命中，退回 `instr` 子串匹配 |
| `confirmed_alias` | 字典 / 目录扩展词 | 带 `rule` 写明是哪条确认产生了这次扩展 |
| `structures` | `structural_relations` + 两端区域原文 | 合成「A 之后是 B（kind）」，只描述已确认关系 |
| `unconfirmed` | 未确认候选 | 只在 `include_candidates=true` 时执行 |
| `semantic` | 向量召回 | 本构建 `not_configured` |
| `conflict_scan` | 已命中 claim 的另一侧 | 在 `CONFLICT_SCAN_LIMIT=200` 行内补回未命中的对侧证据 |

每条通道报告 `status`：`matched`、`no_match`、`not_configured`、`unavailable`、`skipped`、`failed`。个别命名空间缺失时该通道报 `unavailable` 并给出 `reason` 与已到达的范围（`reached`），其余命名空间照常返回命中，不会因为一层不可读而丢掉整个答案。

## 候选融合与排序

命中按 `unit_id` 融合成一个候选：同一来源的多路命中合并为一条，并逐条保留匹配理由（`channel`、`match_type`、`rank`、`raw_score`、`reason`，必要时 `rule` / `relation`）；只有同一通道、同一理由的重复命中才折叠成一条。

排序用确定性优先级 `MATCH_PRIORITY`：`exact` → `confirmed_alias` → `lexical` → `semantic_candidate`。

**不同通道的原始分数永不相加**：`score` 只是该候选最强的那一个单分。`matched_query` 一旦为 `false` 就保持 `false`，所以被冲突扫描补回的单元永远不会被说成查询命中。排序和分数都不改变 `evidence_status`。

## 证据回读与 Untraceable Candidate

候选在返回前会回读**当前**索引（Evidence Hydration）：核对 `recorded_sha256`、重新解析 locator、必要时重新读区域与关系。以下情况判为 Untraceable Candidate 而不是答案：

- 行已不存在（缓存命中但记录被删除 / 重建）；
- 来源文档内容哈希变化；
- locator 或区域不再可读；
- 引用的结构关系已不存在。

Untraceable 候选项带 `reason` / `detail`、`supports_project_fact=false` 和 `UNTRACEABLE_BOUNDARY`，并且**不计入** `evidence`。

## 冲突保护

冲突判定按 **scoped claim** 分组：同一 `section_path` + 同一 `unit_type` + 同一 `claim_topic`（把具体数值去掉后的主题）。

维度取 `CONFLICT_DIMENSIONS` 的子集：`value`、`unit`、`version`、`time`（`scope` 是 claim 的身份而不是差异维度，因此逐侧发布在 `sides[].scope` 上）。

| 情形 | 输出 |
|---|---|
| 签名不同且跨文档 | `conflict_groups`：`winner=null`、`resolution_state=unresolved` |
| 签名不同但同文档 | `potential_conflict_candidate` |
| 签名相同、文本不同 | `potential_conflict_candidate` |
| 完全一致 | 什么都不报（互相印证） |

每个 Conflict Group 的 `sides` 带 `unit_id`、`evidence_id`、`source_document`、`locator`、`display_locator`、原文、`value` / `unit` / `version` / `time`、`evidence_status`、`scope`、`retrieved` 和 `source_reference`；`retrieved=false` 就是被冲突扫描补回、查询本身没命中的那一侧。

**冲突扫描**（`conflict_scan`）在查询命中之后运行：它按同一 `section`、同一 `unit_type`、同一 `claim_topic` 读回另一侧证据，所以高排名的句子不能藏住同一 scoped rule 的另一个值。扫描范围受 `CONFLICT_SCAN_LIMIT` 限制，永远不能变成第二次全库搜索；补回的单元 `matched_query=false`，也不会产生 `preferred_evidence_id` 这类赢家字段。

## 响应状态

`status` 取 `RESPONSE_STATES` 中的一个，按严重程度从高到低判定：

| 状态 | 条件 |
|---|---|
| `degraded` | 有通道 `unavailable`/`failed`，或调用方报告了 `degradations`，或显式请求 `hybrid`/`semantic` 而没有向量能力 |
| `not_found` | 所有通道都没有合格候选（在降级之外，本构建不强行输出弱相关答案） |
| `ambiguous` | 存在 Conflict Group |
| `partial` | 有合格事实，但部分候选项 Untraceable |
| `found` | 有可回读的事实候选且无以上情况 |
| `failed` | 检索本身抛错，返回 `reason` / `detail` 与降级边界 |

**降级优先于 `not_found`**：有通道没跑时，「没找到」不是一个可以下的结论，因为缺的那条通道并没有被排除。

## 探索模式

默认事实查询不使用未确认候选，被挡住的候选计入 `retrieval.held_back_unconfirmed` 并写一条 limitation。调用方显式传 `include_candidates=true` 时执行 `unconfirmed` 通道，候选照常返回并带 `EXPLORATION_BOUNDARY`，但永远不算项目事实（`supports_project_fact=false`）。

## 降级与可观察性

`mode` 分 `requested` 与 `effective` 两个字段。`hybrid` / `semantic` 在本构建降级为 `auto`，并产生 `degradation_events`（`channel=semantic`、`reason=vector_capability_not_configured`、`requested_mode` / `effective_mode`）。`auto` 从不要求本构建没有的能力，因此它不因此降级。

| 字段 | 含义 |
|---|---|
| `mode.requested` / `mode.effective` | 请求的模式与实际跑的模式 |
| `response_meta.channels` | 每条通道最终状态的一览 |
| `response_meta.vector` | `status` / `provider` / `model` / `index_version` / `namespace`（本构建为 `not_configured` 且后三项为 `null`） |
| `response_meta.degradation_events` | 逐条降级事件及其原因 |
| `response_meta.rebuild_vector_index_recommended` | 仅当降级事件来自 `semantic` 时为 `true` |
| `response_meta.explanation_assist` | 释义补理由的 `status` / `examined` / `assisted` / `creates_candidates=false` |
| `limitations` | 逐条可读的限制说明（含被挡住的未确认候选、不可用能力） |

调用方传入的 `degradations`（例如确认记法字典不可读）会记进 `retrieval.unavailable_capabilities` 并让状态变为 `degraded`，核心 FTS5 与文档事实不受影响。

## MCP 工具

```text
retrieve_evidence(query, document_type=None, evidence_type=None, limit=20,
                  mode="auto", include_candidates=False, document="")
```

`limit` 上限 `MAX_LIMIT=100`，默认 20。空 `query`、未知 `mode`、越界 `limit` 在任何查找之前就被拒绝。响应顶层字段：

| 字段 | 含义 |
|---|---|
| `status` / `schema_version` | 响应状态与检索契约版本（`retrieval-v1`） |
| `query` / `expansions` | 原查询与本次用到的扩展 |
| `mode` / `namespaces` / `channels` | 实际模式、逐命名空间与逐通道报告 |
| `candidates` | 融合后的全部候选（含未确认候选） |
| `evidence` | 事实候选的 V1 形状视图（`source_document`、`evidence_type`、`text`、`section_path`、`locator`、`score`、`unit_id`、`match_type`、`evidence_status`、`channels`、`v2` 等） |
| `untraceable` | 回读失败的候选 |
| `conflicts` / `conflict_groups` / `conflict_dimensions` | Potential Conflict Candidate、Conflict Group 与维度表 |
| `retrieval` | 本次的 limit、过滤、探索开关、`held_back_unconfirmed`、候选计数与融合边界 |
| `limitations` / `boundary` | 可读的限制说明与证据边界 |

## 与 V1 `search_evidence` 的关系

- `search_evidence` 是**冻结的 V1 词法检索**，签名与默认响应一字未改；`retrieve_evidence` 是 V2 面，两个工具同时存在。
- 两者都返回同一次读取对应的 `index_status`；V2 的 `status` 取值是 `found`、`not_found`、`partial`、`ambiguous`、`degraded`、`failed`。
- 检索层不是第二票证据：它只挑读什么、按什么顺序读，不改变任何 Evidence Status、不产生赢家字段，也不把机器转写提升为 `verified`。
