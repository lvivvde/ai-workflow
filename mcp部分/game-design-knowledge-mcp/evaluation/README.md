# evaluation

离线评测脚手架。它回答两个问题：V2 之后的改动有没有破坏 V1 契约，以及各层质量是什么水平。评测同时给出组件级（in-process）和端到端（真实 MCP Client）结果，并且**不产出任何混合总分**：每一层保留自己的状态和指标，缺失能力如实记为 `unavailable`。

```text
evaluation/
├── README.md
├── corpora/            # 版本化的脱敏/合成语料
│   ├── v1_compatibility/
│   ├── development_set/
│   └── golden_set/
└── runs/               # 本地产物，不入库
```

## 运行

```powershell
uv run python tools/evaluate.py `
  --corpus evaluation/corpora/v1_compatibility `
  --corpus evaluation/corpora/development_set `
  --corpus evaluation/corpora/golden_set
```

常看参数：

| 参数 | 作用 |
|---|---|
| `--corpus` | 可重复；至少一个语料根目录 |
| `--modes` | 默认 `component,e2e` |
| `--runs-dir` | 产物目录，默认 `evaluation/runs/<run-id>` |
| `--hardware-profile` | 写入 Run Manifest，默认 `baseline` |
| `--refresh-manifest` | 语料改动后重算样本指纹 |
| `--force` | `--refresh-manifest` 允许改写已冻结语料 |
| `--no-artifacts` | 只打印结果，不写 `runs/` |

任一 Release-blocking invariant 违规时退出码为 `1`，`failures` 逐条点名样本、模式和原因。每条运行会写出 `run.json`（分层汇总）、`item_results.json`（逐项响应）和 `report.md`（人读报告）。

## 语料协议

每个语料目录由 `manifest.json` 和 `samples/*.json` 组成。样本声明要索引的文档（DOCX/XLSX 生成器规格，不携带真实设计资料）、工具、参数、期望结果和标注信息。

- `manifest.sample_fingerprints` 记录每个样本的指纹。样本被改动后 `load_corpus` 直接拒绝，必须先 `--refresh-manifest`。
- `frozen: true` 的语料（Golden Set）拒绝刷新，除非显式 `--force`；改写已冻结标签会让此前所有质量报告作废。
- 高风险样本必须有 **两名独立标注者 + 第三方裁决**，否则加载即失败。高风险包括冲突、非 `found` 期望、未知记号、流程图/图片文本/冲突/未知内容类型。
- 人工只需在隔离副本上用 `tests/test_evaluation_harness.py` 里的 `allow_unadjudicated_high_risk` 放宽，正式加载不放宽。

每个样本声明 9 个 stratum 字段（来源类型、内容类型、语言、图像质量、结构复杂度、记号复杂度、冲突状态、能力包、难度），全部记入样本指纹；报告按其中 4 个维度分组汇总（`source_type`、`content_type`、`language`、`difficulty`），其余字段在单条样本上仍会被检查。

## 评测层

协议共 10 层，当前构建的可测量范围如下：

| 层 | 状态 |
|---|---|
| `source_import` | 已测量：文档数、图片数、schema 版本、是否过期 |
| `ocr_transcription` | 已测量降级计数；CER/WER 等文本指标等在 OCR 能力包落地后启用 |
| `layout_regions` | `unavailable`，当前构建未上报区域能力 |
| `reading_order_relations` | `unavailable` |
| `notation_resolution` | `unavailable` |
| `statement_fidelity` | 已测量：声明数与可溯源比例 |
| `retrieval` | 已测量：recall@limit、precision、MRR、无答案误报 |
| `conflict_and_response_state` | 已测量：冲突保留率、期望→观测状态矩阵 |
| `explanation` | `unavailable` |
| `performance_and_degradation` | 已测量：P50/P95 延迟、建库耗时、峰值内存、工作区体积、降级事件 |

`unavailable` 是如实报告，不是通过。任何一层都不会被折叠进单一分数。

## Release-blocking invariants

这五条不参与打分：任一违规即整轮失败，并输出样本、模式和原因。

| 不变量 | 拦截行为 |
|---|---|
| `source_evidence_unchanged` | 评测期间源文件消失、内容变化或被新增；或调用失败却谎报 `found` |
| `no_unsupported_project_fact` | 标注为无答案却断言项目事实；`supported_by` 指向同一响应未返回的引用；把机器转写提升为 `verified`/`explicit` 而无已确认复核 |
| `no_silent_conflict_resolution` | 响应通过 `winner`、`preferred_statement_ref` 等字段替用户选定冲突版本；或标注为冲突却不暴露冲突组 |
| `source_references_traceable` | 声明缺少来源、指向索引外的文档或资产、缺少定位信息、含空 `derived_from` |
| `degradation_not_hidden` | 运行记录了降级，响应却既不上报该计数也不给降级元数据 |

## 离线保证

评测全程用 `network_guard.block_network` 封掉外部网络访问，任何尝试都会记入 `network_violations` 并让运行失败。回环地址例外，因为 asyncio 在 Windows 上依赖回环自管道，封掉会破坏 MCP 客户端而不是网络策略。

## 与 V1 契约的关系

`src/game_design_knowledge/v1_contract.py` 冻结了 V1 的 12 个工具：名称、必填参数、默认值、逐状态响应字段、允许的状态值和定位字段。`tests/test_v1_contract.py` 直接对运行中的服务比对这份契约。

兼容性判断基于公共契约和语义，不与 SQLite 逐字节比较：派生索引可以随时删除重建，字节相等会同时产生误报和漏报。
