# 区域级 OCR：转录、置信度与事实边界

V1 的图片处理只回答一个问题：“这张图上有没有字”。V2-04 之后，图片处理要回答的是：**哪些区域**上有字、每个区域的**原文**是什么、建议怎么规范化、以及这份结果**算哪种事实**。

这份文档描述五件事：分层输出契约、三类置信度、Critical Transcription Tokens、状态映射表、以及 V1 兼容边界。

代码分布：

| 关注点 | 模块 |
|---|---|
| 区域、置信度、状态映射 | `src/game_design_knowledge/ocr_regions.py` |
| 规范化建议与关键标记评分 | `src/game_design_knowledge/ocr_normalization.py` |
| 引擎适配、降级链、超时 | `src/game_design_knowledge/ocr.py` |
| 落库与检索投影 | `src/game_design_knowledge/indexer.py` |

## 分层输出契约

一张图上的文字会以三层形态同时保留，**上层永远不覆盖下层**：

| 层 | 内容 | 存储 |
|---|---|---|
| 1. Raw Transcription | 引擎原样吐出的文字，逐区域保存 | `ocr_regions.text_raw` |
| 2. Normalization Suggestion | 建议的规范化文本 + 逐 span 变更记录 | `ocr_normalizations.normalized_text` / `changes` |
| 3. 检索用的扁平文本 | 由 raw 拼出的整图文本 | `images.ocr_text`（V1 字段） |

规范化的产物是**建议**，不是替换：`NormalizationSuggestion.raw` 始终等于输入原文，`normalize_transcription()` 不会写回 raw。需要规范化文本时读 `normalized_text`，需要“引擎到底写了什么”时读 `text_raw`，两者可以并列展示，也可以被独立评分。

每条建议都带逐 span 变更：

```text
SpanChange(rule, start, end, before, after, reason, confidence, evidence)
```

`apply_changes()` 会把变更回放到 raw 上；回放结果与 `normalized_text` 不一致就抛 `NormalizationError`，所以“建议里写的”和“建议声称改的”不可能悄悄漂移。规则集有版本号 `NORMALIZATION_RULESET_VERSION = "transcription-normalization-v1"`，规则清单是 `RULE_NAMES`：

| 规则 | 做什么 |
|---|---|
| `fullwidth_ascii` | 全角 ASCII / 全角空格转半角 |
| `percent_spacing` | `30 %` → `30%` |
| `cjk_spacing` | 去掉中日韩文字之间、以及 CJK 标点前的多余空格 |
| `whitespace_collapse` | 折叠连续空白 |
| `line_edge_trim` | 去掉每行首尾空白 |

## 三类置信度，没有总分

每个区域分别记录三个置信度，语义不同，不互相折算：

| 字段 | 含义 |
|---|---|
| `text_confidence` | 引擎对这段文字本身的置信度 |
| `region_confidence` | 引擎对“这块区域确实是一个文字块”的置信度 |
| `key_mark_confidence` | 该区域是否命中了关键标记 |

`CONFIDENCE_KINDS` 是这三个字段的唯一声明处。**任何**提交给下游的 OCR 载荷都不允许出现聚合总分：`assert_separate_confidence()` 会检查载荷里是否出现 `AGGREGATE_CONFIDENCE_KEYS`（如 `confidence`、`score`、`overall_confidence`）这类单一分数键，出现即报错。

`key_mark_confidence()` 取**含关键标记的区域里最小的 `text_confidence`**，而不是平均值：一页里只要有一个关键标记读得不可靠，整体关键标记可信度就应该被压低，平均值会把这种风险摊平掉。

区域级的 `key_mark_confidence` 由 `region_key_mark_confidence()` 决定：引擎自己报了关键标记分数就用它的，否则用**确实带有关键标记的那个区域的 `text_confidence`** 代替；不带关键标记的区域不参与，也就不会把这一层抬高。

## Critical Transcription Tokens

数字、百分比、ID、运算符、箭头和否定词是最容易被 OCR 读错、也最容易改变语义的内容，所以单独提取、单独评分：

| kind | 例 |
|---|---|
| `number` | `-3.5`、`1,200` |
| `percent` | `30%` |
| `identifier` | `ITEM_ID_01` |
| `operator` | `+`、`×`、`>=` |
| `arrow` | `->`、`→`、`<--` |
| `negation` | `不`、`未`、`not`、`without` |

提取按顺序认领 span，避免重叠误判：`-3.5` 里的负号算数字的一部分而不是运算符，`ITEM_ID_01` 看作一个标识符而不是被下划线拆开。比较之前先做 `canonical_token()`（NFKC 折叠、去空白、`->` 归一成 `→`），这样“同一件事的两种写法”不会被算成错。

`score_critical_tokens()` 返回**逐 kind** 的分数，而不是一个总体准确率：某个 kind 的参考里一个都没有时，该 kind 的 `accuracy` 是 `None` 而不是 `0` 或 `1`——没有样本就不该伪造一个分数。导出载荷 `critical_token_payload()` 显式带 `"aggregate": False`。

## 状态映射表

引擎返回的是**发生了什么**（provider status），索引里记的是**这次处理算成功还是失败、结果算不算可用**（execution status / quality status）。这张表只有一处实现（`classify_outcome()`），所以降级上报不会和实际行为漂移。

