# SQLite 数据模型

SQLite 是可删除、可重建的派生索引。正式 `.index/knowledge` 会随项目提交，供团队成员直接复用；人工确认的玩法和别名仍不以 SQLite 作为唯一真源。

## Schema 版本

```sql
PRAGMA user_version = 4;
```

版本不匹配时不读取、不猜测：

- v2 → v3 走显式迁移（`game-design-knowledge migrate`），迁移前自动备份，失败还原。
- v3 → v4 走显式迁移，新增处理清单与 stage 尝试两张表。
- 更旧或更新的版本一律显式拒绝，并给出原因。

详解见 [`revisions.md`](revisions.md)。

## documents

保存源文件身份、类型和新鲜度信息。

`path` 在源文件与索引位于同一文件系统时保存为相对索引目录的路径，保证仓库换目录或换电脑后仍可定位；无法构造相对路径时才回退到绝对路径。跨电脑文件修改时间不同时使用 `source_sha256` 复核内容。

```text
id
path
document_type
source_size
source_mtime_ns
source_sha256
indexed_at
logical_document_id
source_revision_id
parse_revision_id
title
status
```

三个修订列是派生索引对不可变修订链的投影：`logical_document_id` 由项目相对路径推导，`source_revision_id` 绑定该文档的内容哈希，`parse_revision_id` 再绑定 schema 版本与 Processing Fingerprint。权威记录在项目的持久状态目录里。

## source_revisions / parse_revisions

派生索引里保留的修订投影，便于不打开持久状态就能回答"这行数据来自哪次解析"。

```text
source_revisions: source_revision_id, document_id, relative_path,
                  content_sha256, byte_size, recorded_at
parse_revisions:  parse_revision_id, source_revision_id, document_id,
                  database_schema_version, processing_fingerprint,
                  created_at, active
```

## index_builds / schema_migrations

```text
index_builds:      build_id, state, started_at, finished_at, note,
                   database_sha256, processing_fingerprint,
                   processing_manifest, parse_revisions
schema_migrations: version, applied_at, description, backup_path
```

`index_builds` 记录每次构建实际使用的处理清单指纹和 parse revision 映射；`schema_migrations` 是显式迁移的审计记录。

## processing_manifests / stage_attempts

运行清单与它逐段的执行记录。两者都由构建过程写进快照，所以随索引一起发布；读取方式见 [`processing.md`](processing.md)。

```text
processing_manifests: build_id, run_id, created_at, configured_fingerprint,
                      configured_manifest, run_manifest, profile,
                      capability, limits
stage_attempts:       attempt_id, build_id, run_id, stage, attempt_number,
                      document_path, execution_status, quality_status,
                      reason_code, detail, fingerprint, input_sha256,
                      output_sha256, cache_hit, fallback_used, engine,
                      engine_version, model, model_version, owner_ticket,
                      coverage, reason_chain, started_at, finished_at,
                      duration_ms
```

`stage_attempts` 是 append-only 的：重试插入新的 `attempt_id`，不会改写上一轮的行。`reason_chain` 保存降级链上每一步的可用性与跳过原因，`fallback_used` 区分“等价成功”和“降级成功”。

## document_blocks

保存 DOCX 正文的有序结构。

```text
id
document_id
parent_id
ordinal
block_type
heading_level
section_path
style
text
source_part
locator
```

`block_type`初始枚举：

```text
heading
paragraph
list_item
table
table_row
table_cell
```

`section_path` 保存从顶层标题到当前块的路径。`locator` 保存稳定、可序列化的 JSON 定位信息。

## workbook_sheets

```text
id
document_id
sheet_name
sheet_index
visibility
used_range
```

## sheet_cells

保存 XLSX 原始单元格事实，不默认解释表头语义。

```text
id
sheet_id
cell_reference
row_index
column_index
raw_value
display_text
formula
data_type
style_id
merged_range
```

公式与缓存值分开保存。无法可靠解释的日期或格式化值保留原始值和 style ID，不自行推断含义。

## images

图片表继续保存：

```text
document_id
asset_path
sha256
heading
paragraph_index
context_text
sheet_name
cell_anchor
ocr_status
ocr_text
```

资产路径相对索引目录保存，保证staging发布后仍有效。

## evidence

为 MCP 提供统一查询视图。

```text
id
document_id
evidence_type
source_table
source_record_id
text
section_path
locator
authority
```

`authority`初始值：

```text
document
configuration
image_ocr
confirmed_catalog
```

`authority` 只描述来源，不自动决定冲突中的正确版本。

## catalog_features

从 `knowledge/catalog.json` 导入：

```text
id
feature_key
canonical_name
source
```

## catalog_aliases

```text
id
feature_id
alias
source
confirmed_at
confirmed_by
```

只有人工确认目录中的记录可以进入该表。

## FTS5

```text
block_fts
cell_fts
image_fts
evidence_fts
```

中文默认使用：

```sql
tokenize='trigram'
```

所有 FTS 表都把对应记录 ID 保存为 `UNINDEXED` 字段。查询结果必须回到源表取得完整定位。

## 索引与约束

- `documents.path`唯一。
- `(document_id, ordinal)`唯一。
- `(sheet_id, cell_reference)`唯一。
- `feature_key`唯一。
- `(feature_id, alias)`唯一。
- 所有 MCP 查询使用参数化 SQL。
- 删除文档时级联删除对应块、工作表、单元格、证据和图片引用。
- 共享图片资产只有在无引用时才能清理。

## 发布与增量更新

完整构建写入同级不可变快照目录（`.index/.knowledge.build-*`），校验通过后才用重命名把数据库与资产换成 active，并原子更新 `CURRENT.json`。快照保留 Active 与 Last Known Good；未通过校验的快照不会成为 active，构建失败、进程中断或 Windows 文件占用都不影响旧索引可读。

增量更新以 `source_sha256` 为边界：

- 未变化：复用旧记录。
- 已变化：在单个事务中替换该文档全部派生记录。
- 已删除：删除文档及关联证据。
- catalog 变化：只刷新 catalog 相关表。

增量更新不得在失败后留下新旧记录混合状态。
