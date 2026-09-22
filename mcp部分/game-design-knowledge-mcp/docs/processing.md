# 分阶段处理流水线、本地能力包与降级语义

建索引不是一个不可分割的动作。它是一串可以被单独重试、单独缓存、单独降级的 stage：每个 stage 都有自己的输入、执行状态、质量状态和指纹，所以“这份结果是怎么来的”可以逐段回答，而不是只能回答“引擎说它跑完了”。

这份文档描述四件事：stage 清单、指纹与缓存契约、能力包与降级链、驻留与资源预算。

## Stage 清单

`src/game_design_knowledge/processing.py` 里的 `DEFAULT_PIPELINE` 是唯一声明处。顺序即依赖顺序，`retrieval_projection` 固定最后一个。

| # | Stage | 层次 | 能力包 | 必选 | 粒度 | 归属 |
|---|---|---|---|---|---|---|
| 1 | `source_parse` | core | core | 是 | 每文档 | V2-03（本次实现） |
| 2 | `ocr` | core | core | 否 | 每文档 | V2-04（本次实现） |
| 3 | `layout` | core | core | 否 | 每文档 | V2-05（本次实现） |
| 4 | `structure_relations` | core | core | 否 | 每文档 | V2-05（本次实现） |
| 5 | `notation` | core | core | 否 | 每文档 | V2-07 |
| 6 | `statements` | core | core | 否 | 每文档 | V2-08 |
| 7 | `explanation_cache` | optional | — | 否 | 每项目 | V2-08 |
| 8 | `retrieval_projection` | core | core | 是 | 每项目 | V2-03（本次实现） |

本版本真正干重活的是四段：`source_parse` 归一化文档身份，`ocr` 按降级链产出区域级转录并给每个区域打分，`layout` 与 `structure_relations` 把区域读成顺序和保守的关系，`retrieval_projection` 发布派生索引快照。其余 stage 已经声明、已经记录，但返回 `execution_status="unavailable"` 并带上归属工单，让“缺能力”这件事显式可见，而不是被悄悄跳过。

`ocr` stage 的规则集与输出 schema 版本固定为 `ocr-regions-v1`，都写进 stage 定义（`processing.py`），所以换了输出契约就会让该 stage 的指纹变化，而不是悄悄改变同一份结果的含义。区域级细节见 [`ocr.md`](ocr.md)。

`explanation_cache` 与 `statements` 不写索引内容：V2-08 的释义层在**读取时**由 `explain_evidence` / `explain_query` 依据已发布的层现算原子陈述与 Profile，因此 `explanation_cache` 的 `reason_code` 是 `not_configured`（本版本不落盘解释缓存），而 `statements` 的原子陈述由该层在读取时构建。释义契约见 [`explanation.md`](explanation.md)。

`layout`（`layout-regions-v1`）与 `structure_relations`（`flow-arrow-v1`）的**计算发生在 `retrieval_projection` 内部**：只有那里同时持有本次解析的 OCR 区域，在 stage 里重算就只会读到上一版索引的区域。这两个 stage 记录的是决策——跑哪套规则集、读哪个引擎产出的区域、需不需要视觉模型——因此它们的 `reason_code` 是 `no_ocr_engine` 而不是 `stage_not_implemented`。两者都是 core 能力：几何与箭头规则不依赖 `enhanced_ocr`/`visual` 包。规则与边界见 [`layout.md`](layout.md)。

Stage 的顺序不是装饰：`retrieval_projection` 把运行清单一起写进快照，所以它必须是最后一个 stage，否则它之后的 stage 尝试永远进不了它要解释的那个索引。

### 执行状态与质量状态

执行状态：`succeeded`、`partial`、`failed`、`unavailable`、`skipped`。
质量状态：`accepted`、`uncertain`、`rejected`。

两者刻意分开：一个 stage 可以“跑成功了但没有可用结果”（`unavailable` + `rejected`），也可以“降级成功”（`partial` + `accepted`）。

