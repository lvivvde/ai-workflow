# Annotation Guide

本文件是标注协议的可执行说明，版本 `eval-guide-0.1`。语料 `manifest.json` 的 `guide_version` 与每个样本的 `annotation.guide_version` 必须等于这个版本号；规则一改就要升版本，并让所有语料重新刷新指纹，因为旧报告的标签已经不是同一套标签。

标注的对象是**样本**（`samples/*.json`），不是索引产物。标注只写「这份材料说的是什么」和「一个正确响应必须做到什么」，不写「当前实现输出什么」，否则 Golden Set 会被调参污染。

## 1. 谁标、标几次

| 层 / 内容 | 标注强度 |
|---|---|
| 普通 OCR 转写、区域与阅读顺序 | 单人标注 + 抽检（第二人对抽样样本复核） |
| 高风险事实、关系、冲突、无答案、释义 | 双人独立标注 + 第三方裁决 |

`annotation` 字段即这套强度：

| 字段 | 含义 |
|---|---|
| `annotators` | 参与标注的人（字符串列表，非空）。两人独立标注时写两个人 |
| `adjudicated` | 第三方裁决是否已完成 |
| `guide_version` | 标注时用的本文件版本 |

高风险样本少于两名标注者或没有完成裁决时，`load_corpus` 直接拒绝整份语料。判定为高风险的条件（与 `Sample.high_risk` 一致）：

- `stratum.conflict_state != "none"`，或 `expected.conflict_group_expected` 为真，或 `expected.conflicts` 非空；
- `expected.response_state != "found"`（含 `not_found`/`partial`/`ambiguous`/`degraded`/`failed`/`stale`）；
- `stratum.notation_complexity != "none"`，或 `expected.notation` 非空；
- `expected.relations` 非空；
- `expected.required_atoms` 非空；
- `expected.transcription` 非空；
- `stratum.content_type` 属于 `flow_notation` / `image_text` / `conflict` / `unknown`。

人工调试时可以在隔离副本上放宽这条规则（`tests/test_evaluation_harness.py` 里的 `allow_unadjudicated_high_risk`），正式加载永远不放宽。

## 2. 样本骨架

```json
{
  "sample_id": "dev-notation-confirmed-arrow",
  "stratum": { "source_type": "docx", "content_type": "flow_notation", "language": "zh",
    "visual_quality": "clean", "structure_complexity": "simple", "notation_complexity": "arrow",
    "conflict_state": "none", "capability_pack": "core", "difficulty": "hard" },
  "documents": [{ "path": "连招与记法.docx" }],
  "tool": "retrieve_evidence",
  "arguments": { "query": "连招流程" },
  "modes": ["component", "e2e"],
  "capabilities": ["core"],
  "expected": { },
  "annotation": { "annotators": ["solo-annotator-1"], "adjudicated": false,
    "guide_version": "eval-guide-0.1" },
  "notes": "为什么这条样本存在"
}
```

`documents` 只写合成/脱敏规格，不携带真实策划资料（见第 8 节）。9 个 stratum 字段全部必填且取值受限（`schema.STRATUM_VALUES`），全部计入样本指纹。

## 3. 标注来源范围

`expected.required_evidence[]` 每项声明一条**必须可回指**的来源：

| 字段 | 含义 |
|---|---|
| `source_document` | 来源文档路径，必须与语料里实际物化的路径一致 |
| `locator_contains` | 定位必须含有的键值（如 `{"paragraph_index": 3}`、`{"cell_reference": "A2"}`），只写人能核对的最小事实 |
| `text` | 该来源必须含有的原文片段（可选，写规范化的最小片段） |

标的是「哪个文档的哪一段/哪个单元格」，不是行号本身：行号会随索引重建变化，段落号与单元格地址不会。

## 4. 标注文字、区域与顺序

有图片文本时用 `expected.transcription`：

| 字段 | 含义 |
|---|---|
| `text` | 参考转写全文，CER/WER 的分母 |
| `critical_tokens` | 数字、单位、ID、运算符、箭头、否定词，必须原样留存（逐 token 比对） |
| `regions` | 标注出的区域文本列表，用来算区域完整率 |
| `reading_order` | 区域应被阅读的顺序（写区域文本） |

标法：

- `critical_tokens` 只标**会改变结论**的字符：`-3.5` 是一个 token，不要拆成 `-`、`3.5`；`→`、`≤`、`!`、`无`、`不` 都要单独标。
- 转写按图上实际字形写，不做同义改写；看不清的字用 `[?]` 占位并在 `notes` 说明，不要凭上下文补字。
- `regions` 按视觉块（标题、正文段、标注框）拆，不按行拆；同一块里的多行合成一个区域文本。
- `reading_order` 只在顺序有唯一答案时标；确实有分支时留空并在 `notes` 写明是哪一步开始分支（这一条会变成后续工单，而不是硬判对错）。

