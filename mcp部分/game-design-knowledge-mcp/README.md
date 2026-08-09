# game-design-knowledge-mcp

面向游戏策划资料的本地知识索引与 MCP 查询服务。

## 定位

- DOCX、XLSX、CSV、Markdown 等原始文件始终是唯一真源。
- SQLite 是可删除、可重建的派生索引，不承载人工维护的数据；正式共享索引可以随项目提交。
- AI 先查询索引，再按命中位置读取少量原文，并在回答中返回文件、章节、工作表、行号或单元格等出处。
- 优先支持精确 ID、字段和全文检索；语义向量检索作为后续增强，不替代精确查询。

## 目录结构

```text
game-design-knowledge-mcp/
├── README.md
├── docs/                 # 按文件格式分类的策划原始资料
├── .index/knowledge/     # 可提交的预构建共享索引与图片资产
├── src/                  # 文件解析、增量索引、SQLite、MCP 服务
├── tests/                # 单元测试、集成测试和测试夹具
└── examples/             # 项目内部共享资料和示例配置
```

`docs/` 和 `examples/` 都属于当前项目内部资料，可以随私有项目仓库提交，无需为了本工作流额外脱敏。`.index/knowledge/` 是团队共享的预构建索引：一名成员在资料变化后重建并提交，其他成员拉取后即可直接查询，不需要重复建库。资料与索引不得脱离项目访问边界传播。

## 第一阶段目标

1. 扫描用户指定的资料目录，不修改原文件。
2. 使用文件哈希进行增量更新和过期检测。
3. 将 DOCX 按章节、段落、列表和表格索引。
4. 将 XLSX 按工作表、表头、配置行和单元格索引。
5. 使用 SQLite FTS5 提供中文全文搜索和结果排序。
6. 提供 MCP 查询接口，返回稳定的原文定位信息。

## 当前功能

当前正式版已经实现：

- 从 DOCX 提取内嵌图片，并记录所属标题、段落序号和段落文字。
- 从 XLSX 提取 drawing 图片，并记录工作表、锚点单元格和单元格文字。
- 图片按 SHA256 去重保存，SQLite 只记录相对资产路径和结构化出处。
- 使用 SQLite FTS5 trigram 搜索标题、邻近文字和 OCR 文本。
- 自动检测 Tesseract；不可用或失败时明确记录状态，不生成伪造文字。
- 使用 staging 构建并在成功后发布，失败不会覆盖已有索引。
- 提供严格证据查询工具，以及需要“先预览、再确认”的受控文档导入和共享索引重建工具。
- 索引 DOCX 标题、段落、列表和表格单元格，以及 XLSX 原始值、公式、样式和合并范围。
- 使用统一证据模型查询正文、配置和图片，并返回 `found`、`not_found`、`ambiguous` 或 `stale`。
- 只解析人工玩法目录中的正式名和已确认别名，不自动联想或写入外号。
- 使用文件 SHA 增量复用未变化文档，在 staging 中替换变化记录并清理已删除文档。

正式规格见 [`docs/spec.md`](docs/spec.md)，数据模型见 [`docs/data-model.md`](docs/data-model.md)。

## 安装与验证

另一台电脑从零搭建请先阅读 [`docs/setup.md`](docs/setup.md)。

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -v
```

Windows 上也可以一条命令完成锁定安装、测试、复用并验证共享索引，以及生成本机 MCP 配置：

```powershell
.\scripts\bootstrap.ps1
```

索引自己的资料：

```powershell
.\scripts\bootstrap.ps1 -Source "D:\GameProject\DesignDocuments" -Output ".index\game-project"
```

可选的大语料性能基准：

```powershell
uv run python tools/benchmark.py --documents 1000
```

真实启动 stdio MCP 并调用 `index_status`：

```powershell
uv run python tools/smoke_stdio.py .index/knowledge
```

## 建立示例索引

```powershell
uv run game-design-knowledge index . --output .index/knowledge
```

`.index/knowledge` 会提交到项目仓库。源文档路径以相对索引目录的形式保存；另一台电脑即使仓库绝对路径和文件修改时间不同，只要内容 SHA256 一致，索引仍可直接使用。资料变化后由维护者重建并把 SQLite 与 `assets/` 一起提交。

## 启动 MCP Server

先指定索引目录，再启动本地 stdio server：

```powershell
$env:GAME_DESIGN_INDEX_DIR = "D:\你的仓库路径\mcp部分\game-design-knowledge-mcp\.index\knowledge"
$env:GAME_DESIGN_PROJECT_ROOT = "D:\你的仓库路径\mcp部分\game-design-knowledge-mcp"
uv run game-design-knowledge-mcp
```

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "game-design-knowledge": {
      "command": "D:\\你的仓库路径\\mcp部分\\game-design-knowledge-mcp\\.venv\\Scripts\\game-design-knowledge-mcp.exe",
      "env": {
        "GAME_DESIGN_INDEX_DIR": "D:\\你的仓库路径\\mcp部分\\game-design-knowledge-mcp\\.index\\knowledge",
        "GAME_DESIGN_PROJECT_ROOT": "D:\\你的仓库路径\\mcp部分\\game-design-knowledge-mcp"
      }
    }
  }
}
```

