# 正式版规格：全文与配置事实查询

版本目标：`0.2`。

## 目标

系统保留图片索引能力，并将 DOCX 正文和 XLSX 单元格转换为可搜索、可定位的文档证据，使 MCP 客户端能够回答“是否存在某玩法”“文档明确记录了哪些规则”“某个配置字段和值是什么”。

所有回答继续受 [`evidence-policy.md`](evidence-policy.md) 约束：只使用文档事实，不自动联想玩法、不推断设计意图、不创建未经确认的别名。

## 范围

当前正式版支持：

- DOCX 正文标题、段落、列表、表格及现有图片锚点。
- XLSX 工作表、单元格原始值、公式、类型、合并范围及现有图片锚点。
- DOCX/XLSX 统一证据模型。
- SQLite FTS5 中文全文检索。
- 正式玩法目录和人工确认别名。
- MCP 结构化查询结果：`found`、`not_found`、`ambiguous`、`stale`。
- 原子发布、过期检测和后续按文件增量更新。

当前正式版不支持：

- PDF、PPTX全文解析。
- 模型语义联想和向量数据库。
- 自动生成或自动写入玩法别名。
- 根据图片、配置值或行业惯例推断设计意图。
- 自动修改源文档。
- Word文本框、SmartArt、脚注、尾注和批注的完整语义还原。

## 公开接缝

### CLI

```text
game-design-knowledge index <source> --output <index> [--project-root <project>]
                     [--low-memory] [--idle-timeout <seconds>]
game-design-knowledge capabilities
game-design-knowledge capability status [--profile <profile>] [--model-root <dir>]
game-design-knowledge capability doctor [--bundle <dir>] [--pack <pack>]... [--model-root <dir>]
game-design-knowledge capability plan --bundle <dir> --pack <pack> [--model-root <dir>]
game-design-knowledge capability install --bundle <dir> --pack <pack> [--model-root <dir>]
                     [--confirm] [--apply-python]
game-design-knowledge capability verify --bundle <dir> [--pack <pack>]...
game-design-knowledge capability uninstall --pack <pack> [--model-root <dir>] [--confirm]
game-design-knowledge capability manifests [--write <dir>]
game-design-knowledge capability baseline [--profile <profile>] [--model-root <dir>]
                     [--index-dir <index>] [--query <text>] [--output <file.jsonl>]
game-design-knowledge migrate --database <index.sqlite> [--plan]
```

`index` 现在是 8 个 stage 的流水线（见 [`processing.md`](processing.md)）：每次都写入不可变 Stage Attempt 与运行清单，可选能力缺失时明确降级、必选 stage 失败时退出码 1 且不发布索引。`--low-memory` 让能力包在批次结束时卸载，`--idle-timeout` 设置空闲驻留上限。`capabilities` 打印硬件画像、三类能力包的检查明细与当前驻留情况。

`capability` 子命令覆盖一台没有网络的机器上的整套能力包生命周期（见 [`capabilities.md`](capabilities.md)）：`status` 报告包、画像与驻留，`doctor` 在安装前逐项说明本机的磁盘、路径与 Tesseract 语言包结论，`plan` / `install` 只从本地 bundle 取字节（`--no-index --find-links --only-binary :all: --require-hashes`，没有任何下载路径），`verify` 逐字节核对 bundle 与本 build 的 pin，`uninstall` 只删模型文件并保留事实、证据与词法索引（`requires_reindex=false`），`manifests` 打印或刷新随包发布的清单，`baseline` 跑一次实测并写出含延迟、峰值内存、磁盘与降级事件的运行记录。没有 `--confirm` 的 `install` / `uninstall` 只返回预览。

CLI 写入同级不可变快照，校验通过后才发布；失败、中断或文件占用都不会替换已有索引。

发布成功后，CLI 把本次构建的来源、解析修订与检索单元登记到项目持久状态。项目根默认从 `<root>/.index/<name>` 这种输出布局推断，也可以用 `--project-root` 显式指定；两者都给不出项目根时，索引仍然可用，只是不会被登记为项目持久状态。

旧 schema 的索引不会被静默误读，必须用 `migrate` 显式迁移（先 `--plan` 预览，迁移前自动备份，失败还原）。

### MCP

保留：

```text
search_images
get_image_context
index_status
```

新增：

