# game-design-knowledge-mcp

面向游戏策划资料的本地知识索引与 MCP 查询服务。它把 DOCX、XLSX 和其中的图片转换为可搜索、可定位的 SQLite 派生索引；原始文件始终是事实真源。

## 使用边界

- 当前正式版解析 DOCX 和 XLSX。CSV、Markdown、PDF、PPTX 仅作为资料分类目录保留。
- SQLite 可以删除并重建，不承载需要人工维护的唯一数据。团队可以提交 `.index/knowledge/`，让其他成员直接复用共享索引。
- AI 先查索引，再按命中位置读取少量原文；回答必须返回文件、章节、工作表、行号或单元格等出处。
- 查询优先使用精确 ID、字段和全文搜索。语义检索不能替代明确证据。
- Tesseract OCR 是可选依赖。缺失时仍会提取图片和记录锚点，但不会生成 OCR 文本。

完整证据规则见 [`docs/evidence-policy.md`](docs/evidence-policy.md)，写入和回滚规则见 [`docs/import-policy.md`](docs/import-policy.md)。

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

`docs/`、`examples/` 和 `.index/knowledge/` 服从当前项目的访问权限。资料变化后由一名维护者重建并提交共享索引，其他成员拉取后可以直接查询。资料与索引不得脱离项目访问边界传播。

## 当前功能

- 索引 DOCX 标题、段落、列表、表格单元格和图片锚点。
- 索引 XLSX 原始值、公式、样式、合并范围和图片锚点。
- 使用 SQLite FTS5 trigram 查询正文、配置、图片邻近文字和 OCR 文本。
- 通过文件 SHA 增量复用未变化文档，并检测过期或已删除的来源。
- 在 staging 中构建，成功后才发布；失败不会覆盖已有索引。
- 返回 `found`、`not_found`、`ambiguous` 或 `stale`，并附带结构化出处。
- 只识别人工目录中的正式名和已确认别名，不自动创建或联想外号。
- 导入和重建共享索引必须先预览、再由用户明确确认。

正式规格见 [`docs/spec.md`](docs/spec.md)，数据模型见 [`docs/data-model.md`](docs/data-model.md)。

## 安装与验证

另一台电脑从零搭建、重建共享索引或排查环境问题时，使用 [`docs/setup.md`](docs/setup.md)。

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

## 索引资料

```powershell
uv run game-design-knowledge index . --output .index/knowledge
```

`.index/knowledge` 可以随项目提交。源文档路径尽量保存为相对路径；换电脑或移动仓库后，只要内容 SHA256 不变，索引仍可复用。资料变化后，由维护者重建并把原文、SQLite 和 `assets/` 放在同一个 Git 提交中。

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

| 用途 | 工具 |
|---|---|
| 查询图片 | `search_images`、`get_image_context` |
| 查询正文证据 | `search_evidence`、`get_evidence` |
| 查询配置 | `search_config_cells`、`get_sheet_range` |
| 查询玩法 | `find_feature`、`get_feature_evidence` |
| 检查索引 | `index_status` |
| 受控导入 | `plan_document_import`、`import_documents` |
| 重建共享索引 | `rebuild_shared_index` |

所有 shared-index 读取工具都会返回同一次读取对应的 `index_status`；只要源文档或人工目录已过期，顶层 `status` 就统一为 `stale`。

## 第三方 AI 导入资料

导入 DOCX/XLSX 必须经过以下流程：

1. 调用 `plan_document_import`，只读预览源路径、目标路径、操作和 SHA256。
2. 向用户展示计划并等待明确确认。
3. 使用相同参数、返回的 `plan_token` 和 `confirmed=true` 调用 `import_documents`。
4. 验证 `index_status.is_stale` 为 `false`，再报告结果和待提交路径。

默认复制到正式资料目录。只有用户明确指定测试资料时才使用 `examples`，明确要求移动时才使用 `move`。工具禁止覆盖同名文件；导入或建库失败时会恢复本次文件操作并保留旧索引。

## 人工玩法目录

目录模板位于 [`knowledge/catalog.json`](knowledge/catalog.json)，填写规则见 [`docs/catalog.md`](docs/catalog.md)。别名必须提供 `name`、`confirmed_at` 和 `confirmed_by`，查询工具不会自动修改该文件。索引源可以是项目根目录、`knowledge/` 或 `knowledge/docs/`。

## 文档证据政策

- 没有证据时明确返回未找到，不映射到相似玩法。
- 文档只记录部分规则时，同时说明未记录内容。
- 多份证据冲突时列出各自出处，不替用户选择正确版本。
- 只有文档明确说明时才能陈述设计意图。
- 别名必须由文档或用户明确确认。

实际游戏项目还应将 [`examples/client-rules/AGENTS.example.md`](examples/client-rules/AGENTS.example.md) 复制到项目 `AGENTS.md` 或平台永久 Rules 中。

## OCR

当前版本使用系统中的 `tesseract` 命令，默认语言为 `chi_sim+eng`。可以通过环境变量覆盖：

```powershell
$env:GAME_DESIGN_OCR_LANG = "chi_sim+eng"
```

如果没有安装 Tesseract，文档和图片仍会正常建立索引，图片记录为 `ocr_status=unavailable`。本项目不会自动安装系统级 OCR 软件。

## 文档导航

- [`docs/setup.md`](docs/setup.md)：新电脑部署、客户端配置、验证和更新流程。
- [`docs/spec.md`](docs/spec.md)：支持范围、公开接口、查询结果和验收标准。
- [`docs/data-model.md`](docs/data-model.md)：SQLite Schema、约束和增量更新规则。
- [`docs/evidence-policy.md`](docs/evidence-policy.md)：查询时的事实与证据边界。
- [`docs/import-policy.md`](docs/import-policy.md)：导入、确认、回滚和提交边界。
- [`docs/catalog.md`](docs/catalog.md)：正式玩法与别名的人工确认格式。
