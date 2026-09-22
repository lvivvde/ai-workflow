# 设计者记法字典与可审计人工审核

策划文档里到处是速记：流程图里的箭头、圈住一组分支的花括号、只在本图角落成立的图例、只在这张表里成立的表头。读懂这些速记是**人对项目做的判断**，所以它写进 Durable Project State，和已确认别名放在一起，既不写进推导索引，也不改源文档。

本模块有三条不可让渡的前提：

- 已确认的含义一定带范围：一张图的某一个区域、一个文档、一个文档类型，或整个项目。范围之外不会被「顺手继承」。
- 索引只是**提出**一个读法，那是 candidate。候选永远不进字典，也不能作为 Project Fact 被引用。
- 这里不改任何源证据。修正含义是**追加**新含义，旧值、旧审核事件、原始转录全部留在原处。

## 记法条目与范围

条目（`notation.json` 里的 `entries`）记录 `notation_token`（被解释的记号）与 `meaning`（确认的含义），并带以下范围与时效字段：

| 字段 | 说明 |
|---|---|
| `scope.kind` | `project` / `document_type` / `document` / `region` |
| `scope.value` | `project` 固定为 `*`；`document_type` 为类型名（如 `xlsx`）；`document` 为项目相对路径；`region` 为 `<项目相对路径>#<区域标签>` |
| `version` | `parse_revision_id` / `source_revision_id` / `document`：这条确认是在哪个修订上做出的 |
| `valid_from` / `valid_until` | 可选时效窗口，超出窗口的条目不再回答问题 |
| `authority` | 这条确认说话的层级（见下） |
| `status` | `confirmed` / `rejected` / `superseded` |
| `basis` | 依据（候选 ID、原因、来源引用） |
| `confirmed_by` / `confirmed_at` | 谁在什么时候确认的 |
| `supersedes` / `superseded_by` / `resolution` | 修正链与冲突裁决记录 |

范围只做**字面匹配**：为某个区域确认的含义不会回答同一文档另一个区域、另一个文档、或另一个文档类型的问题。路径必须是项目相对路径，绝对路径、盘符、`..` 一律拒绝。

## 字典文件与审核日志

```text
.design-state/notation.json                      # 已确认含义的物化视图
.design-state/journal/review_events.jsonl        # append-only 审核事件
```

每次写操作都先写审核事件、再写物化视图。这样进程中断只会留下「有记录但尚未落到视图」的状态，可审计；反过来（视图改了却没有记录）不会发生。撤销某次决定是**追加**一个新事件，而不是改历史。

## 来源优先级

回答「这个记号在这里是什么意思」时，层级由高到低：

1. `region_legend`——本图图例 / 本区域确认
2. `document_definition`——本文档定义
3. `document_type_definition`——本类型定义
4. `project_dictionary`——版本匹配的项目字典
5. `candidate_interpretation`——候选解释（只作候选报告，永不算 Project Fact）
6. `external_common_knowledge`——隔离的外部常识（只并排展示，绝不与项目读法合并）

回答只由**最高且持有生效含义的层**决定；更低层可以不同意，这会被报成 `conflicts`。只要存在分歧，答案就停在 `ambiguous`：`resolved` 为空、`supports_project_fact=false`，并给出 `authority_winner`（按优先级本该胜出的那一条）。要把它落定，必须用 `resolve_conflict` 记录一次裁决。

## 候选与项目事实

候选来自索引：只有布局引擎自己标为 `candidate` 的结构关系才会出现在这里（规则集能确认的关系是机器支持的结构，不算有争议的读法，走证据包即可）。

每个候选自带 `claim_boundary`、几何依据、不确定性代码，并明确写 `supports_project_fact=false`、`requires_review_action=true`。被拒绝（`reject`）或被忽略（`ignore`）的候选仍会列出，只是带上 `rejected` / `ignored` 标记：

- `reject` 说「这个读法是错的」，之后不再参与回答；
- `ignore` 说「别再问我同一件事」，同样不进字典。

两者都只写审核日志，不动字典。

## 版本迁移候选

一条确认是在某个 Parse Revision 上做出的，就不会自己跟到下一个修订上。新修订成为 active 之后：

