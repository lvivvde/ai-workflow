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
├── evaluation/           # 分层离线评测的语料与脚手架
└── examples/             # 项目内部共享资料和示例配置
```

`docs/`、`examples/` 和 `.index/knowledge/` 服从当前项目的访问权限。资料变化后由一名维护者重建并提交共享索引，其他成员拉取后可以直接查询。资料与索引不得脱离项目访问边界传播。

## 当前功能

- 索引 DOCX 标题、段落、列表、表格单元格和图片锚点。
- 索引 XLSX 原始值、公式、样式、合并范围和图片锚点。
- 使用 SQLite FTS5 trigram 查询正文、配置、图片邻近文字和 OCR 文本。
- 通过文件 SHA 增量复用未变化文档，并检测过期或已删除的来源。
- 在不可变快照中构建，校验通过后才原子发布；失败、中断或文件占用都不会覆盖已有索引，并保留 Last Known Good 快照。
- 用 Logical Document → Source Revision → Parse Revision → Published Revision Bundle 记录来源链，来源哈希、处理清单和定位链路可回查。
- 人工确认状态（Review Events、确认字典）放在可删除重建的派生索引之外。
- 按 source / durable state / parse / lexical / semantic / explanation 六层分别报告新鲜度，不再只给一个 `is_stale`。
- 返回 `found`、`not_found`、`ambiguous` 或 `stale`，并附带结构化出处。
- 只识别人工目录中的正式名和已确认别名，不自动创建或联想外号。
- 导入和重建共享索引必须先预览、再由用户明确确认。
- 冻结 V1 公共契约，并用分层离线评测加五条 Release-blocking invariant 守住兼容性和证据安全。

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

离线评测：分层报告组件级与端到端质量，并强制五条 Release-blocking invariant。

```powershell
uv run python tools/evaluate.py `
  --corpus evaluation/corpora/v1_compatibility `
  --corpus evaluation/corpora/development_set `
  --corpus evaluation/corpora/golden_set
```

评测不产出混合总分，缺失能力如实记为 `unavailable`；任一 invariant 违规即退出码 `1` 并点名样本。协议细节见 [`evaluation/README.md`](evaluation/README.md)。

真实启动 stdio MCP 并调用 `index_status`：

```powershell
uv run python tools/smoke_stdio.py .index/knowledge
```

## 索引资料

```powershell
uv run game-design-knowledge index . --output .index/knowledge
```

`.index/knowledge` 可以随项目提交。源文档路径尽量保存为相对路径；换电脑或移动仓库后，只要内容 SHA256 不变，索引仍可复用。资料变化后，由维护者重建并把原文、SQLite 和 `assets/` 放在同一个 Git 提交中。

构建过程是"写快照 → 校验 → 发布"三步：每次构建写入同级 `.index/.knowledge.build-*` 快照目录，校验 SQLite 完整性和 schema 版本后才用重命名替换 `.index/knowledge/knowledge.sqlite` 并写 `CURRENT.json`。发布失败时旧索引继续可读，未校验的快照永远不会成为 active。快照目录是本地恢复资料，不进入 Git。

## 分阶段处理与本地能力包

建索引是 8 个 stage 的流水线（`source_parse` → `ocr` → `layout` → `structure_relations` → `notation` → `statements` → `explanation_cache` → `retrieval_projection`）。每个 stage 独立重试、独立缓存、独立降级：

| 机制 | 行为 |
|---|---|
| Stage Attempt | 每次执行写一行不可变记录；重试新增行，不覆盖历史 |
| Stage Fingerprint | 输入、上游输出、运行时版本、生效配置任一变化，只失效该 stage 及其下游 |
| Stage Cache | 内容寻址，整条指纹一致才命中，命中后重新哈希校验 |
| 能力包 | `core`（必装）、`enhanced_ocr`、`visual`（可选，均本地安装） |
| 降级链 | RapidOCR → PaddleOCR → Tesseract 兼容回退，逐级记录被跳过的原因 |
| 驻留 | 批次内复用；idle timeout、`--low-memory`、显式卸载、进程退出都会释放 |

可选能力缺失时索引照常发布，缺的部分明确标成 `unavailable`/`degraded`；必选 stage 失败时 `cli index` 退出码 1 且不发布。完整契约见 [`docs/processing.md`](docs/processing.md)。

```powershell
uv run game-design-knowledge capabilities
uv run game-design-knowledge index . --output .index\knowledge --low-memory
```

## 不可变修订与持久状态

派生索引可以随时删除重建；任何"人做过决定"的东西都不放在里面：

