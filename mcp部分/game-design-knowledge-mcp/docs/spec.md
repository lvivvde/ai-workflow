# 正式版规格：全文与配置事实查询

版本目标：`0.2`。

## 目标

系统保留图片索引能力，并将DOCX正文和XLSX单元格转换为可搜索、可定位的文档证据，使MCP客户端能够回答“是否存在某玩法”“文档明确记录了哪些规则”“某个配置字段和值是什么”。

所有回答继续受 [`evidence-policy.md`](evidence-policy.md) 约束：只使用文档事实，不自动联想玩法、不推断设计意图、不创建未经确认的别名。

## 范围

当前正式版支持：

- DOCX正文标题、段落、列表、表格及现有图片锚点。
- XLSX工作表、单元格原始值、公式、类型、合并范围及现有图片锚点。
- DOCX/XLSX统一证据模型。
- SQLite FTS5中文全文检索。
- 正式玩法目录和人工确认别名。
- MCP结构化查询结果：`found`、`not_found`、`ambiguous`、`stale`。
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
game-design-knowledge index <source> --output <index>
```

CLI继续使用staging构建，全部成功后才发布。

### MCP

保留：

```text
search_images
get_image_context
index_status
```

新增：

```text
search_evidence(query, document_type=None, evidence_type=None, limit=20)
get_evidence(evidence_id, context_before=1, context_after=1)
find_feature(name)
get_feature_evidence(name, include_documents=True, include_configs=True, include_images=True)
search_config_cells(query, workbook=None, sheet=None, limit=50)
get_sheet_range(workbook, sheet, range)
plan_document_import(source_paths, destination="docs", operation="copy")
import_documents(source_paths, plan_token, destination="docs", operation="copy", confirmed=False)
rebuild_shared_index(confirmed=False)
```

三个写入相关工具遵循 [`import-policy.md`](import-policy.md)：预览不写入，导入必须携带未失效的计划令牌和明确确认；目标固定、防覆盖，建库失败恢复文件并保留旧索引。

## 统一查询结果

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
  "limitations": ["当前文档及已确认别名中未找到该名称"]
}
```

不得附加相似玩法候选。

## 证据定位

DOCX证据至少包含：

```text
source_document
section_path
block_type
block_ordinal
table/row/cell（适用时）
```

XLSX证据至少包含：

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

只有该文件明确列出的正式名称、ID和确认别名可以被 `find_feature` 解释为同一玩法。MCP不提供自动写入目录的工具。

## 验收查询

真实语料验收至少包含：

1. 搜索样例中明确存在的DOCX标题，返回对应块和原文位置。
2. 搜索样例XLSX中的明确单元格文字，返回工作簿、Sheet和A1地址。
3. 查询不存在的“大风车”，返回 `not_found`，不映射到其他玩法。
4. `get_evidence`返回命中块及相邻块，不越过文档边界。
5. `index_status`显示6份样例文档未过期。
6. 样例中的13张图片仍可搜索和定位。
7. 未确认的导入只返回计划，不移动、复制或重建文件。
8. 确认导入后按扩展名分类并原子重建；损坏文件触发回滚。
9. 第三方客户端启动后可以发现三个导入/重建工具。

## 完成标准

- 新旧测试全部通过。
- 真实6份样例可原子建库。
- 所有证据都有可追踪定位。
- 不存在未经确认的别名联想。
- SQLite schema版本为2。
- 构建失败不覆盖旧索引。
- 导入失败不遗留本次文件操作，且禁止覆盖同名资料。
- README和新电脑搭建指南与实现一致。
