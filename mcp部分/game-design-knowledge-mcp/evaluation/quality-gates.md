# Quality Gates

发布门槛不是「一个准确率数字过线」，而是一组**逐层条件**：每条条件点名一层、一个指标、一个绝对边界，以及支撑它所需的最少标注样本数。层与层之间不做加权，也不产出混合总分；这是准确率评测协议的核心约束，门禁只是把它变成可判定的是/否。

门槛定义在 [`quality-gates.json`](quality-gates.json)（`quality-gates-0.1`），由 [`gates.py`](../src/game_design_knowledge/evaluation/gates.py) 读取与判定，入口是 [`tools/evaluate.py`](../tools/evaluate.py)。

## 1. 文件结构

```json
{
  "version": "quality-gates-0.1",
  "description": "为什么是这些数字",
  "gates": [ { "layer": "retrieval", "metric": "recall_at_limit", "floor": 0.6 } ],
  "regression_limits": [ { "layer": "retrieval", "metric": "recall_at_limit", "max_drop": 0.05 } ],
  "notes": [ "读这份门槛时要记住的话" ]
}
```

`gates` 与 `regression_limits` 都可以为空列表，但 `gates` 不允许缺失或为空——一份没有任何条件可判的门禁不是门槛。

## 2. 单条门槛的字段

| 字段 | 必填 | 含义 |
|---|---|---|
| `layer` | 是 | 10 层之一，取值必须属于 `EVALUATION_LAYERS` |
| `metric` | 是 | 该层指标名，必须是这层真的会发布的键 |
| `floor` | 与 `ceiling` 二选一 | 下限（小于即失败） |
| `ceiling` | 与 `floor` 二选一 | 上限（大于即失败） |
| `samples_at_least` | 否（默认 1） | 判定所需的最少标注样本数 |
| `mode` | 否 | 只判某个模式（`component` / `e2e`）；不写则每个模式各判一次 |
| `error_class` | 否 | 失败归因；不写则用该层的默认归因（见表 3） |
| `unavailable_class` | 否（默认 `environment`） | 这层不可测量时算哪一类问题 |
| `note` | 否 | 失败信息里会带上的一句话 |

指标的读取方式是「按人读报告的方式读」：`rate`/`f1` 这类映射取其中的比率，`precision`/`recall`/`f1` 三元组取 `f1`，只有 `matched`/`expected` 的覆盖率取两者之商；键不存在或不是数字就是**没有这个指标**（失败原因写「这层没有发布该指标」，而不是悄悄算成 0）。门禁只点名词表里真实发布的键：映射内部的分量（如区域完整率里的 `precision`）不是可选指标名，想卡它就要先让这一层把该分量单独发布出来。

## 3. 判定顺序

每条门槛按固定顺序判定，先失败先定因：

| 顺序 | 情况 | 结论 |
|---|---|---|
| 1 | 整轮运行完全没有报告这一层 | 失败，归 `environment`（「运行根本没跑这一层」） |
| 2 | 这一层报告为 `unavailable` / `partial` / 其它非 `measured` | 失败，归 `unavailable_class`（默认 `environment`），并在原因里带上该层自己给的说明 |
| 3 | 已测量但样本数少于 `samples_at_least` | 失败，归 `annotation`（缺的是标注，不是实现） |
| 4 | 已测量但没发布这条指标 | 失败，归 `annotation`（指标得先被发布才谈得上卡阈值） |
| 5 | 指标越界 | 失败，归该层默认归因或 `error_class` |
| 6 | 以上都不成立 | 通过 |

**不可测量不是通过**：任何非 `measured` 的层都会失败，只是失败归到环境或标注类，而不是被折进绿灯。

## 4. 错误分类

每条失败都带一个分类，后续工单按分类拆，而不是按症状拆：

| 分类 | 含义 |
|---|---|
| `data` | 语料或索引数据本身的问题（文档没进索引、两轮不可比） |
| `annotation` | 标注不够或不够清楚（样本太少、指标没被发布） |
| `ocr` | 转写质量（CER/WER、关键标记） |
| `layout` | 区域与阅读顺序 |
| `relation` | 结构关系 |
| `notation` | 记法解读 |
| `retrieval` | 检索召回与排序 |
| `fact` | 事实边界与冲突保留 |
| `explanation` | 释义覆盖与缺口披露 |
| `performance` | 延迟、内存、磁盘与降级 |
| `environment` | 这台机器缺能力（没有 OCR 引擎、没有视觉模型） |