## MCP 工具

- `search_images(query, limit=10)`：搜索图片标题、邻近文字和 OCR 文本。
- `get_image_context(image_id)`：返回图片文件、原文档、DOCX 标题/段落或 XLSX 工作表/单元格。
- `index_status()`：返回文档数、图片数、OCR 统计和源文件过期状态。
- `search_evidence(query, document_type=None, evidence_type=None, limit=20)`：查询 DOCX/XLSX 统一原文证据。
- `get_evidence(evidence_id, context_before=1, context_after=1)`：读取证据及同文档相邻块。
- `search_config_cells(query, workbook=None, sheet=None, limit=50)`：查询 XLSX 原始值、显示文字和公式。
- `get_sheet_range(workbook, sheet, range)`：读取指定工作表 A1 范围。
- `find_feature(name)`：只解析正式名和人工确认别名。
- `get_feature_evidence(name, ...)`：按正式名聚合正文、配置和图片证据。
- `plan_document_import(source_paths, destination="docs", operation="copy")`：只读预览 DOCX/XLSX 的源路径、目标路径、SHA256 和操作，不修改文件。
- `import_documents(..., plan_token, confirmed=true)`：按已确认计划复制或移动文件，并原子重建共享 SQLite；失败时恢复文件并保留旧索引。
- `rebuild_shared_index(confirmed=false)`：预览或明确确认后重建共享索引。

## 第三方 AI 导入资料

第三方 AI 只连接本 MCP 也可以完成分类和建库，但必须遵循两步确认：

1. 正式资料默认使用 `destination="docs"`；只有用户明确说“测试/示例资料”时才使用 `destination="examples"`。
2. 默认 `operation="copy"`，只有用户明确要求移动原文件时才使用 `move`。
3. 先调用 `plan_document_import`，向用户展示每个源文件、目标文件和操作。
4. 用户明确确认后，使用完全相同的参数、返回的 `plan_token` 和 `confirmed=true` 调用 `import_documents`。
5. 工具只接受 DOCX/XLSX，禁止覆盖同名文件；建库失败会回滚文件操作。
6. 完成后将原始资料、`.index/knowledge/knowledge.sqlite` 和 `.index/knowledge/assets/` 放在同一个 Git 提交中。

该流程只处理文件分类与事实索引，不会从文档名称或内容猜测、创建玩法别名。

## 人工玩法目录

目录模板位于 [`knowledge/catalog.json`](knowledge/catalog.json)，填写规则见 [`docs/catalog.md`](docs/catalog.md)。别名必须提供 `name`、`confirmed_at` 和 `confirmed_by`，查询工具不会自动修改该文件。索引源可以是项目根目录、`knowledge/` 或 `knowledge/docs/`。

## 文档证据政策

本 MCP 使用严格的“只按文档事实回答”模式：禁止根据模型常识、名称相似度或行业经验推断项目玩法；没有证据时必须明确返回未找到；设计意图必须有文档明示依据；别名只有文档记载或用户明确确认后才能使用。

完整证据规则见 [`docs/evidence-policy.md`](docs/evidence-policy.md)，文件写入规则见 [`docs/import-policy.md`](docs/import-policy.md)。实际游戏项目还应将 [`examples/client-rules/AGENTS.example.md`](examples/client-rules/AGENTS.example.md) 的内容复制到项目 `AGENTS.md` 或平台永久 Rules 中，以加固客户端行为。

## OCR

当前版本使用系统中的 `tesseract` 命令，默认语言为 `chi_sim+eng`。可以通过环境变量覆盖：

```powershell
$env:GAME_DESIGN_OCR_LANG = "chi_sim+eng"
```

如果没有安装 Tesseract，文档和图片仍会正常建立索引，图片记录为 `ocr_status=unavailable`。本项目不会自动安装系统级 OCR 软件。

## 与 Skill 的边界

索引、解析和查询能力放在本 MCP 项目中。待 MCP 接口稳定后，可以在 `skill部分/` 增加一个轻量 Skill，只负责约束 AI 的查询顺序、证据核对、冲突报告和回答格式。
