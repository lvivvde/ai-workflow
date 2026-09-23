# evaluation

离线评测脚手架。它回答两个问题：V2 之后的改动有没有破坏 V1 契约，以及各层质量是什么水平。评测同时给出组件级（in-process）和端到端（真实 MCP Client）结果，并且**不产出任何混合总分**：每一层保留自己的状态和指标，缺失能力如实记为 `unavailable`。

```text
evaluation/
├── README.md
├── annotation-guide.md # 标注协议（guide_version eval-guide-0.2）
├── quality-gates.json  # 发布门槛定义（quality-gates-0.1）
├── quality-gates.md    # 门槛怎么判、失败怎么归因
├── corpora/            # 版本化的脱敏/合成语料
│   ├── v1_compatibility/
│   ├── development_set/   # 内含 assets/：像素里真有字的合成 PNG
│   └── golden_set/        # 同上
└── runs/               # 本地产物，不入库
```

## 运行

```powershell
uv run python tools/evaluate.py `
  --corpus evaluation/corpora/v1_compatibility `
  --corpus evaluation/corpora/development_set `
  --corpus evaluation/corpora/golden_set
```

Windows 上三档 Hardware Profile 各跑一次、逐档判门禁、并留下 Run Manifest：

```powershell
.\scripts\windows-baseline.ps1            # .baseline\evaluation\{baseline,recommended,visual}
.\scripts\windows-baseline.ps1 -RequireGates
```

常看参数：

| 参数 | 作用 |
|---|---|
| `--corpus` | 可重复；至少一个语料根目录 |
| `--modes` | 默认 `component,e2e` |
| `--runs-dir` | 产物目录，默认 `evaluation/runs/<run-id>` |
| `--hardware-profile` | 写入 Run Manifest，默认 `baseline` |
| `--gates` | 门禁定义，默认 `evaluation/quality-gates.json` |
| `--baseline` | 上一轮 `run.json`，用于判定相对回退限制 |
| `--no-gates` | 只出分层报告，不判门禁 |
| `--refresh-manifest` | 语料改动后重算样本指纹 |
| `--force` | `--refresh-manifest` 允许改写已冻结语料 |
| `--no-artifacts` | 只打印结果，不写 `runs/` |

任一 Release-blocking invariant 违规、或任一质量门槛失败时退出码为 `1`，`failures` 与门禁的 `followups` 逐条点名样本、层、指标、错误分类和原因。每条运行会写出 `run.json`（分层汇总 + Run Manifest）、`item_results.json`（逐项响应）和 `report.md`（人读报告）。

判门禁也可以单独对一份历史 Run Manifest 做，不必重跑语料（改过阈值后重判时用）：

```powershell
uv run python tools/judge_gates.py --run evaluation/runs/<run-id>/run.json `
  --baseline evaluation/runs/<上一轮>/run.json --json-out verdict.json
```

## 语料协议

每个语料目录由 `manifest.json` 和 `samples/*.json` 组成。样本声明要索引的文档（DOCX/XLSX/独立 PNG 生成器规格，不携带真实设计资料）、工具、参数、期望结果和标注信息。三份语料的用途不同：

| 语料 | split | 冻结 | 用途 |
|---|---|---|---|
| `v1_compatibility` | `v1_compatibility` | 否 | V1 契约兼容性安全网 |
| `development_set` | `development` | 否 | 调试、错误分析、回归定位 |
| `golden_set` | `golden` | 是 | held-out 验收集，标签未被用于调参 |

- `manifest.sample_fingerprints` 记录每个样本的指纹。样本被改动后 `load_corpus` 直接拒绝，必须先 `--refresh-manifest`。
- `frozen: true` 的语料（Golden Set）拒绝刷新，除非显式 `--force`；改写已冻结标签会让此前所有质量报告作废。
- 图片文档（独立 PNG 与 DOCX 内嵌图片）用 `asset` + `asset_sha256` 指向语料自带的 `assets/<名称>.png`：像素里真的有标注的字符，改图必须同步改 pin 与标注。图片由 `tools/render_corpus_assets.py` 渲染（**开发期工具，需要 Pillow**；评测只用标准库把 PNG 原样写出去），规则与实测约束见 [`annotation-guide.md` §11](annotation-guide.md)。
- 高风险样本必须有 **两名独立标注者 + 第三方裁决**，否则加载即失败。高风险包括冲突、非 `found` 期望、未知记号、流程图/图片文本/冲突/未知内容类型。
- 人工只需在隔离副本上用 `tests/test_evaluation_harness.py` 里的 `allow_unadjudicated_high_risk` 放宽，正式加载不放宽。
- `manifest.review_seeds` 声明样本需要的人工确认（记法含义等）。评测在跑样本前用常规 `plan_review_action` / `apply_review_action` 复现它们，所以运行日志里有这次确认，而语料本身不依赖任何手工改过的状态目录。
- 标注字段与标法见 [`annotation-guide.md`](annotation-guide.md)；`guide_version` 与 `manifest.guide_version` 必须一致。