## 5. 标注顺序与关系

`expected.relations[]` 每项声明一条区域之间的结构关系，按**区域文本**引用两端：

| 字段 | 含义 |
|---|---|
| `source` / `target` | 关系两端的区域文本（`source->target` 构成标签） |
| `kind` | 关系类型，默认 `next_step` |

只标能从图上直接读出的顺序（独占箭头、唯一上下端点），分支、连续箭头、缺端点和跨容器关系一律不标成确认关系；要记录就写进 `notes`。关系层按 Precision/Recall/F1 计分，标错了会同时拉低两侧。

## 6. 标注记法含义

`expected.notation[]` 每项声明一个设计师记法符号该怎么被解读：

| 字段 | 含义 |
|---|---|
| `symbol` | 记号原文（如 `→`） |
| `meaning` | 已确认含义（`rejected` 时写被人否决的读法） |
| `status` | `confirmed` / `rejected` / `unknown` |
| `scope` | 含义生效范围：`project` / `document_type` / `document` / `region` |

三种状态的含义是硬的：

- `confirmed`：响应必须解出该含义，并按范围判定（为一个区域确认的含义不得回答同文档其它区域）。
- `rejected`：只能报告为装饰，不得当事实使用。
- `unknown`：必须保持原符号并标 `unknown`，不得猜含义。

范围是标注的一部分：同一符号在两个文档里含义不同时，要写成两条带不同 `scope` 的标注，语料 `manifest.review_seeds` 负责在执行前把人工确认结果复现出来（见第 9 节）。

## 7. 标注可达性、引用、缺口与冲突

| 字段 | 含义 |
|---|---|
| `response_state` | 期望的响应状态（`found` / `not_found` / `partial` / `ambiguous` / `degraded` / `failed` / `stale`） |
| `allowed_answer_set` | 允许出现的答案集合（见下文） |
| `conflict_group_expected` | 是否必须暴露冲突组 |
| `conflicts[]` | 冲突主题与彼此不一致的取值（`topic` + `values`） |
| `required_atoms[]` | 释义必须覆盖的必需 atom（用 atom id 或稳定片段描述） |
| `required_gaps[]` | 必须披露的来源缺口（写缺口句子或 `gap_code`） |

标法：

- **可回答性**：来源确实不能回答时标 `not_found`，不要为了有正例而把答案算进来。这类样本归高风险，要双人标注。
- **Allowed Answer Set**：允许多个措辞等价、信息等价的答案时，把每个可接受答案各写一条（同义表述、单位换算后的等价写法、两种都正确的读法）。集合里**不得**出现需要额外推断才能得到的答案；只要有一个答案需要推断，就改成 `ambiguous` 或 `partial`。
- **引用**：`required_evidence` 里每条引用都要在语料里真实存在，且定位信息足够让人手查。
- **缺口**：来源没写触发方式、没写单位、没写设计目的时，用 `required_gaps` 明确要求响应把这个缺口说出来；缺口是标注对象，不是实现的自由发挥。
- **冲突**：冲突样本必须写 `conflicts[]` 与 `conflict_group_expected=true`，且**不得**指定胜者；任何形式的「谁更新听谁的」都不是标注，而是实现错误。

## 8. 私有资料边界

- 语料只放**合成或脱敏**材料：DOCX/XLSX 由 `generator` 规格在评测时现造；图片文档写 `generator.kind = "png"`（内嵌图片写在 docx 的 `type: "image"` 块上），并用 `asset` + `asset_sha256` 指向语料自带 `assets/` 下已提交的合成 PNG，见第 11 节。
- 公共仓只提交规格、期望、标注与指纹（`sample_fingerprints`），不提交真实策划原文或其派生产物。
- 真实策划材料的评测在本地受控评测仓中进行：把真实语料目录作为 `--corpus` 传入即可，同一套协议与同一套门禁照常生效，但这些目录不入库、不随仓库传播。

## 9. 人工确认结果怎么复现

记法含义、确认别名这类结论存放在持久状态里，不写在样本上。样本需要的确认动作写在语料 `manifest.review_seeds`，评测在跑样本之前把它们逐条交给常规的 `plan_review_action` / `apply_review_action`（预览到应用一步不少），因此运行自己的 journal 会记下这些决定，样本无法区分「种子里复现的确认」和「人工做过的确认」：

```json
"review_seeds": [
  { "action": "confirm", "notation_token": "→", "meaning": "下一步",
    "scope_kind": "project", "authority": "project_dictionary",
    "reason": "合成语料的一次项目级确认，用于在隔离工作区里复现确认字典",
    "actor": "synthetic-reviewer" }
]
```