默认归因写在 `gates.DEFAULT_ERROR_CLASSES`，逐层一条；每层都有默认值，新增层时若不补默认值会落到 `data` 上，测试会盯着这张表与 `EVALUATION_LAYERS` 对齐。

## 5. 相对回退限制

绝对下限回答「够不够好」，相对限制回答「有没有变差」：

```json
{ "layer": "retrieval", "metric": "recall_at_limit", "max_drop": 0.05 }
```

规则：

- 只在**显式传入 `--baseline <上一轮 run.json>`** 时判定。没有基线时报告会写明「未判定」，而不会当作通过；
- 两侧都必须以 `measured` 状态报出同一个 `layer` + `mode`；一侧没有数字，这条限制就失败（`data` 类），因为「这轮没测」不能算「这轮没退」；
- 这一轮该层报 `unavailable` 时归 `environment`，与绝对下限的归因一致；
- 比较的是同一档 Hardware Profile 的前后两轮；跨档位比数字没有意义，不要把 `recommended` 的分数当 `baseline` 的基线。

## 6. 后续任务怎么拆

失败时 `tools/evaluate.py` 在 stderr 逐条打印：

```text
quality gates: FAILED
  [environment] environment: ocr_transcription.cer — the layer is unavailable: ...
  [retrieval] retrieval: retrieval.recall_at_limit — recall_at_limit=0.4200 over 6 sample(s) is below the floor 0.6
```

`run.json` 同级也保留同样内容（`gates` 段与 `followups` 列表），一条失败对应一条可分配的任务；一类失败对应一个工单，而不是「修复评测」这种无从下手的标题。

## 7. 用法

```powershell
# 默认就会判门禁
uv run python tools/evaluate.py --corpus evaluation/corpora/development_set

# 与上一轮比较相对回退限制
uv run python tools/evaluate.py --baseline evaluation/runs/<上一轮>/run.json

# 只出分层报告，不判门禁（调试语料时用）
uv run python tools/evaluate.py --no-gates --no-artifacts

# 换一套门槛（例如私有语料自己的门槛）
uv run python tools/evaluate.py --gates D:\controlled-eval\gates.json
```

退出码：invariant 违规或任一门槛失败都是 `1`；`--no-gates` 时只由 invariant 决定退出码。

## 8. Windows 无 OCR 机器上的边界

首轮 Windows 基线在**没有安装核心 OCR 引擎**的机器上跑，`ocr_transcription` 层报 `unavailable` 与 `no_usable_engine`，于是 `cer` / `critical_token_coverage` 两条门槛必然失败，归 `environment`。这是预期行为，不是评测坏了：

- 门槛不会被跳过，也不会被折成通过，报告里始终是「这一层这轮没有测」；
- 装了核心 OCR 引擎（Tesseract 或离线 RapidOCR 模型）后再跑同一份门槛，这两条才会真正被判定；
- 因此首次基线的结论是「哪些层被真正测了、哪些层只能等装好引擎」，而不是「质量已达标」。

## 9. 阈值怎么来的

首轮门槛由首轮基线（`evaluation/runs/`）的实测与样本量共同决定：

- 先看**样本量**：标注样本只有个位数的层，下限不敢定得尖锐，`samples_at_least` 优先于小数位；样本不够就是 `annotation` 类工单，而不是把阈值调低；
- 再看**置信区间**：比率类指标带 Wilson 区间，区间宽就说明样本少，阈值只能落在区间之外才有区分力；
- 不可测量的层不设「临时阈值」：它们要么被测量，要么保持 `unavailable` 并归到环境/标注类。

调门槛是显式动作：改 `quality-gates.json`、写清理由（`description` / `note`）、重跑基线，并把旧报告标为作废。偷偷放宽阈值来让构建变绿，比失败更糟。

调阈值只看 **Development Set 与 V1 Compatibility Corpus**：Golden Set 是 held-out 检查集，它的实测数字不参与定阈值，也不在改阈值时被当作依据。首轮门槛是在汇总三份语料的同一次基线运行上取的整数值（`evaluation/runs/`），因此这条纪律从**下一轮**起是硬约束：以后改 `quality-gates.json` 前先用 Development Set + V1 语料重跑一遍，再拿 Golden Set 复核结论是否仍然成立。
