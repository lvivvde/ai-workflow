# 新电脑环境搭建指南

本文面向 Windows 环境，说明如何在另一台电脑部署、配置和验证 `game-design-knowledge-mcp`。前置依赖满足后，优先使用一键部署；需要排障或了解各步骤时再走手动流程。

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

## 3. 选择部署方式

### 3.1 一键部署

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

### 3.2 手动创建项目环境

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

### 3.3 手动运行测试

```powershell
uv run python -m unittest discover -s tests -v
```

当前版本的全部测试都应通过。任何测试失败时先停止，不要继续配置 MCP。

## 4. 使用或重建共享索引

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

当前仓库示例语料的预期值如下。示例语料变化后，需要同步更新这些数值：

- `documents_indexed` 为 6。
- `images_indexed` 为 13。

首次建库（或删掉 `.index/knowledge` 之后重建）才会看到这两个数字；增量重建会把未变化的文档计成 `documents_reused`，不再重复统计图片，此时应查数据库里的 `images` 行数来确认图片仍是 13 张。

OCR 统计取决于本机是否安装 RapidOCR / PaddleOCR / Tesseract。三者都没有时，13 张图片应记录为 `ocr_unavailable`，`index_status.processing` 里的 `ocr` attempt 会把这条降级链逐级列出来。

重建成功后，把 `.index/knowledge/knowledge.sqlite` 和 `.index/knowledge/assets/` 与原始资料一起提交。数据库中的源文档路径使用相对索引目录的形式；另一台电脑的仓库绝对路径和 Git checkout 文件时间即使不同，只要 SHA256 内容一致，`index_status()` 也不会误报过期。

索引命令写入同级不可变快照（`.index/.knowledge.build-*`），校验通过后才替换正式索引并更新 `CURRENT.json`。失败、中断或被其他进程占用时旧索引继续可读，未通过校验的快照不会成为 active。快照与 `CURRENT.json` 只属于本机，不提交。

紧挨着还有 `.index/.knowledge.cache/`：内容寻址的 stage 缓存，只影响重算速度，可以随时删除；它不放在 `.index/knowledge/` 里面，所以正式索引的形状不变，首次构建失败也不会留下半个索引目录。

重建还会把本次构建的来源、解析修订和检索单元登记到持久状态目录 `.design-state/`（默认路径；可用 `GAME_DESIGN_STATE_DIR` 覆盖）。该目录保存 Review Events、确认字典和逻辑文档身份，删除 `.index` 后重建不会丢失；它默认不进 Git，随机附仓库提交的人工真源仍是 `knowledge/catalog.json` 与 `docs/`。

如果 `index_status()` 报告的 `schema_version` 低于本构建的目标版本（当前为 6），或 `index_freshness` 把 `lexical_index` 报成 `incompatible`，说明索引是旧 schema，需要显式迁移：

```powershell
uv run game-design-knowledge migrate --plan --database .index\knowledge\knowledge.sqlite
uv run game-design-knowledge migrate --database .index\knowledge\knowledge.sqlite
```

迁移前会自动备份到 `.index/knowledge/schema-backups/`，失败会还原备份；比当前版本更新的 schema 只会被拒绝，不会被旧代码改写。

注意迁移只**增加表**，不会回填派生数据：v5 → v6 会补上 `layout_runs` / `layout_elements` / `structural_relations`，但已发布索引里那些“内容未变、按 SHA 复用”的文档不会重新计算布局。要让共享索引带上阅读顺序与箭头关系，迁移后按第 4 节重建一次（资料未变时也可先删掉 `.index/knowledge/knowledge.sqlite` 再重建）。规则见 [`layout.md`](layout.md)。

## 5. 索引自己的策划资料

可以把可提交资料放入 `docs/` 对应分类，也可以直接指定仓库外的资料目录：

```powershell
uv run game-design-knowledge index `
  "D:\GameProject\DesignDocuments" `
  --output .index\game-project
```

当前正式版处理 DOCX 标题、段落、列表、表格与图片锚点，也处理 XLSX 单元格、公式、样式、合并范围与图片锚点。CSV、Markdown、PDF、PPTX 目前不解析。

## 6. 配置 MCP 客户端

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

### 6.1 让第三方 AI 分类文件并生成 SQLite

第三方 AI 能通过 MCP Server 读取待导入文件的本机绝对路径时，可以完成分类和建库。可以直接告诉 AI：

```text
请把这些文件作为正式策划资料导入。先给我展示移动/复制计划，
我确认后再执行，并在完成后验证索引。
```

AI 必须按 [`import-policy.md`](import-policy.md) 执行。主流程是：

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

导入边界：

- 只接受 DOCX/XLSX，最多一次 100 个文件。
- 拒绝符号链接、`.git/`、`.venv/`、`.index/` 中的输入文件。
- 永不覆盖同名目标文件。
- 计划生成后文件内容或路径发生变化，`plan_token` 会失效，必须重新预览确认。
- 复制/移动或解析失败时，新文件会回滚，旧 SQLite 保持可用。
- 不允许 AI 直接写 SQLite 表，也不会自动生成未被文档明确记载的玩法别名。

如果文件已经由人工放入项目目录，只需要更新索引，AI 先调用 `rebuild_shared_index(confirmed=false)` 展示计划，用户确认后再调用 `rebuild_shared_index(confirmed=true)`。

