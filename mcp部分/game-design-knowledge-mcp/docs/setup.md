# 新电脑环境搭建指南

本文面向 Windows 环境，用于在另一台电脑拉取仓库后，从零搭建 `game-design-knowledge-mcp`。

## 1. 前置依赖

在 PowerShell 中检查：

```powershell
python --version
uv --version
git --version
```

要求：

- Python 3.10 或更高版本。
- uv 可用，用于创建项目虚拟环境并按 `uv.lock` 安装依赖。
- Git 可用。
- Node.js 不是本项目的运行依赖。
- Tesseract OCR 可选；缺失时索引仍可正常工作。

缺少系统级软件时，请先手动完成安装，再继续本指南。本项目不会自动修改系统级环境。

## 2. 拉取仓库

```powershell
git clone <仓库地址>
cd "<仓库路径>\mcp部分\game-design-knowledge-mcp"
```

如果仓库已经存在：

```powershell
git pull
cd "<仓库路径>\mcp部分\game-design-knowledge-mcp"
```

后续命令均在 `game-design-knowledge-mcp` 目录执行。

## Windows 一键部署

前置依赖满足后，可以直接运行：

```powershell
.\scripts\bootstrap.ps1
```

脚本会依次执行锁定依赖安装、完整测试、验证仓库自带的共享索引，并输出包含本机绝对路径的 MCP JSON。默认不会重复建库；仅在共享索引缺失时自动创建。资料变化后需要重建时使用：

```powershell
.\scripts\bootstrap.ps1 -RebuildIndex
```

索引仓库外的其他资料时使用独立输出目录：

```powershell
.\scripts\bootstrap.ps1 `
  -Source "D:\GameProject\DesignDocuments" `
  -Output ".index\game-project"
```

脚本不会安装 Python、uv、Git 或 Tesseract 等系统软件；缺少前置依赖时会明确停止。

## 3. 创建项目环境

```powershell
uv sync --locked
```

该命令会：

- 创建本机 `.venv/`。
- 按 `uv.lock` 安装 MCP Python SDK 和项目包。
- 生成 `game-design-knowledge` 与 `game-design-knowledge-mcp` 命令入口。

`.venv/` 是本机产物，已被 Git 忽略，不需要从其他电脑复制。

验证 MCP SDK：

```powershell
uv run python -c "import importlib.metadata; print(importlib.metadata.version('mcp'))"
```

## 4. 运行测试

```powershell
uv run python -m unittest discover -s tests -v
```

当前版本的全部测试都应通过。任何测试失败时先停止，不要继续配置 MCP。

## 5. 使用或重建共享索引

仓库已经包含以下共享索引，普通使用者拉取后无需执行建库命令：

```text
.index/knowledge/knowledge.sqlite
.index/knowledge/assets/
```

需要验证它可以启动 MCP 时运行：

```powershell
uv run python tools/smoke_stdio.py .index/knowledge
```

只有原始资料或人工目录发生变化时，维护者才需要重建：

```powershell
uv run game-design-knowledge index `
  . `
  --output .index/knowledge
```

固定预期：

- `documents_indexed` 为 6。
- `images_indexed` 为 13。

OCR统计取决于本机是否安装 Tesseract。未安装时，13 张图片应记录为 `ocr_unavailable`。

重建成功后，把 `.index/knowledge/knowledge.sqlite` 和 `.index/knowledge/assets/` 与原始资料一起提交。数据库中的源文档路径使用相对索引目录的形式；另一台电脑的仓库绝对路径和 Git checkout 文件时间即使不同，只要 SHA256 内容一致，`index_status()` 也不会误报过期。

索引命令使用 staging 构建：全部成功后才替换正式索引；失败不会覆盖已有可用索引。

## 6. 索引自己的策划资料

可以把可提交资料放入 `docs/` 对应分类，也可以直接指定仓库外的资料目录：

```powershell
uv run game-design-knowledge index `
  "D:\GameProject\DesignDocuments" `
  --output .index\game-project
```

当前正式版处理 DOCX 标题、段落、列表、表格与图片锚点，也处理 XLSX 单元格、公式、样式、合并范围与图片锚点。CSV、Markdown、PDF、PPTX 目前不解析。

## 7. 配置 MCP 客户端

先取得三条本机绝对路径：

```powershell
Resolve-Path .venv\Scripts\game-design-knowledge-mcp.exe
Resolve-Path .index\knowledge
Resolve-Path .
```

把结果填入 MCP 客户端配置：

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

注意：

- 不要复制其他电脑的绝对路径。
- `GAME_DESIGN_INDEX_DIR` 指向包含 `knowledge.sqlite` 的目录，不是 SQLite 文件本身。
- `GAME_DESIGN_PROJECT_ROOT` 指向本 MCP 项目根目录，导入工具只会写入该目录下的固定资料目录和共享索引。
- 修改 MCP 配置后需要重启 AI 客户端。

## 7.1 让第三方 AI 分类文件并生成 SQLite

第三方 AI 只要能通过 MCP Server 读取待导入文件的本机绝对路径，就可以完成分类和建库。推荐直接告诉 AI：

```text
请把这些文件作为正式策划资料导入。先给我展示移动/复制计划，
我确认后再执行，并在完成后验证索引。
```

AI 必须按以下顺序操作：

