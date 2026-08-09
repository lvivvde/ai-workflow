# 第三方 AI 文件导入与建库策略

本策略约束所有通过 `game-design-knowledge` MCP 导入策划资料的 AI 客户端。目标是让第三方 AI 能完成文件分类和 SQLite 重建，同时避免静默移动、覆盖、错误建库或无证据联想。

## 分类规则

| 用户意图 | destination | DOCX 目标 | XLSX 目标 |
|---|---|---|---|
| 正式项目资料（默认） | `docs` | `docs/docx/` | `docs/xlsx/` |
| 明确指定测试/示例资料 | `examples` | `examples/sample-corpus/docx/` | `examples/sample-corpus/xlsx/` |

未明确说明时一律按正式项目资料处理。当前不接受 CSV、Markdown、PDF、PPTX 或目录导入。

## 强制状态机

```text
收到文件路径
  -> plan_document_import（只读）
  -> 向用户展示源路径、目标路径、copy/move、SHA256
  -> 等待用户针对该计划明确确认
  -> import_documents（相同参数 + plan_token + confirmed=true）
  -> 原子重建共享 SQLite
  -> 检查 index_status.is_stale == false
  -> 报告结果和待提交路径
```

AI 不得跳过预览，不得自行设置 `confirmed=true`，也不得把“继续看看”“应该可以”等模糊表达当作确认。计划变化、文件内容变化或令牌不匹配时必须重新预览。

## copy 与 move

- 默认使用 `copy`，保留用户原文件。
- 只有用户明确要求移动并确认原位置文件会消失时才使用 `move`。
- 已经位于项目中的文件不得用 `copy` 制造重复证据；应保留原位重建，或明确使用 `move` 分类。
- 同名目标文件存在时一律停止，不覆盖、不自动改名。由用户决定改名或替换策略。

## 建库与回滚

- 导入工具只接受普通 DOCX/XLSX 文件，单次最多 100 个。
- 拒绝符号链接和来自 `.git/`、`.venv/`、`.index/` 的输入。
- 文件操作完成后，从 `GAME_DESIGN_PROJECT_ROOT` 原子重建 `GAME_DESIGN_INDEX_DIR`。
- 解析或建库失败时恢复本次复制/移动，旧 SQLite 继续可用。
- 禁止 AI 直接执行 SQL 修改正式数据库。
- 成功后原始资料、`knowledge/catalog.json`（如有变化）和 `.index/knowledge/` 必须处于同一个 Git 提交。

## 事实边界

导入成功只表示文件已进入证据库，不表示文档中的名称已成为正式玩法或别名。玩法与别名仍按 `docs/evidence-policy.md` 和 `knowledge/catalog.json` 的确认规则处理；AI 不得根据文件名、相似度或行业经验创建映射。

## 只重建索引

文件已经由人工放入正确目录时：

1. 调用 `rebuild_shared_index(confirmed=false)` 展示项目根目录与索引目录。
2. 用户明确确认后调用 `rebuild_shared_index(confirmed=true)`。
3. 验证返回的 `index_status.is_stale` 为 `false`。