导入完成后应将工具返回的 `git_paths_to_commit` 纳入同一个提交，使其他电脑拉取后无需再次建库。

## 7. 配置严格文档证据规则

MCP Server 已通过运行时 instructions 提供严格证据政策。为了避免客户端忽略或弱化规则，还应把以下模板复制到实际游戏项目的 `AGENTS.md` 或平台永久 Rules：

```text
examples/client-rules/AGENTS.example.md
```

完整政策见 [`evidence-policy.md`](evidence-policy.md) 和 [`import-policy.md`](import-policy.md)。

核心约束是：只使用文档证据；未找到时明确回答未找到；禁止自动联想玩法、猜测设计意图或创建别名。

## 8. 验证 MCP

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

`search_images("文档信息", 10)` 应返回带出处的图片。`search_evidence("大风车")` 在当前示例文档中应返回空结果，不得自动联想到其他玩法。

改过 `src/` 之后，再跑一遍分层评测确认没有破坏 V1 契约或证据安全不变量：

```powershell
uv run python tools/evaluate.py `
  --corpus evaluation/corpora/v1_compatibility `
  --corpus evaluation/corpora/development_set `
  --corpus evaluation/corpora/golden_set
```

退出码为 `0` 表示五条 Release-blocking invariant 全部通过；非 `0` 时 `failures` 会逐条点名样本、模式和原因。评测全程不联网，产物写入 `evaluation/runs/`。协议见 [`../evaluation/README.md`](../evaluation/README.md)。

## 9. 可选 OCR

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

## 10. 更新流程

只更新代码时：

```powershell
git pull
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python tools/smoke_stdio.py .index/knowledge
```

原始资料或人工目录修改后，由一名维护者重建共享索引：

```powershell
uv run game-design-knowledge index . --output .index/knowledge
uv run python tools/smoke_stdio.py .index/knowledge
git add .index/knowledge
```

未变化文件按 SHA 复用，变化文件在快照中替换，删除文件同步清理；校验通过后才发布。原文与索引必须放在同一个提交中。其他成员拉取该提交即可复用，也可以先通过 `index_status()` 检查 `is_stale`。

人工确认的玩法与别名写入 `knowledge/catalog.json`。别名必须是包含 `name`、`confirmed_at`、`confirmed_by` 的对象；MCP 不会自动添加外号。

重建前后可以分别调用 `index_freshness()`，确认 source / durable state / parse / lexical 四层都回到 `fresh`。每层都会给出期望版本、实际版本、最近成功时间和建议动作。

## 11. 常见问题

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

### OCR 全部是 `unavailable`

本机既没有 RapidOCR/PaddleOCR，也没有 Tesseract（`ocr` stage 会逐级记录被跳过的原因和版本，见 `index_status.processing`、`get_image_context` 与 [`ocr.md`](ocr.md)）。这不影响图片提取、标题搜索和位置查询。

如果装了 RapidOCR 但仍然报 `models_missing`，是 ONNX 模型文件不在本地：把模型放到 `GAME_DESIGN_OCR_MODEL_DIR` 或包内 `models/` 后重建索引。构建期不会下载模型。

### `index_status.processing` 显示某些 stage 是 `unavailable`

这些 stage 的归属工单尚未交付，或本机缺少对应能力包。缺失的都是可选能力，索引照常可用；每个 absent stage 都带 `owner_ticket` 与 `reason_code`。若运行 `game-design-knowledge capabilities` 查看本机能力包与推荐 profile。

### `index_status()` 返回 `is_stale: true`

至少一份源文档在索引后被修改、移动或删除。重新运行建立索引的命令。

### `index_freshness()` 报告 `lexical_index` 为 `incompatible`

已发布的索引是旧 schema。按第 4 节的 `game-design-knowledge migrate` 显式迁移，或删除 `.index/knowledge` 后重建。

### 持久状态被拒绝（`StateVersionError`）

`.design-state/manifest.json` 由更新版本的 MCP server 写过。先升级本仓库代码（`git pull && uv sync --locked`），不要手工改写该文件；它记录了不可重建的人工确认历史。

### 查询别名没有结果

这是严格证据模式的预期行为。只有文档明确记载或用户明确确认的别名才允许使用，AI 不会自动联想。

## 12. 提交边界

以下内容只属于本机：

```text
.venv/
*.sqlite-shm
*.sqlite-wal
.index/.knowledge.build-*/     # 不可变快照与恢复资料
.index/.knowledge.cache/       # 内容寻址的 stage 缓存，可丢弃
.index/knowledge/CURRENT.json  # 本机发布指针
.index/knowledge/schema-backups/
.design-state/                 # 归档字节与人工确认历史
__pycache__/
*.pyc
```

`.index/knowledge/knowledge.sqlite` 与 `.index/knowledge/assets/` 是明确例外，应和当前项目内部资料一起提交。其他临时索引目录默认仍被忽略。`evaluation/corpora/` 属于版本化内容，`evaluation/runs/` 是本机可重现产物，默认忽略。`docs/`、`examples/` 中的资料服从项目仓库本身的访问权限，无需额外脱敏，但不得发布到项目环境之外。