每个样本声明 9 个 stratum 字段（来源类型、内容类型、语言、图像质量、结构复杂度、记号复杂度、冲突状态、能力包、难度），全部记入样本指纹；报告按其中 4 个维度分组汇总（`source_type`、`content_type`、`language`、`difficulty`），其余字段在单条样本上仍会被检查。

## 报告结构

`run.json` 与 `report.md` 都按同一套分组给出原始计数与样本数，且**不合并总分**：

- **Run**：Run Manifest（run id、硬件档位、平台、语料版本与指纹、工具表版本）与「本报告不提供混合总分」的声明；
- **Layers**：逐层逐模式的状态（`measured` / `unavailable`）与指标；
- **Strata**：按 4 个维度分组的样本数、期望→观测状态矩阵；
- **Capability packs**：每个能力包自己的样本数与响应状态，绝不与其它包合并；
- **Response states**：每种模式的期望→观测状态矩阵；
- **Release-blocking invariants**：五条零容忍检查的违规明细。

比率类指标一律给「计数 / 总数 / 比率 / Wilson 95% 区间」，所以样本少时区间会自己变宽；样本不足的层不会被当成通过。

## 评测层

协议共 10 层，当前构建的可测量范围如下：

| 层 | 状态 |
|---|---|
| `source_import` | 已测量：文档数、图片数、建库耗时、schema 版本、是否过期 |
| `ocr_transcription` | 有 OCR 引擎时测量标注层：CER、WER、Critical Token 覆盖、区域完整率、区域数、降级/回退/低质量计数。没有可用引擎时本层如实报 `unavailable` 并写明原因，标注**不**被算作命中 |
| `layout_regions` | 有 OCR 区域时测量：图片数、layout run、元素数、元素/图、几何不确定计数、规则版本；没有区域进入规则集时 `unavailable`（并说明语料仍标注了关系） |
| `reading_order_relations` | 同 `layout_regions`：有区域时按标注给 Precision/Recall/F1；没有区域时 `unavailable` |
| `notation_resolution` | 已测量：已确认条目是否解出标注含义、未知符号是否保持未知（`annotated_reading_matches`） |
| `statement_fidelity` | 已测量：声明数、可溯源比例（含逐条来源/定位） |
| `retrieval` | 已测量：recall@limit、recall@1、recall@5、precision@limit、MRR、无答案误报/漏报 |
| `conflict_and_response_state` | 已测量：冲突保留率、期望→观测状态矩阵 |
| `explanation` | 已测量：Required Atom Coverage、引用可解析率、unsupported atom rate、缺口披露、冲突保留 |
| `performance_and_degradation` | 已测量：P50/P95 延迟、建库耗时、峰值内存、工作区体积、降级事件 |

`unavailable` 是如实报告，不是通过。任何一层都不会被折叠进单一分数。

可选向量能力（`semantic-v1`）单独测量：它默认关闭，未完成时不阻塞 Core V2 基线，也不与任何其它层合并。

## 质量门槛

发布门槛是逐层条件的集合，不是单一阈值：每条条件点名一层、一个指标、一个绝对边界和所需最少标注样本数；判定顺序、错误分类（数据/标注/OCR/布局/关系/记法/检索/事实/释义/性能/环境）与阈值取法见 [`quality-gates.md`](quality-gates.md)。相对回退限制只在显式传入 `--baseline` 时判定，没有基线时报告写明「未判定」而不是当作通过。

## Release-blocking invariants

这五条不参与打分：任一违规即整轮失败，并输出样本、模式和原因。

| 不变量 | 拦截行为 |
|---|---|
| `source_evidence_unchanged` | 评测期间源文件消失、内容变化或被新增；或调用失败却谎报 `found` |
| `no_unsupported_project_fact` | 标注为无答案却断言项目事实；`supported_by` 指向同一响应未返回的引用；把机器转写提升为 `verified`/`explicit` 而无已确认复核 |
| `no_silent_conflict_resolution` | 响应通过 `winner`、`preferred_statement_ref` 等字段替用户选定冲突版本；或标注为冲突却不暴露冲突组 |
| `source_references_traceable` | 声明缺少来源、指向索引外的文档或资产、缺少定位信息、含空 `derived_from`。独立资产（`document_type=image`，整份文件就是被声明的对象）由资产本身定位；内嵌图片、表格单元格等仍必须写明读取的位置 |
| `degradation_not_hidden` | 运行记录了降级，响应却既不上报该计数也不给降级元数据 |

## 离线保证

评测全程用 `network_guard.block_network` 封掉外部网络访问，任何尝试都会记入 `network_violations` 并让运行失败。回环地址例外，因为 asyncio 在 Windows 上依赖回环自管道，封掉会破坏 MCP 客户端而不是网络策略。

## 与 V1 契约的关系

`src/game_design_knowledge/v1_contract.py` 冻结了 V1 的 12 个工具：名称、必填参数、默认值、逐状态响应字段、允许的状态值和定位字段。`tests/test_v1_contract.py` 直接对运行中的服务比对这份契约。

兼容性判断基于公共契约和语义，不与 SQLite 逐字节比较：派生索引可以随时删除重建，字节相等会同时产生误报和漏报。
