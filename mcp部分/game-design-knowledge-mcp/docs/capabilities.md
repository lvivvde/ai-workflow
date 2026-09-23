# Windows 离线能力包：安装、校验、卸载与排障

本文说明如何在**全新 Windows 10/11** 上从预下载的离线包完成 Core / Enhanced OCR / Visual 能力包部署，并留下可核对的运行记录。前置依赖与索引流程见 [`setup.md`](setup.md)，处理语义与降级链见 [`processing.md`](processing.md)。

三条不变量贯穿全文：

- **不联网**：本项目没有下载路径。安装只从本地离线包拷贝与安装，索引与查询期间也不访问网络。
- **版本锁定**：每个包的 Python 依赖与模型文件都按精确版本 + SHA256 固定，清单见 [`../capabilities/manifests`](../capabilities/README.md)。
- **可选能力可删**：删掉任何可选能力只损失它提供的召回或转录，事实库、FTS5 与核心工具不受影响，也不需要重建索引。

## 1. 能力包与清单

| 包 | 层次 | 可选 | 内容 | 声明体积 |
|---|---|---|---|---|
| `core` | core | 否 | OpenCV、RapidOCR（ONNX Runtime）+ 项目自带 OOXML / SQLite-FTS / 原子发布 | 下载 ~420 MB，安装 ~1.15 GB |
| `enhanced_ocr` | enhanced | 是 | PaddleOCR、PaddlePaddle、PaddleX（PP-StructureV3） | 下载 ~980 MB，安装 ~4.7 GB |
| `visual` | visual | 是 | Ollama + Qwen2.5-VL 3B，只做粗粒度图片解释 | 下载 ~3.2 GB，安装 ~3.4 GB |

`capabilities/manifests/<包名>.json` 是这些声明的固化副本（含每个 Python 依赖的精确版本、模型文件的相对路径与 SHA256）。它们由代码生成，测试逐字节比对，因此不能手工篡改：

```powershell
uv run game-design-knowledge capability manifests --write capabilities\manifests
```

## 2. 离线包格式

离线包是一个目录，在一台能联网的机器上准备好后整体拷入目标机器：

```text
<bundle>/
├── bundle.json
├── wheels/<包名>/<文件名>.whl
└── models/<包名>/<相对路径>     # 与模型仓库内的相对路径一致，例如 qwen2.5-vl-3b/pin.json
```

`bundle.json`（格式定义见 [`../capabilities/bundle.schema.json`](../capabilities/bundle.schema.json)）：

```json
{
  "bundle_version": 1,
  "platform": { "os": "windows", "architecture": "AMD64", "python": "3.12" },
  "packs": {
    "visual": {
      "version": "1.0.0",
      "wheels": [
        {
          "distribution": "ollama",
          "version": "0.3.3",
          "filename": "ollama-0.3.3-py3-none-any.whl",
          "sha256": "<64 位十六进制>"
        }
      ],
      "models": [
        {
          "relative_path": "qwen2.5-vl-3b/pin.json",
          "sha256": "<与 capabilities/manifests/visual.json 一致>"
        }
      ]
    }
  }
}
```

包内声明必须与本构建的 pin 完全一致：少一个 wheel、多一个 wheel、版本不同、模型文件哈希不同都属于 `bundle_pin_mismatch`，会被拒绝而不是"尽力安装"。

**bundle 必须携带整个 wheel 闭包。** 清单里的 `python_artifacts` 是包负责的东西（有归属、会随卸载列出），`python_dependencies` 是它们运行所需的传递依赖（有 pin，但不归这个包所有，也不随卸载移除）。离线安装的最后一步是 `pip install --no-index ... --require-hashes`，hash-checking 模式要求**每一个要安装的 distribution** 都带着哈希出现在 requirements 里，所以闭包必须一起声明、一起携带、一起校验：少一个成员，安装会在中途失败；多一个没声明的 wheel，`verify` 会拒绝。requirements 文件由 bundle 的 `wheels` 逐条生成，因此"装上一个包"意味着"装完它声明的整个闭包"。

## 3. 从离线包安装

把离线包拷到目标机器后按下面顺序执行（示例为 `visual`，替换 `--pack` 即可装 `core` 或 `enhanced_ocr`）：

```powershell
# 1) 先诊断：能力包状态、磁盘、Tesseract 兼容回退与语言包、模型仓库路径
uv run game-design-knowledge capability doctor --bundle D:\offline\gdk-bundle

# 2) 预览：只校验与列出将执行的动作，不写任何文件
uv run game-design-knowledge capability plan --bundle D:\offline\gdk-bundle --pack visual

# 3) 安装：拷贝模型文件并生成离线 requirements（默认不执行 pip）
uv run game-design-knowledge capability install --bundle D:\offline\gdk-bundle --pack visual --confirm

# 4) 需要时一并安装 Python 依赖（pip 以 --no-index --only-binary --require-hashes 运行）
uv run game-design-knowledge capability install --bundle D:\offline\gdk-bundle --pack visual --confirm --apply-python

# 5) 复核：逐个 wheel 与模型文件对 pin 校验
uv run game-design-knowledge capability verify --bundle D:\offline\gdk-bundle --pack visual
```