| 内容 | 位置 | 说明 |
|---|---|---|
| 逻辑文档 ID | `<项目根>/.design-state/manifest.json` | 由项目相对路径推导，换机器/换盘符后不变 |
| Source Revision 归档 | `<项目根>/.design-state/archive/` | 按内容寻址保存原始字节，同哈希只存一份 |
| Parse Revision / Review Events | `<项目根>/.design-state/journal/` | append-only JSONL，撤销是一次新事件 |
| 确认字典 | `<项目根>/.design-state/dictionary.json` | 别名必须带 `confirmed_at`/`confirmed_by` |
| Published Revision Bundle | `<项目根>/.design-state/bundles/` | 可用 `verify_bundle` 从 bundle 回查到归档字节和定位 |

`.design-state` 是本机持久状态，默认不进 Git；随仓库提交的人工真源仍是 `knowledge/catalog.json` 和 `docs/`。目录位置可用 `GAME_DESIGN_STATE_DIR` 覆盖。删除 `.index` 或整个派生索引后重建，Review Events、确认字典和逻辑文档身份都不会丢失。

索引 schema 当前为 `PRAGMA user_version = 4`。旧索引必须显式迁移，不能被静默误读：

```powershell
uv run game-design-knowledge migrate --plan --database .index\knowledge\knowledge.sqlite
uv run game-design-knowledge migrate --database .index\knowledge\knowledge.sqlite
```

迁移前自动备份到 `.index/knowledge/schema-backups/`；迁移失败会还原备份。比当前版本更新的 schema 只会被拒绝，不会被旧代码改写。

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
| 分层新鲜度 | `index_freshness` |
| 能力包与驻留 | `capability_status` |
| 受控导入 | `plan_document_import`、`import_documents` |
| 重建共享索引 | `rebuild_shared_index` |

所有 shared-index 读取工具都会返回同一次读取对应的 `index_status`；只要源文档或人工目录已过期，顶层 `status` 就统一为 `stale`。`index_status` 额外带 `freshness` 与 `processing`（最近一次运行的运行清单摘要和逐 stage 尝试计数），`index_freshness` 单独返回六层各自的状态、期望/实际版本、最近成功时间和建议动作，`capability_status` 只读报告能力包、硬件画像和当前驻留。

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

图片文字按引擎降级链处理：RapidOCR（core 能力包，ONNX Runtime）→ PaddleOCR（`enhanced_ocr`，需显式安装）→ 系统 `tesseract`（兼容回退）。链上每一步都会记录被跳过的原因和版本，`index_status.processing` 与 `get_image_context` 都能读到。

结果保存在**区域**粒度：每个区域有边界框、原文（Raw Transcription）、规范化建议与逐 span 变更、以及分开的文字/区域/关键标记三类置信度。原文永不被规范化文本覆盖，也不生成单一总分。数字、百分比、ID、运算符、箭头和否定词会作为 Critical Transcription Tokens 单独评分。

当前这条链在本机没有可用引擎时，行为与 V1 完全一致：文档和图片照常建立索引，图片记录为 `ocr_status=unavailable`。本项目不会自动安装系统级 OCR 软件，也不会在构建时下载模型——RapidOCR 的 ONNX 模型必须已经存在于 `GAME_DESIGN_OCR_MODEL_DIR` 或包内 `models/`。完整契约见 [`docs/ocr.md`](docs/ocr.md)。

可以通过环境变量覆盖语言与超时：

```powershell
$env:GAME_DESIGN_OCR_LANG = "chi_sim+eng"
$env:GAME_DESIGN_OCR_TIMEOUT = "60"
```

## 文档导航

- [`docs/setup.md`](docs/setup.md)：新电脑部署、客户端配置、验证和更新流程。
- [`docs/spec.md`](docs/spec.md)：支持范围、公开接口、查询结果和验收标准。
- [`docs/data-model.md`](docs/data-model.md)：SQLite Schema、约束和增量更新规则。
- [`docs/evidence-policy.md`](docs/evidence-policy.md)：查询时的事实与证据边界。
- [`docs/import-policy.md`](docs/import-policy.md)：导入、确认、回滚和提交边界。
- [`docs/catalog.md`](docs/catalog.md)：正式玩法与别名的人工确认格式。
- [`docs/revisions.md`](docs/revisions.md)：不可变修订、快照发布、持久状态与 schema 迁移。
- [`docs/processing.md`](docs/processing.md)：处理阶段、指纹与缓存契约、能力包与降级语义。
- [`docs/ocr.md`](docs/ocr.md)：区域级 OCR 的分层输出、三类置信度、关键标记评分与状态映射。
- [`evaluation/README.md`](evaluation/README.md)：分层评测协议、语料格式和 Release-blocking invariants。