```text
search_evidence(query, document_type=None, evidence_type=None, limit=20,
                include_v2_metadata=False)
get_evidence(evidence_id, context_before=1, context_after=1)
find_feature(name)
get_feature_evidence(name, include_documents=True, include_configs=True, include_images=True)
search_config_cells(query, workbook=None, sheet=None, limit=50)
get_sheet_range(workbook, sheet, range)
plan_document_import(source_paths, destination="docs", operation="copy")
import_documents(source_paths, plan_token, destination="docs", operation="copy", confirmed=False)
rebuild_shared_index(confirmed=False)
capability_status()
```

证据包与资产：

```text
get_evidence_package(unit_id, sections=None, limit=20, cursor="")
get_evidence_packages(unit_ids, sections=None, limit=20)
get_asset(asset_reference, include_content=False)
get_processing_manifest()
```

`get_evidence_package` 以 `evidence:<id>` / `image:<id>` 为锚点，把 source、statement、transcription、visual_interpretation、notation、explanation、uncertainties 分层返回，并整份带上 provenance 与逐层 unavailable；`get_asset` 只接受索引签发的 Asset Reference，不接受任何文件路径；`get_processing_manifest` 返回运行清单、逐 stage 尝试与 degradation 事实。四个工具都是只读新增，V1 工具在未显式请求 V2 元数据时字段与默认行为不变，详见 [`evidence-package.md`](evidence-package.md)。

分层详尽释义：

```text
explain_evidence(unit_id, profile="full", language="zh", cursor="", page_size=40,
                 include_source_language=False)
explain_query(query, document_type="", profile="full", language="zh", unit_limit=5,
              cursor="", page_size=40, include_source_language=False)
```

两个工具先用确定性证据规则构建结构化 Explanation Atom，再渲染 Brief/Standard/Full（默认 `full`）；`explain_evidence` 解释单个 Retrieval Unit 用的是 `get_evidence_package` 的同一批层，`explain_query` 把一个问题的命中单元一起解释并在 `retrieval.channels` 报告各通道命中。每个 Atom 自带 Source Reference、Locator 与最小原文片段，冲突双方分别成 Atom 且顶层 `conflicts` 完整暴露，`brief` 也不会省略会改变结论的冲突、缺口和定位；没有本地语言模型时模板渲染仍满足完整契约。详见 [`explanation.md`](explanation.md)。

V2 混合检索与证据回读：

```text
retrieve_evidence(query, document_type=None, evidence_type=None, limit=20,
                  mode="auto", include_candidates=False, document="")
```

`retrieve_evidence` 是 V2 检索面：保留原查询，只用已确认记法字典与玩法别名做确定性扩展，再走命名空间隔离的 `exact`、`lexical`（FTS5）、`confirmed_alias`、`structures` 等通道。每个候选在返回前回读当前索引并带稳定 locator，无法回读的命中只作为 Untraceable Candidate；同一 scoped claim 的多侧证据结构化为 Conflict Group（`winner=null`）或 Potential Conflict Candidate，冲突扫描会补回未被查询命中的对侧，高排名不掩盖不同的值、单位、版本或时间。`mode` 取 `lexical`、`auto`（默认）、`hybrid`、`semantic`；没有向量能力时显式降级为 `auto` 并报告 `degradation_events`，字典或命名空间不可用时给出显式 degradation 而核心 FTS5 继续工作。`status` 为 `found`、`not_found`、`partial`、`ambiguous`、`degraded`、`failed` 之一；`search_evidence` 保持冻结的 V1 词法检索不变。详见 [`retrieval.md`](retrieval.md)。

可选本地向量召回与查询改写（V2-10，实验开关，默认关闭）：

```text
semantic_index_status()
rebuild_semantic_index(confirmed=False)
drop_semantic_index(confirmed=False)
```

`GAME_DESIGN_EMBEDDING_PROVIDER` 为空时不加载任何 embedding 模型，检索行为与确定性底座一致；设为 `hashing`/`hashing:<dim>` 使用内置参考实现，设为 `module:attribute` 导入本地 provider。文档向量只在显式 `rebuild_semantic_index(confirmed=true)` 时写入可删除的侧车 `<index_dir>/semantic.sqlite`（`semantic-v1`，导入流程不写向量），查询时只生成查询向量；向量记录绑定稳定 `unit_id`、来源哈希与模型三元组（`model_id`/`model_version`/`dimension`），不同模型或维度的向量绝不混搜。`auto` 只在确定性通道没有事实命中时才请求向量，`hybrid` 缺能力时降级为 `degraded` 并继续用事实与 FTS5 回答，`semantic` 只跑向量通道并把确定性通道标为 `skipped`。命中记为 `semantic_candidate` 且必须回读当前事实，相似度不与 BM25 相加，低于阈值的命中只进 `possible_related`。`GAME_DESIGN_QUERY_REWRITER` 未配置时不改写查询；配置后每个变体都要通过守门（不得改变数值、单位、版本、时间、否定或范围），且永不更新 Designer Notation Dictionary。V2 首发不实现原始图片 embedding。