要点：

- 模型文件落在模型仓库（默认 `%LOCALAPPDATA%\game-design-knowledge\models`，可用 `GAME_DESIGN_MODEL_DIR` 覆盖），**不在**项目数据目录里，删除它永远不会删除文档、事实或词法索引。
- 安装必须带 `--confirm`；不带时只返回 `confirmation_required` 预览。
- 离线 pip 命令固定为 `pip install --no-index --find-links <bundle>\wheels\<包名> --only-binary :all: --require-hashes -r <模型仓库>\<包名>\offline-requirements.txt`，没有任何可回退到网络的选项。
- 平台或 Python 不匹配（例如把 `windows`/`AMD64`/`3.12` 的包装到别的平台或 3.11）会报 `bundle_platform_mismatch` / `bundle_python_mismatch` 并停止。

- `--apply-python` 用当前解释器执行上面那条 pip 命令。`uv venv` 建出来的虚拟环境默认**不带 pip**：`capability plan` 仍是 `ready`，安装则会如实返回 `python_install_failed`；补救见第 8 节的 `python_install_failed` 条目。

## 4. 卸载

```powershell
# 预览
uv run game-design-knowledge capability uninstall --pack enhanced_ocr
# 执行
uv run game-design-knowledge capability uninstall --pack enhanced_ocr --confirm
```

卸载只删除该包在**模型仓库**里的目录。返回的 `facts_untouched`、`lexical_index_untouched` 都是 `true`，`requires_reindex` 为 `false`：项目事实与词法索引不在删除范围内。Python 依赖不会被悄悄卸载；需要时按返回的 `python_artifacts.uninstall_command` 自行执行。

## 5. 资源预算与运行记录

三种 Hardware Profile 的并发与驻留默认值（唯一来源是 `run_records.PROFILE_BUDGETS`，`capability status` 与 `index_status.processing.limits` 读同一份）：

| Profile | OCR 并发 | 重任务并发 | OCR 批大小 | 空闲超时 | 向量召回 | 视觉模型 |
|---|---|---|---|---|---|---|
| `baseline` | 1 | 1 | 4 | 120 s | 关闭 | 不安装 |
| `recommended` | 2 | 1 | 8 | 300 s | 关闭 | 不安装 |
| `visual` | 2 | 1 | 8 | 300 s | 关闭 | 按需加载 |

每个 Profile 都能生成一条运行记录：

```powershell
uv run game-design-knowledge capability baseline --profile baseline --index-dir .index\knowledge --output .baseline\windows\baseline.jsonl
```

记录是一行 JSON（`capability-run-v1`），至少包含：

| 字段 | 含义 |
|---|---|
| `latency_seconds` | 本次测量的墙钟耗时 |
| `peak_memory_bytes` / `peak_memory_mb` | 进程峰值工作集（Windows 取 `PeakWorkingSetSize`） |
| `memory_bytes_start` / `memory_bytes_end` | 起止工作集 |
| `disk.before` / `disk.after` | 模型仓库或索引目录所在卷的空闲/已用空间 |
| `degradation_events` | 每个不可用能力包与 OCR 降级链的原因（`channel` / `reason` / `detail`） |
| `budget` | 该 Profile 生效的并发与驻留预算 |
| `hardware` / `platform` | 本机画像与平台信息（记录只对本机成立） |
| `notes` | 逐包状态、已加载能力、OCR 选择与查询结果 |

> 正式验收记录必须来自 Windows 10/11 原生环境。macOS 或 WSL 上的结果只是那一台的证据，不作为 Windows 发布门槛。

## 6. 一键验收脚本

```powershell
.\scripts\windows-smoke.ps1
```

脚本依次检查前置依赖、输出 `capability doctor`、重建共享索引（`-SkipIndex` 可跳过）、通过 stdio 跑 MCP smoke test，然后为三个 Profile 各追加一条运行记录到 `.baseline\windows\<profile>.jsonl`。缺少能力包只会让记录里多出降级事件，不会让脚本失败。

## 7. Windows 原生验收清单