1. 调用 `plan_document_import`：
   - `source_paths`：一个或多个本机 DOCX/XLSX 绝对路径。
   - `destination="docs"`：正式项目资料。
   - `destination="examples"`：仅用于用户明确指定的测试/示例资料。
   - `operation="copy"`：默认选择，保留原文件。
   - `operation="move"`：只有用户明确要求移动时选择。
2. 把工具返回的每个源路径、目标路径、操作和冲突情况展示给用户。
3. 用户明确确认后，使用完全相同的参数、返回的 `plan_token`，调用 `import_documents(..., confirmed=true)`。
4. 工具把文件放到对应 `docx/` 或 `xlsx/` 目录，随后原子重建 `.index/knowledge/knowledge.sqlite` 和图片资产。
5. 检查返回的 `index_status.is_stale` 必须为 `false`，再报告完成。

安全边界：

- 只接受 DOCX/XLSX，最多一次 100 个文件。
- 拒绝符号链接、`.git/`、`.venv/`、`.index/` 中的输入文件。
- 永不覆盖同名目标文件。
- 计划生成后文件内容或路径发生变化，`plan_token` 会失效，必须重新预览确认。
- 复制/移动或解析失败时，新文件会回滚，旧 SQLite 保持可用。
- 不允许 AI 直接写 SQLite 表，也不会自动生成未被文档明确记载的玩法别名。

如果文件已经由人工放入项目目录，只需要更新索引，AI 先调用 `rebuild_shared_index(confirmed=false)` 展示计划，用户确认后再调用 `rebuild_shared_index(confirmed=true)`。

导入完成后应将工具返回的 `git_paths_to_commit` 纳入同一个提交，使其他电脑拉取后无需再次建库。

## 8. 配置严格文档证据规则

MCP Server 已通过运行时 instructions 提供严格证据政策。为了避免客户端忽略或弱化规则，还应把以下模板复制到实际游戏项目的 `AGENTS.md` 或平台永久 Rules：

```text
examples/client-rules/AGENTS.example.md
```

完整政策：

```text
docs/evidence-policy.md
docs/import-policy.md
```

核心约束是：只使用文档证据；未找到时明确回答未找到；禁止自动联想玩法、猜测设计意图或创建别名。

## 9. 验证 MCP

重启客户端后调用：

```text
index_status()
```

示例索引应返回：

```text
documents_indexed: 6
images_indexed: 13
stale_documents: 0
is_stale: false
```

然后测试已存在和不存在的查询：

```text
search_images("文档信息", 10)
search_evidence("Core Loop")
search_config_cells("糖果起点")
search_evidence("大风车")
```

前者应返回带出处的图片；后者在当前示例文档中不存在，应返回空结果，不得自动联想到其他玩法。

## 10. 可选 OCR

检查系统是否已有 Tesseract：

```powershell
tesseract --version
```

默认 OCR 语言：

```text
chi_sim+eng
```

可以在构建索引前覆盖：

```powershell
$env:GAME_DESIGN_OCR_LANG = "chi_sim+eng"
```

需要确保对应语言数据已经由 Tesseract 安装。OCR不可用或失败时，图片仍会提取和建立锚点，但不会产生 OCR 文本。

## 11. 更新流程

代码更新后：

```powershell
git pull
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run game-design-knowledge index . --output .index/knowledge
uv run python tools/smoke_stdio.py .index/knowledge
git add .index/knowledge
```

原始资料修改后，由一名维护者重新运行相同索引命令并把原文与索引放在同一个提交中。未变化文件按 SHA 复用，变化文件在 staging 中替换，删除文件同步清理；全部成功后才发布。其他成员拉取该提交即可复用。也可以先通过 `index_status()` 检查 `is_stale`。

人工确认的玩法与别名写入 `knowledge/catalog.json`。别名必须是包含 `name`、`confirmed_at`、`confirmed_by` 的对象；MCP 不会自动添加外号。

## 12. 常见问题

### 看不到 SQLite

索引在点目录 `.index/` 中。正式共享目录 `.index/knowledge/` 已提交到 Git；使用 PowerShell 查看：

```powershell
Get-ChildItem -Force .index\knowledge
```

### `ModuleNotFoundError: No module named 'mcp'`

项目依赖没有完整同步。重新运行：

```powershell
uv sync --locked --verbose
```

成功后再使用 `.venv` 或 `uv run`，不要依赖系统 Python 中的包。

### OCR全部是 `unavailable`

Tesseract不在 `PATH` 中。这不影响图片提取、标题搜索和位置查询。

### `index_status()` 返回 `is_stale: true`

至少一份源文档在索引后被修改、移动或删除。重新运行建立索引的命令。

### 查询外号没有结果

这是严格证据模式的预期行为。只有文档明确记载或用户明确确认的别名才允许使用，AI不会自动联想。

## 13. 提交边界

以下内容只属于本机：

```text
.venv/
*.sqlite-shm
*.sqlite-wal
__pycache__/
*.pyc
```

`.index/knowledge/knowledge.sqlite` 与 `.index/knowledge/assets/` 是明确例外，应和当前项目内部资料一起提交。其他临时索引目录默认仍被忽略。`docs/`、`examples/` 中的资料服从项目仓库本身的访问权限，无需额外脱敏，但不得发布到项目环境之外。
