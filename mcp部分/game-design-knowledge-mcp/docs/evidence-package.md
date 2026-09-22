# 分层证据包、图片资产访问与独立图片导入

V2 在保持 V1 工具默认行为不变的前提下，增加四个只读工具和一套版本化引用。它们共同的前提是：**任何派生项都必须能回到原始字节与原始区域**，并且**每一层都自带它的事实边界**。

## 检索单元

证据包以 Retrieval Unit 为锚点：

```text
evidence:<evidence_id>   一条可定位的正文事实（段落、标题、列表项、表格单元格）
image:<image_id>         一张图片（内嵌图片或独立图片）
```

`unit_id` 只接受这两种前缀加正整数。路径、表名、SQL 片段一律拒绝，工具不会把参数拼进查询。

## 证据包的分层

`get_evidence_package(unit_id, sections=None, limit=20, cursor="")` 返回：

| 层 | 内容 |
|---|---|
| `source` | 版本化 Source Reference（逻辑文档 ID、Source Revision、Parse Revision、路径、locator、原始区域）与 Display Locator |
| `statement` | 该单元自身的原文、类型与 authority；图片不是陈述，此时该层明确报 `unit_is_not_a_statement` |
| `transcription` | OCR 运行摘要 + 逐区域转录（三个置信度、关键 token、规范化建议） |
| `visual_interpretation` | 阅读顺序运行摘要 + 逐 Visual Element（行、列、`depth_hint`） |
| `notation` | 逐 Structural Relation（端点、几何依据、规则版本、`claim_boundary`） |
| `explanation` | 该单元的 Brief/Standard/Full 释义（默认 `full`），带 `expand.tool=explain_evidence`；只有该单元确实没有可解释内容时才报 `no_explanation_profile` |
| `uncertainties` | 逐条不确定项；几何不确定性与 OCR 不确定性分开标注 |

`provenance` 与 `unavailable` 不参与分页，每次整份返回。前者说明这份包来自哪次构建（build / run、processing fingerprint、schema 版本、参与规则集、OCR 引擎），后者逐条说明哪一层为什么没有内容。两者的存在是为了让「这一层是空的」永远不同于「这一层没有被服务」。

### 事实边界

- 每个层的条目都带自己的 `source_reference`，并把 `region` 收窄到它实际来自的区域；不确定性条目描述的是「层」，因此带 `kind=uncertainty` 而不是来源引用。
- 转录带 `evidence_boundary`，关系带 `claim_boundary`：可见几何不等于因果、运行时依赖或作者意图。
- 没有内容的一层不会用空数组冒充「已查过且为空」。

## 分页

`limit` 上限 200，默认 20。分页在**一条按层顺序展开的扁平条目序列**上进行，因此：

- `page.cursor` / `page.next_cursor` 是稳定偏移量，`has_more=false` 时 `next_cursor` 为空串；
- `page.section_totals` 给出整份包每层的总条数，而不是本页条数；
- 任何一页里的条目都自带来源引用，页边界不会把一条陈述和它的来源拆开；
- `cursor` 只接受上一页返回的 `next_cursor`，其他值报错。

## 批量与部分成功

`get_evidence_packages(unit_ids, sections=None, limit=20)` 一次最多 50 个单元：

- 全部命中：`status=found`；
- 部分命中：`status=partial`，`packages` 保留所有成功的包，`unresolved` 逐条说明 `not_found` / `invalid` 及原因；
- 全部未命中：`status=not_found`。

任一单元的局部失败都不会丢弃已经取到的证据。

## 图片资产访问

`get_asset(asset_reference, include_content=False)`：

- 只接受本索引签发的 `asset-<32 位十六进制>` 引用；文件路径、相对路径、长度或字符不合法的输入一律 `not_found`；
- 引用由「索引目录名 + 索引内相对路径」推导，索引重建后仍然稳定，且无法为索引之外的文件凭空签出；
- `include_content=true` 时在 4 MiB 以内内联 base64，并同时返回读取到的 SHA256 与 `sha256_matches_index`，被改写过的资产可以被识别；
- 返回的 `asset.path` 一定落在索引目录内。

## 独立图片导入

`plan_document_import` / `import_documents` 现在接受 `.png` / `.jpg` / `.jpeg`，与 DOCX/XLSX 走同一条状态机：

- 预览阶段给出每个文件的 `source_type`（`document` 或 `standalone_image`）、目标路径与 SHA256；
- 计划条目落入 `docs/png/`、`docs/jpg/`、`docs/jpeg/`（或 `examples/sample-corpus/...`）；
- 未确认不落盘，同名目标一律拒绝覆盖；
- 确认后重建索引：独立图片登记为 `document_type=image` 的文档，`relationship_id=standalone`，资产写入同一个 `assets/`；
- 导入或建库失败时回滚本次文件操作，并保留旧索引与旧 `CURRENT.json`。

独立图片复用内嵌图片的证据与处理契约：OCR 降级链、区域转录、阅读顺序、结构关系、`get_asset` 全部一致，它不是「另一种东西」。

## 与 V1 的兼容

- `search_evidence` 等 V1 工具的字段与默认行为不变；`include_v2_metadata=false`（默认）时返回的条目**没有**任何 V2 字段；
- 显式传 `include_v2_metadata=true` 时，每条命中的证据额外带一个紧凑 Hydrated Evidence Hit：`unit_id`、版本化 `source_reference`、`display_locator`、`section_names`，以及指向 `get_evidence_package` 的 `expand` 提示。完整包按需展开，搜索结果本身不携带层内容；
- 新增工具只做新增，不重命名、不删除、不改默认值。