| 项目 | 怎么验证 | 期望 |
|---|---|---|
| 全新环境离线安装 | 拷入离线包后 `capability doctor` → `plan` → `install --confirm` → `verify` | 每一步都有明确状态；`verify` 全项通过 |
| 运行期间无网络 | 上述命令与 `capability baseline` 全程不访问网络；离线 pip 命令带 `--no-index`/`--only-binary`/`--require-hashes` | 没有下载、没有索引回退 |
| 模型按需驻留与释放 | `capability status` 的 `residency`；`--low-memory` 或空闲超时后 `packs` 为空 | 只在处理期间驻留，批次结束或超时后释放 |
| 文件占用 | 用其它进程打开模型文件或索引数据库后执行安装/卸载/重建 | 显式报错（如 `capability_removal_failed`），active snapshot 与持久状态不被破坏 |
| 快照指针切换 | 重建期间旧索引持续可读；崩溃后 `CURRENT.json` 仍指向已验证快照 | 未校验快照永不成为 active |
| Unicode / 空格 / 盘符路径 | 把 `GAME_DESIGN_MODEL_DIR` 指到含中文与空格的路径（如 `D:\游戏 项目\models`）后重跑安装与基线 | 全部通过；`doctor.paths` 里 `ascii` 只是提示项 |
| 进程退出 | 调用一次能力包后直接结束进程 | `atexit` 释放全部驻留，无残留后台进程 |
| 磁盘不足 | 把模型仓库指到小容量卷后 `capability doctor` | 报 `disk_too_small` 并给出还差多少 GB，不尝试安装 |
| 卸载可选能力 | `capability uninstall --pack visual --confirm` 后查询事实与词法 | `search_evidence` 结果不变，`index_status.is_stale` 仍为 `false` |

## 8. 排障

### `bundle_missing` / `bundle_manifest_invalid`

离线包目录不存在，或缺 `bundle.json`、JSON 不合法、`bundle_version` 不是 `1`、缺 `platform.*` / `packs`、某个包缺 `version` / `wheels` / `models`。按 [`bundle.schema.json`](../capabilities/bundle.schema.json) 修好再重试。

### `bundle_platform_mismatch` / `bundle_python_mismatch`

离线包不是给本机（`os` / `architecture`）或当前解释器（`python` 主次版本）准备的。在目标平台重新准备离线包；不要试图绕过，wheel 的 ABI 标签并不兼容。

### `bundle_artifact_missing` / `bundle_checksum_mismatch`

包内少了 wheel 或模型文件，或文件被替换/截断。`capability verify` 会逐条列出文件名、实际 SHA256 与 pin。重新拷一份完整离线包。

### `bundle_pin_mismatch`

离线包与本构建的 pin 不一致：少声明、多声明、版本不同或模型哈希不同。先确认两边版本（`capability manifests` 与 `bundle.json`），再重新准备离线包；升级代码后需要重新生成离线包。

### `capability_removal_failed`

Windows 上有进程仍占用模型文件。关闭占用进程（例如仍在运行的 OCR/视觉进程）后重试；报错会列出仍在原地的文件，事实库与索引不受影响。

### `tesseract_missing` / `tesseract_language_pack_missing`

`capability doctor` 的 `tesseract` 段会给出可执行文件路径、版本、已装语言与缺失语言。Tesseract 是**兼容回退**，不是等价默认值：默认需要 `chi_sim+eng`（可用 `GAME_DESIGN_OCR_LANG` 覆盖）。缺少语言包时它只会转写对应语言很糟，所以本项目宁可报降级也不假装成功。

### `disk_too_small`

`doctor.disk.packs.<包名>` 给出该卷空闲空间、包声明的下限与还差多少 GB。清理磁盘或把 `GAME_DESIGN_MODEL_DIR` 指到更大的卷。

### `model_root_not_absolute` / `model_root_not_writable` / `model_root_path_long`

模型仓库路径必须是绝对路径、可写，且长度不超过 200 字符（更长需要启用 Windows 长路径）。含中文或空格是允许的，`doctor.paths` 会把 `ascii` 标为提示项而不是失败项。

### 构建期报模型缺失（`models_missing`）

当前 pin 的 `rapidocr-onnxruntime==1.3.24` 自带 ONNX 模型，`core` 包因此不声明任何 `model_artifacts`：装上 wheel 就能转录，不需要再放模型文件（这一点由 Windows 实跑确认）。只有当引擎自己的模型文件确实不在（例如安装被裁剪、或 `GAME_DESIGN_OCR_MODEL_DIR` 指到空目录）才会报 `models_missing`，见 [`ocr.md`](ocr.md)。本项目在任何阶段都不会下载模型。

### `python_install_failed`（`No module named pip`）

解释器里没有 pip：`uv venv` 建的虚拟环境默认不装 pip，而离线安装的最后一步要由它执行 pip。先 `uv pip install pip`（或用带 pip 的解释器重跑），其它步骤不用重做——bundle、已写出的 requirements 与模型仓库都还在原处。

### `enhanced_ocr` 装不上：pin 指向不存在的版本

该包声明的 `paddlex==2.4.4` 在当前索引里不存在（只有 2.1.0 与 3.x），因此它的闭包无法解析，bundle 也就无法为它生成完整 requirements。这是 pin 的问题，不是操作问题：修它等于决定 Enhanced OCR 面向哪一代 PaddleOCR/PP-StructureV3，属于产品决策。`core` 与 `visual` 两个包的闭包已固定，可正常离线安装。

## 9. 与其他文档的关系

- 部署与共享索引：[`setup.md`](setup.md)
- 阶段、降级链与驻留：[`processing.md`](processing.md)
- OCR 契约：[`ocr.md`](ocr.md)
- 能力包清单与离线包格式：[`../capabilities/README.md`](../capabilities/README.md)