种子字段就是 review action 的入参：`action`（`confirm` / `correct` / `reject` / `ignore` / `resolve_conflict`）、`notation_token`、`meaning`、`entry_id`、`scope_kind`（`project` / `document_type` / `document` / `region`）、`scope_value`、`document`、`document_type`、`region`、`authority`（`region_legend` / `document_definition` / `document_type_definition` / `project_dictionary`）、`resolution_kind`、`candidate_id`、`reason`、`actor`、`basis`。缺少 `action` 的种子在加载语料时就被拒绝。

这样做法是：评测不读手工改过的状态目录，而是每次从空状态按种子推出来，确认过的语料与没确认的语料不会互相污染。

## 10. 标注完成后会发生的校验

`load_corpus` 会拒绝以下情况，请在提交前先跑一遍本地评测（`tools/evaluate.py --no-artifacts`）：

- stratum 字段缺失或取值不在允许集合内；
- `expected.response_state` 不在允许状态内，或 `allowed_answer_set` 不是字符串列表；
- 高风险样本缺少第二名标注者或第三方裁决；
- 文档路径越出语料目录（绝对路径、盘符、`..`）；
- 图片资源缺失、不是 PNG、越出语料目录，或字节与 `asset_sha256` 不符；
- `manifest.sample_fingerprints` 与样本内容不一致（样本改过就要 `--refresh-manifest`）；
- `review_seeds` 里缺 `action`。

`frozen: true` 的语料（Golden Set）拒绝刷新，除非显式 `--force`；一旦改写已冻结标签，此前所有质量报告作废，必须重跑基线。

## 11. 图片语料：合成图与标注必须逐字一致

随仓语料的图片是**合成的**，但必须是**像素里真有字**的合成图。1x1 占位图会让 `ocr_transcription` / `layout_regions` / `reading_order_relations` 三层永远没有分母：标注写得再全，也只能证明"链路跑通、区域被记录"（这正是 `golden_set@0.2.0` 之前的状态）。

### 11.1 图片怎么来

| 项 | 规定 |
|---|---|
| 存放 | `evaluation/corpora/<split>/assets/<名称>.png`，由语料目录随身携带 |
| 引用 | 独立图片 `{"kind": "png", "asset": "assets/<名称>.png", "asset_sha256": "<sha256>"}`；内嵌图片写成 docx image 块的 `asset` / `asset_sha256` |
| 生成 | `tools/render_corpus_assets.py`（**开发期工具**，需要 Pillow）；它按文本行渲染并打印每张图的 sha256 |
| 校验 | `--check` 只比对不写盘；`tests/test_evaluation_fixtures.py` 会核对「已提交的字节 == pin」「渲染器的行 == 标注的 regions」 |

评测本身不依赖 Pillow：`fixtures.py` 只是把这些 PNG 原样写进工作区，所以语料仍然只用标准库就能物化，`asset_sha256` 让「换了图却忘了改标注」在物化阶段就失败，而不是悄悄变成一份不可复现的成绩单。

### 11.2 箭头只能这么画（实测约束）

独立箭头块是关系层唯一的观测来源：`layout.py` 只在**整块都是箭头字符**时才把它判为箭头，方向取自转写出来的字形。在本机（Windows 11 / RapidOCR 1.3.24，30 多种渲染实测）只有一种画法拿得到这个块：

| 画法 | 引擎结果 |
|---|---|
| `↑↑`（≥2 个上箭头，≥56px，**独占一行**） | `↑↑`，成为独立箭头块 → 产生 `next_step` 关系 |
| 单个 `↑`（40/56/72px） | `1` / `个` → 判为文本，不是箭头 |
| 单个 `↓`、`↓↓`、`⇓⇓`、`↧↧`、`∨∨`、手绘向下多边形 | 整块丢弃，或读成 `↑↑` |
| 独立 `→`、`→→`（含宽间距横向排列） | 整块丢弃，只剩两侧文字 |
| **行内**箭头（`起手→追击`、`起手 ↓↓ 追击`） | 正常转写（conf 0.90–0.9997），但区域是文本块，产不出关系 |

由此定下三条构造规则：

1. **带关系的图**画成自下而上的流程链：`relations` 的 `source` 是下方的块、`target` 是上方的块（箭头朝上，流向下一个在上一行）。
2. **`transcription` 与 `reading_order` 记的是画面自上而下的阅读顺序**，不是流程顺序；流程顺序由 `relations` 表达。两者不一致时在 `notes` 里写明。
3. **带 `relations` 标注的样本，其图必须有独占一行的 `↑↑` 块**；只有行内箭头的图（例如 `起手→追击→收招`）只标 `transcription`，不标 `relations`，并在 `notes` 里说明原因。

第 1、3 条是为了「测产品」而不是「测引擎字形混淆」：把向下箭头画进语料，量到的是引擎把 `↓` 读成 `↑` 这件事，不是管线本身。这类字形缺陷另开工单跟踪，不在语料里假装它不存在。
