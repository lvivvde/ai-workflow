# Changelog

## Unreleased

- 所有 shared-index 读取工具统一返回 freshness 状态，包括 `not_found`、`ambiguous` 和图片查询路径。
- 单次查询复用一个 SQLite 连接和 freshness snapshot；源文档或目录过期时统一返回 `status=stale`。

## 1.1.0 - 2026-08-09

- 增加第三方 AI 可调用的文档导入预览、确认导入与共享索引重建 MCP 工具。
- 正式资料与示例资料按 DOCX/XLSX 自动进入固定分类目录。
- 增加计划令牌、明确确认、防覆盖、并发锁和建库失败文件回滚。
- MCP 配置增加项目根目录，补充跨电脑导入与 Git 提交策略。

## 1.0.0 - 2026-08-09

- 索引 DOCX 标题、段落、列表、表格和内嵌图片。
- 索引 XLSX 工作表、单元格、公式、样式、合并范围和锚定图片。
- 提供统一证据查询、配置查询、图片查询和上下文读取 MCP 工具。
- 提供严格的 `found`、`not_found`、`ambiguous`、`stale` 返回契约。
- 支持人工确认的正式玩法目录和别名，禁止自动联想。
- 支持按文件 SHA 增量更新和 staging 原子发布。
- 随项目提交可移植的 `.index/knowledge` 共享索引，其他成员拉取后可直接部署查询。
