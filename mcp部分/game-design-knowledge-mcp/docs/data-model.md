# SQLite数据模型

SQLite是可删除、可重建的派生索引。人工确认的玩法和别名不以SQLite作为唯一真源。

## Schema版本

```sql
PRAGMA user_version = 2;
```

版本不匹配时要求重建，不设计复杂的数据迁移。

## documents

保存源文件身份、类型和新鲜度信息。

```text
id
path
document_type
source_size
source_mtime_ns
source_sha256
indexed_at
title
status
```

## document_blocks

保存DOCX正文的有序结构。

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

`section_path`保存从顶层标题到当前块的路径。`locator`保存稳定、可序列化的JSON定位信息。

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

保存XLSX原始单元格事实，不默认解释表头语义。

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

公式与缓存值分开保存。无法可靠解释的日期或格式化值保留原始值和style id，不自行推断含义。

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

为MCP提供统一查询视图。

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

`authority`只描述来源，不自动决定冲突中的正确版本。

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

只有确认目录中的记录可以进入该表。

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

所有FTS表都保存对应记录ID作为 `UNINDEXED` 字段，查询结果必须回到源表取得完整定位。

## 索引与约束

- `documents.path`唯一。
- `(document_id, ordinal)`唯一。
- `(sheet_id, cell_reference)`唯一。
- `feature_key`唯一。
- `(feature_id, alias)`唯一。
- 所有MCP查询使用参数化SQL。
- 删除文档时级联删除对应块、工作表、单元格、证据和图片引用。
- 共享图片资产只有在无引用时才能清理。

## 发布与增量更新

完整构建继续写入同级staging目录并原子发布。

增量更新以 `source_sha256` 为边界：

- 未变化：复用旧记录。
- 已变化：在单个事务中替换该文档全部派生记录。
- 已删除：删除文档及关联证据。
- catalog变化：只刷新catalog相关表。

增量更新不得在失败后留下新旧记录混合状态。