| provider status | execution | quality | reason_code | V1 `ocr_status` |
|---|---|---|---|---|
| `succeeded` + 过门槛 | `succeeded` | `accepted` | — | `succeeded` |
| `succeeded` + 未过门槛 | `succeeded` | `rejected` | `no_text_detected` / `below_quality_threshold` | `succeeded` |
| `timeout` + 有残留文本 | `partial` | `uncertain` | `ocr_timeout` | `succeeded` |
| `timeout` 无文本 | `failed` | `rejected` | `ocr_timeout` | `failed` |
| `failed` + 有残留文本 | `partial` | `uncertain` | `engine_error` | `succeeded` |
| `failed` 无文本 | `failed` | `rejected` | `engine_error` | `failed` |
| `corrupt_image` | `failed` | `rejected` | `corrupt_image` | `failed` |
| `unsupported_format` | `failed` | `rejected` | `unsupported_image_format` | `failed` |
| `missing_language` | `unavailable` | `rejected` | `missing_language_pack` | `unavailable` |
| `models_missing` | `unavailable` | `rejected` | `models_not_installed` | `unavailable` |
| `unavailable` | `unavailable` | `rejected` | `no_usable_engine` | `unavailable` |

几条刻意的选择：

- **部分输出不算 accepted。** `partial` 永远配 `uncertain`：“半页”不等于“这一页”。
- **有残留文本 ≠ 成功。** 超时和引擎报错只要吐出了文字，就保留这些区域并标 `partial`，而不是把已经拿到的内容丢掉；但也不会报成干净的成功。
- **可用性问题和内容问题分开。** 缺语言包、缺模型、没有可用引擎属于 `unavailable`（环境问题，装了就可能有结果）；损坏图片、格式不支持属于 `failed`（这张图本身不行）。两者的修复建议不同，`corrective_action` 会分别给出。
- **`retryable` 只给真的值得重试的。** 超时和引擎报错可以重试；缺模型、语言包缺失、图片损坏重试没有意义。

质量门槛由 `QualityGate` 声明，默认值是一份显式默认而不是隐藏常量：

```text
min_text_confidence = 0.5
min_key_mark_confidence = 0.5
min_characters = 1
```

未通过时 `gate_failures` 会列出具体是哪一条没通过，`corrective_action` 告诉人该怎么办。

构建报告与 `index_status` 里的计数口径（`low_quality_images` / `ocr_low_quality`）只统计**引擎跑出来了、但没过门槛**的图片：`reason_code = below_quality_threshold`。没有可用引擎（`unavailable`）、图片损坏（`failed`）是另外的类别，各自有自己的计数，不会混进“低质量”里。

## 事实边界

OCR 结果默认停在 `transcription` 或 `machine-supported`：

- `machine-supported`：引擎成功跑完且过了质量门槛。
- `transcription`：其余所有情况，包括部分输出和被拒绝的输出。

`assert_transcription_boundary()` 会拒绝任何试图把 OCR 结果标成 `explicit` 或 `verified` 的载荷。机器读出来的字不能自动升级成“项目已确认的事实”，这条边界和 [`evidence-policy.md`](evidence-policy.md) 一致。

## 降级链与“不静默下载”

```text
rapidocr (core, ONNX Runtime)
    -> paddleocr (enhanced, 需显式安装)
    -> tesseract (compatibility, V1 行为)
```

`select_engine()` 会走完整条链并记录每一步的 `engine`、`tier`、`pack`、`available`、`usable`、`version`、`detail`，所以“为什么用的是这个引擎”可以从运行清单里读出来，而不是靠猜。`prefer` 指定请求的引擎，`allow_compatibility_fallback=False` 显式关掉兼容回退（宁可 `unavailable` 也不用低一档的能力冒充）。

单张图的实际尝试 `run_image_ocr()` 另一套语义：

- 先试请求的引擎；`unavailable`、`failed`、`missing_language`、`models_missing` 这几种情况会继续往下一级试，`succeeded`、`timeout`、`corrupt_image`、`unsupported_format` 立即停下——后面这些不是“换个引擎就能好”的问题。
- 每个引擎的尝试都留一条 `attempts` 记录。
- 如果最后一级给出了自己的理由（例如“装了 PaddleOCR 但没装语言包”），就保留那个理由，不压成笼统的 `no_usable_engine`。

RapidOCR 适配器要求模型文件**已经存在**于 `GAME_DESIGN_OCR_MODEL_DIR` 或包内 `models/*.onnx`；找不到就报 `models_missing` 并附上 `this build never downloads models`。适配器里没有任何下载路径——这是代码性质，不是文档承诺。

其它环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `GAME_DESIGN_OCR_MODEL_DIR` | RapidOCR 本地模型目录 | 包内 `models/` |
| `GAME_DESIGN_OCR_LANG` | 语言（Tesseract 形如 `chi_sim+eng`） | `chi_sim+eng` |
| `GAME_DESIGN_OCR_TIMEOUT` | 单张图超时秒数 | `60` |

超时由 `_call_with_timeout()` 用线程池包裹引擎调用实现，超时后返回 `timeout` 而不是挂住整个建索引流程。图片预检 `image_preflight()` 先看签名再决定要不要交给引擎：空文件和“扩展名是 PNG 但签名不对”都算 `corrupt_image`，没有扩展名或格式不在支持列表里算 `unsupported_format`。

## V1 兼容边界

- `images.ocr_status` 仍然只有 V1 的三个取值 `succeeded` / `failed` / `unavailable`，含义不变；V2 的 `partial` 投影到 V1 的 `succeeded`（V1 字段只回答“有没有拿到可用文字”）。
- `images.ocr_text`、`images.ocr_error` 保持原字段，`ocr_error` 只在 `failed` / `unavailable` 时出现。
- 不传 `ocr_engine` / `ocr_providers` 时，索引走 V1 单引擎路径，行为与 V1 一致。
- 旧索引（schema v4）迁移到 v5 后照常可读：`index_status` 与 `get_image_context` 在 OCR 表为空时返回零值或 `None`，不报错，也不编造区域。
- 区域级数据是**追加**的：原有的 `images` 行和 V1 报告字段都不删不改。