策划记法字典与人工审核：

```text
notation_dictionary(document="", include_history=False, limit=200)
resolve_notation(notation_token, document, document_type="", region="",
                 parse_revision_id="", external_common_knowledge="")
plan_review_action(action, notation_token="", meaning="", scope_kind="",
                   scope_value="", document="", document_type="", region="",
                   entry_id="", authority="", parse_revision_id="", ...)
apply_review_action(action, plan_token="", confirmed=False, ...)
review_history(subject_type="", subject_id="", action="", limit=200)
```

`notation_dictionary` 与 `resolve_notation` 只读：前者把已确认条目、候选与迁移候选分开放，后者回答单个记号在某个范围内的含义。已确认含义写入 Durable Project State，既不进推导索引也不改源文档；索引提出的读法一律是 candidate，不参与回答、也不能被引用为项目事实。确认按 `project` / `document_type` / `document` / `region` 四档**字面**范围生效，范围外不继承；新 Parse Revision 只产生 migration candidate，必须重新做一次显式审核。

`plan_review_action`、`apply_review_action`、`review_history` 是写侧：每个写入动作先返回 preview 与 `plan_token`，只有带同一 token 且 `confirmed=True` 才应用；token 覆盖这次决定的内容与写入前的字典摘要，字典一变即失效。每次应用先写 append-only Review Event、再物化视图，记录操作者、时间、范围、前后值、理由与依据；拒绝、忽略候选只写日志。五个动作为 `confirm`、`correct`、`reject`、`ignore`、`resolve_conflict`。详见 [`notation.md`](notation.md)。

三个写入工具遵循 [`import-policy.md`](import-policy.md)：预览不写入；导入必须携带未失效的计划令牌和明确确认；目标目录固定且禁止覆盖；建库失败时恢复文件并保留旧索引。

`capability_status` 只读，报告本地能力包与本机资源，不安装、不下载任何东西。

## 统一查询结果

所有读取工具都返回同一次 shared-index 读取对应的 `index_status`。当 `index_status.is_stale` 为 `true` 时，顶层 `status` 统一为 `stale`。其余字段仍保留原本的命中、空结果或歧义信息，调用方不得把过期索引当作当前事实。

```json
{
  "status": "found",
  "query": "幸运转盘",
  "match_type": "exact",
  "evidence": [],
  "limitations": [],
  "conflicts": [],
  "index_status": {
    "is_stale": false
  }
}
```

未找到时必须返回：

```json
{
  "status": "not_found",
  "query": "大风车",
  "match_type": null,
  "evidence": [],
  "limitations": ["当前文档及已确认别名中未找到该名称"],
  "index_status": {
    "is_stale": false
  }
}
```

不得附加相似玩法候选。

## 证据定位

DOCX 证据至少包含：

```text
source_document
section_path
block_type
block_ordinal
table/row/cell（适用时）
```

XLSX 证据至少包含：

```text
source_document
sheet_name
cell_reference
raw_value
formula（适用时）
```

## 人工确认目录

正式玩法和别名保存在：

```text
knowledge/catalog.json
```

只有该文件明确列出的正式名称、ID 和确认别名可以被 `find_feature` 解释为同一玩法。MCP 不提供自动写入目录的工具。

## 验收查询

真实语料验收至少包含：

1. 搜索样例中明确存在的DOCX标题，返回对应块和原文位置。
2. 搜索样例 XLSX 中的明确单元格文字，返回工作簿、工作表和 A1 地址。
3. 查询不存在的“大风车”，返回 `not_found`，不映射到其他玩法。
4. `get_evidence` 返回命中块及相邻块，不越过文档边界。
5. `index_status` 显示 6 份样例文档未过期。
6. 样例中的 13 张图片仍可搜索和定位。
7. 未确认的导入只返回计划，不移动、复制或重建文件。
8. 确认导入后按扩展名分类并原子重建；损坏文件触发回滚。
9. 第三方客户端启动后可以发现三个导入/重建工具。

## 完成标准

- 新旧测试全部通过。
- 6 份真实样例可原子建库。
- 所有证据都有可追踪定位。
- 不存在未经确认的别名联想。
- SQLite Schema 版本为 2。
- 构建失败不覆盖旧索引。
- 导入失败不遗留本次文件操作，且禁止覆盖同名资料。
- README 和新电脑搭建指南与实现一致。