必选 stage（`required=True`）出现 `failed`/`unavailable` 时，`ProcessingRun.blocking_failures()` 会列出它，调用方按各自语义处理：

- `cli index` 打印到 stderr 并以退出码 1 结束，不发布索引；
- `import_documents` / `rebuild_shared_index` 抛出 `ProcessingError`（保留原始异常为 `__cause__`），导入的文件按既有策略回滚；
- 读取路径不受影响，因为失败根本没有发布新快照。

可选 stage 缺失不会中断 Core：索引照常发布，缺的部分以 `unavailable`/`degraded` 出现在 `index_status.processing` 里。

## 指纹与缓存契约

有两个指纹，含义不同，不可互换：

| 指纹 | 位置 | 绑定什么 |
|---|---|---|
| Configured Fingerprint | `ProcessingManifest.fingerprint` | 配置了哪些 stage、规则集版本、handler 与版本、输出 schema |
| Stage Fingerprint | `stage_fingerprint()` | 一次尝试的输入哈希、上游输出哈希、运行时身份与版本、生效配置、规则集与输出 schema |

Configured Fingerprint 会被写进 Parse Revision：同一份字节 + 不同处理配置 = 不同的 Parse Revision，不会被当作命中缓存。写 Parse Revision 的所有入口（pipeline、`index_documents` 默认值、freshness 校验）都取同一个 `configured_manifest()`，否则一份健康的索引会被判成 stale。

Stage Fingerprint 是缓存键。规则是：

- 缓存命中要求整条指纹完全一致；只比输入哈希不够。
- 命中后返回的 payload 会重新哈希校验，半写或被人改过的条目按 `corrupt` 计数并重新计算，不会被当成有效结果复用。
- 输入或处理器指纹变化时，该 stage 及其全部下游失效；`downstream_stages(name)` 是唯一的传递闭包实现。
- 不缓存的只是“可复用性”，不是“记录”：每次尝试都会新增一行不可变 Stage Attempt。

缓存是内容寻址、一次构建即可丢弃的本地目录，位置在索引目录同级：

```text
<项目根>/.index/.knowledge.cache/stages/<stage>/<fingerprint>.json
```

放在同级而不是索引目录里面，是因为 `.index/knowledge/` 要保持读取方期望的形状，而且首次构建失败时不能留下半个索引目录。丢掉缓存只损失重算时间。

### 重试语义

```python
run_pipeline(project_root, index_directory, retry_stages=["layout"])
```

- 重试的 stage 及其全部下游重新执行，产生新的 Stage Attempt（`attempt_number` 递增），不覆盖历史。
- 不受影响的 stage 以 `reused` 形式携带上一轮的 attempt id 前进，不重新执行，也不假装执行过。
- attempt id 由 `run_id + stage + 文档 + 序号` 推导，所以重试行永远不会撞掉旧行。

## 能力包与降级链

三类本地能力包（`capabilities.py`），都可以不装：

| 包 | 层次 | 可选 | 内容 | 声明规模 |
|---|---|---|---|---|
| `core` | core | 否 | OpenCV 几何、RapidOCR（ONNX Runtime）、项目自带 OOXML 解析 / SQLite-FTS / 原子发布 | 下载 ~420 MB，安装 ~1.15 GB |
| `enhanced_ocr` | enhanced | 是 | PaddleOCR、PaddlePaddle、PaddleX / PP-StructureV3 | 下载 ~980 MB，安装 ~4.7 GB |
| `visual` | visual | 是 | Ollama + Qwen2.5-VL 3B，仅做粗粒度图片解释 | 下载 ~3.2 GB，安装 ~3.4 GB |

每个包都声明用途、许可证、CPU / 内存 / 磁盘下限、体积与空闲超时；`detect_pack()` 逐项检查并留下产生该结论的每一条 check。包状态到 stage 执行状态的映射只有一处（`PACK_TO_EXECUTION_STATUS`），保证降级上报不会和检测结果漂移：