- 旧绑定条目不再参与回答，`migration_candidates` 列出它（`from_parse_revision`、`to_parse_revision`、`requires_review_event=true`、`auto_applied=false`）；
- 要让同一个读法在新修订上生效，必须再来一次**显式 Review Action**；重新确认时旧绑定会被标为 `superseded`，迁移候选随之清空。

## Review Action 与预览 token

五个动作：

| 动作 | 作用 | 写入 |
|---|---|---|
| `confirm` | 把某个范围里的记号含义确认为 `confirmed` | `notation.json` + 日志 |
| `correct` | 修正已有条目的含义，旧条目转 `superseded` | `notation.json` + 日志 |
| `reject` | 否决条目或候选读法 | 条目：`notation.json` + 日志；候选：仅日志 |
| `ignore` | 承认候选存在但不再提示，不进字典 | 仅日志 |
| `resolve_conflict` | 记录冲突由哪个来源胜出，或由人选定 | `notation.json` + 日志 |

流程固定为两步：

1. `plan_review_action(...)` 返回 `status=confirmation_required`、`preview`（before / after / changes）、`writes`、`affected`、`propagates_beyond_scope=false`、`derived_knowledge_index_modified=false` 和 `plan_token`；
2. `apply_review_action(..., plan_token=..., confirmed=true)` 才真正写入。

token 覆盖这次决定的内容（动作、范围、含义、层级、被替代者、理由、执行人，以及写入前的字典状态摘要），**不覆盖时钟**：条目标识在应用时才生成，记录时间也以应用时刻为准。预览之后字典一旦变化，token 不再匹配，应用会被拒绝并要求重新预览。写操作没有预览 token 一律不生效。

## 冲突解决

`resolve_conflict` 记录两种裁决：

- `authority`：胜出者层级严格高于所有异议者，记录的是「按来源权威判定」；
- `human_choice`：异议者与胜出者同级（或人故意选择低层级读法），记录的是人工选择。

把低层级读法强行标成 `authority` 会被拒绝。裁决记录 `winner_entry_id`、`resolved_entry_ids`、`loser_authorities`、理由与执行人，异议条目转 `superseded` 并保留在历史里；**Source Evidence 与推导索引都不参与**，也不会被改写。

## MCP 工具

```text
notation_dictionary(document="", include_history=False, limit=200)
resolve_notation(notation_token, document, document_type="", region="", parse_revision_id="", external_common_knowledge="")
plan_review_action(action, notation_token="", meaning="", scope_kind="", scope_value="", document="", document_type="", region="", entry_id="", authority="", parse_revision_id="", valid_from="", valid_until="", resolution_kind="", candidate_id="", reason="", actor="", note="", basis=None)
apply_review_action(action, plan_token="", confirmed=False, ...)
review_history(subject_type="", subject_id="", action="", limit=200)
```

`notation_dictionary` 把已确认条目与候选、迁移候选分开放；`resolve_notation` 回答单个记号；`review_history` 返回 append-only 事件（含 before/after）。

两个读工具的响应都带 `index_status`：已确认含义是人的决定，但候选读法来自索引，回答又发生在同一次运行里，所以答案自己带上该索引的计数（`documents_indexed`、`images_indexed`、`ocr_failed`、`ocr_unavailable`、`stale_documents`、`is_stale` 等），缺 OCR 引擎这类降级不会只出现在运行报告里。字段与 V1 工具的 `index_status` 同源；索引不可读时该字段为 `null`，响应本身照常返回。

## 拒绝与回滚不会损坏推导索引

否决一条读法、把否决用后续事件推翻、或重新确认，都只写 `.design-state/`。测试会用字节比对证明 `knowledge.sqlite` 与源文件在整串动作前后完全一致，并且索引仍可读、仍持有同样的图片与结构关系。

## 与 V1 的兼容

- 新增的都是**新工具**，V1 工具签名与默认响应不变；
- `index_status` 的 V1 字段不变，记法统计只出现在 `DurableState.status()`（`notation_entries` / `notation_confirmed` / `notation_superseded` / `notation_rejected`）；
- 未确认候选在任何响应里都不被当作已确认事实。