| 包状态 | Stage 执行状态 |
|---|---|
| `available` | `succeeded` |
| `degraded` | `partial` |
| `not_installed` | `unavailable` |
| `insufficient_resources` | `unavailable` |
| `self_check_failed` | `failed` |

### OCR 降级链

```text
rapidocr (core, ONNX Runtime)
    -> not installed / models missing
paddleocr (enhanced, 显式安装)
    -> not installed / missing language pack
tesseract (compatibility, V1 behaviour)
```

- 链上每个引擎都会留下一条 `reason_chain` 记录：请求了谁、是否可用、版本、被跳过的原因。
- `tesseract` 是兼容性回退，不是等价默认值；`allow_compatibility_fallback=False` 可以显式关掉它，此时宁可 `unavailable` 也不用低一档的能力冒充。
- 走了回退就是 `partial` + `fallback_used=True`，绝不会报成干净的成功。
- 请求的引擎优先：显式指定 `rapidocr` 时先试它，失败再按链下钻；`succeeded`、`timeout`、`corrupt_image`、`unsupported_format` 立即停止，不再换引擎重试。

单张图的引擎尝试记录在 `ocr_runs.reason_chain` 与 `attempts` 里；region 的几何、原文、三类置信度和规范化建议见 [`ocr.md`](ocr.md) 与 [`data-model.md`](data-model.md)。

### 模型安装与“不静默下载”

`ModelStore` 只做三件事：从本地目录拷贝、按 pin 校验和验证、删除。类里没有任何下载路径，所以“不静默下载模型”是代码性质，不是文档承诺。安装前必须 `confirmed=True`，不确认只返回预览；校验和不符直接报错，不会写入。

`visual` 包的模型 pin（运行时 / 模型 / 量化）随包发布，其 SHA256 就是对 `visual_model_pin_bytes()` 实际字节取的哈希。

完全离线的做法（预下载 wheel 与模型、bundle 校验、安装、卸载、Profile 资源预算与运行记录）见 [`capabilities.md`](capabilities.md)：那里把“本机能不能装”这件事独立成一次可复查的诊断，而不是散落在安装脚本里。

## 驻留与资源预算

`CapabilityRuntime` 负责加载与释放：

| 触发 | 行为 |
|---|---|
| 批次内重复 `acquire` | 复用已加载的 handle，不重复加载 |
| `idle_timeout` 到期 | `sweep()` 或下一次 `acquire` 时释放，超出阈值的包逐个卸载 |
| Low-memory Mode | 任一包空闲即释放，批次结束时 `end_batch()` 全部卸载 |
| 显式 `release` / `release_all` | 立即释放 |
| 进程退出 | `atexit` 钩子释放全部驻留（Windows 同样生效） |

包状态变化（例如安装中途变化）会让已加载的 handle 失效并重新加载，而不是继续用一个和当前检测结果不符的实例。

`capabilities` CLI 子命令和 `capability_status` MCP 工具都能打印硬件画像、推荐 profile、每个包的检查明细与当前驻留情况；策略字段固定声明：不自动下载、无遥测、无后台扫描。

## 运行清单

每次运行都会留下 `ProcessingManifest`（运行清单），并被写入已发布的索引（`processing_manifests` + `stage_attempts` 两张表）。它能回答：

- 这次用的是哪个运行时、哪个引擎版本、哪个模型与量化；
- 哪个 stage 走了降级链、链上每一步为什么被跳过；
- 生效配置、profile、并发上限与低内存开关；
- 每个 stage 的输入 / 输出哈希、耗时、覆盖范围、归属工单。

读取方式是 `index_status.processing`（摘要）或直接查 `stage_attempts` / `processing_manifests`。

## 与其他文档的关系

- 修订身份与快照发布：[`revisions.md`](revisions.md)
- SQLite 表结构：[`data-model.md`](data-model.md)
- 部署与重建流程：[`setup.md`](setup.md)
