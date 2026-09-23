# Changelog

## Unreleased

- 随仓语料的图片换成**像素里真有字**的合成图（`development_set@0.2.0`、`golden_set@0.3.0`）：此前图片文档一律写 1x1 占位图，标注只说明"这张图本该写什么"，于是 `ocr_transcription` / `layout_regions` / `reading_order_relations` 三层在装有引擎的机器上也只能报 `unavailable`。换上真图后三层首次有分母，三档基线里这三层的 environment 类失败随之消失（实测 `cer=0.0`、`critical_token_coverage=1.0`、区域完整率 10/10、`annotated_relation_scores=1.0`）。Golden Set 已冻结，这次按 `--force` 改版并作废此前报告里这三层的结论。
- 图片文档改用**语料自带的资产**：`generator.asset` + `asset_sha256`（独立 PNG 与 DOCX 内嵌图片都支持），物化时逐字节核对，缺图、不是 PNG、越出语料目录、哈希漂移都报 `SchemaError`，图像与标注不会再无声地各说各话；图片由开发期工具 `tools/render_corpus_assets.py` 渲染（`--check` 只比对不写盘），评测本身仍只用标准库。
- 记录并固化**语料构造约束**：本机实测（Windows 11 / RapidOCR 1.3.24，30 多种渲染）里只有「独占一行的重复上箭头」能被转写成独立箭头块，单个箭头、向下箭头与被丢弃的横向箭头都拿不到区域，因此语料只画自下而上的流程链——`transcription` 记画面自上而下的阅读顺序，`relations` 记箭头方向（下方块 → 上方块），只有行内箭头的图不标关系。规则与实测表见 `evaluation/annotation-guide.md §11`，引擎侧的字形缺陷单独立工单跟踪。
- 新增 `tests/test_evaluation_fixtures.py`（12 项）：图片资源的解析、缺图/漂移/越界/非 PNG 的拒绝、占位图这条降级路径仍可用，以及「已提交字节 == 渲染器输出」「渲染器画的行 == 标注的 regions」「`transcription.text` == `regions` 拼接」三条回归。
- 修能力包离线安装在真实机器上**装不完**的问题：包清单现在声明固定的 wheel 闭包（新增 `python_dependencies`，core 声明 14 个、visual 声明 8 个），bundle 必须携带整个闭包，`--require-hashes` 的离线 pip 因此能一次装完；此前只声明三个顶层 wheel，而 hash-checking 模式要求每个要装的 distribution 都带哈希出现在 requirements 里，于是合法 bundle 也会在安装中途失败。`verify` 逐条核对闭包，少一个成员或以未声明的 wheel 充数都报 `bundle_pin_mismatch`。
- 修 wheel 分发名比较：按 PEP 503 归一化，`PyYAML` 与 pin 里的 `pyyaml` 视为同一分发，同一 bundle 重复声明同一分发会被拒绝而不是静默取最后一条。
- 修 Windows 中文控制台下 CLI 直接崩的问题：`stdout`/`stderr` 以替换字符输出，JSON 负载里含当前代码页编不了的字符不再触发 `UnicodeEncodeError`；离线 pip 子进程以 UTF-8 输出，中文路径不再变成替换字符。
- 修评测 harness 与产品**用不同的方式建索引**：harness 现在用 `selected_ocr_engine()` 取与 `run_pipeline` 相同的引擎选择，并把选中的引擎写进 Run Manifest 的 `capabilities.ocr_engine`（含降级链）。此前 harness 直接调 `build_index_atomically` 且不指定引擎，等于永远走 V1 单引擎路径，机器上装了 RapidOCR 也只能报 `unavailable`。
- 评测 harness 测试不再隐含"本机没装 OCR 引擎"：改为断言「不可测量的层必须写明原因」，因此装没装引擎的机器上都能跑。
- Windows 实机验收补记：Core 包从离线 bundle 安装成功（17 个 wheel，全程 `--no-index`），`capability status` 报 `core=available`，`tools/evaluate.py` 的 Run Manifest 记录到 `ocr_engine={selected: rapidocr, version: 1.3.24}`；用一张自带文字的 PNG 走 `index` 命令，索引得到 `ocr_succeeded=1`、`ocr_regions=2`，转写文本与图片内容逐字一致。
- `docs/capabilities.md` 补齐离线包闭包要求、`uv venv` 无 pip 时的补救（`uv pip install pip`）与 `enhanced_ocr` pin 指向不存在版本（`paddlex==2.4.4`）的说明；`evaluation/quality-gates.md` 更正 OCR 层的边界——除引擎外，随仓语料的图片本身无文字，`cer` 要等语料换成带文字的图片才有分母。
- 新增 V2-12 评测语料与首轮分层基线：`development_set` / `golden_set` 扩充到覆盖 DOCX、XLSX、内嵌图片与**独立 PNG**、中英文、箭头记法、未知符号、冲突、无答案、降级与释义 strata；Golden Set 升到 `0.2.0` 并保持冻结（改标签必须显式 `--force` 且作废此前报告）。
- 新增标注协议 [`evaluation/annotation-guide.md`](evaluation/annotation-guide.md)（`eval-guide-0.1`）：普通转写单人标注加抽检、高风险事实/关系/冲突/无答案/释义双人标注 + 第三方裁决；逐字段说明来源范围、文字与 Critical Token、区域与阅读顺序、关系、记法（confirmed/rejected/unknown 与 scope）、Allowed Answer Set、引用、缺口与冲突怎么标。
- 语料可用 `manifest.review_seeds` 声明样本需要的人工确认，评测在跑样本前用常规 `plan_review_action` / `apply_review_action` 逐条复现，运行自己的日志会记下这些决定，语料不依赖任何手工改过的状态目录。
- 评测层补齐带标注指标：OCR 层按标注算 CER/WER、Critical Token 覆盖与区域完整率；关系层算 Precision/Recall/F1；记法层核对已确认含义是否解出、未知符号是否保持未知；释义层算 Required Atom Coverage、引用可解析率、unsupported atom rate、缺口披露与冲突保留；检索层增加 `recall_at_1`、`recall_at_5`、无答案误报/漏报。**没有 OCR 引擎时这些层如实报 `unavailable` 并写明原因，标注不算命中。**
- 报告新增 Run 段（Run Manifest、硬件档位、语料版本与指纹）与 Capability packs 段：每个能力包单独给样本数与响应状态，绝不与其它包合并；比率类指标一律给计数、总数、比率与 Wilson 95% 区间，报告不产出任何混合总分。
- 新增发布门槛 [`evaluation/quality-gates.json`](evaluation/quality-gates.json)（`quality-gates-0.1`）与判定实现 `evaluation/gates.py`：逐层绝对下限/上限 + 所需最少标注样本数 + 相对回退限制；判定顺序为「整层缺报 → `environment`；非 `measured` → 该层 `unavailable_class`（默认 `environment`）；样本不足或指标未发布 → `annotation`；越界 → 该层默认归因」，不可测量不等于通过。失败按数据/标注/OCR/布局/关系/记法/检索/事实/释义/性能/环境分类并拆成后续任务，阈值取法与 Windows 无 OCR 的边界写在 [`evaluation/quality-gates.md`](evaluation/quality-gates.md)。
- `tools/evaluate.py` 新增 `--gates`（默认提交的门槛）、`--no-gates` 与 `--baseline`：运行时打印门禁判定，失败以退出码 `1` 结束并逐条列出错误分类与后续任务；新增 `tools/judge_gates.py`，可对已记录的 `run.json` 重新判门禁而不必重跑语料（改阈值后重判历史结果，或让基线脚本把 invariant 判定与门禁判定分开）。
- 新增 Windows 分层基线脚本 `scripts/windows-baseline.ps1`：对 `baseline` / `recommended` / `visual` 三档各跑一次评测，产出 Evaluation Run Manifest，逐档判门禁并在下一轮作为相对回退基线（`-SkipEvaluation` 只重判历史 Run Manifest，`-RequireGates` 让门禁失败也以非零退出码结束）。
- 记法读工具（`resolve_notation` / `notation_dictionary`）的响应新增 `index_status`：已确认含义是人的决定，但候选读法来自索引，答案因此带上该索引的计数，缺 OCR 引擎这类降级不再只出现在运行报告里，`degradation_not_hidden` 不变量在记法层同样成立。
- 修 `evaluation/claims.py` 的定位判定：独立资产（`document_type=image`）整份文件就是被声明的对象，由资产本身定位；内嵌图片、表格单元格等仍必须写明读取位置。此前独立图片的检索命中会被误判为「缺少定位」。
- 修 `statement_fidelity` 层漏报支撑样本的问题：该层此前已测量却把样本数报成 0，门禁因此只能给「标注不足」，现在层会点名所有返回了声明的样本。
- 修 `scripts/windows-smoke.ps1` 与 `scripts/windows-baseline.ps1` 的编码：两份脚本以 UTF-8 无 BOM 保存时，Windows PowerShell 5.1 会把中文按系统代码页解码并直接解析失败；现在带 BOM，5.1 与 7 都能正常解析。

- 新增完全离线的能力包投递（V2-11）：一个 bundle 目录（`bundle.json` + `wheels/<pack>/` + `models/<pack>/`）携带 wheel 与模型文件、各自的 SHA256、以及它是在什么平台和解释器上打包的；`OfflineBundle.read/verify/plan/install/uninstall` 逐字节核对后才会安装，pin 漂移、多出未声明的 wheel 或模型、checksum 不符、跨平台或跨 Python 版本一律在安装前停下，并给出 `bundle_missing`/`bundle_manifest_invalid`/`bundle_unknown_pack`/`bundle_pack_absent`/`bundle_platform_mismatch`/`bundle_python_mismatch`/`bundle_artifact_missing`/`bundle_checksum_mismatch`/`bundle_pin_mismatch` 这些可判定的原因码，而不是一句“失败”。
- 安装与卸载都没有网络路径：Python 侧只打印固定的离线命令（`--no-index --find-links <bundle>/wheels/<pack> --only-binary :all: --require-hashes -r <已写出的哈希 requirements>`），模型文件由 `ModelStore` 从 bundle 目录按 pin 拷贝，全程不启动下载、不在幕后 `pip install`；测试在 `block_network` 里把 `subprocess.run` 换成断言来证明这一点。卸载只删模型文件，事实、证据与词法索引不动（`requires_reindex=false`），文件被占用时以 `capability_removal_failed` 停下并列出仍在原地的文件。
- 新增 `capability` CLI 子命令组：`status`、`doctor`、`plan`、`install`、`verify`、`uninstall`、`manifests`、`baseline`。`capability doctor` 在安装前逐项说明这台机器能不能装：磁盘余量与每个包的 `min_free_disk_gb`、模型根是否绝对路径/可写/长度在 200 字符内、Tesseract 是否存在以及是否真的带有所需语言包（默认 `chi_sim+eng`，缺 `chi_sim` 就明说缺它，而不是事后把中文转写得很差），并对每个包报出当前状态；`install` / `uninstall` 不带 `--confirm` 只返回预览。
- 随包发布的清单 `capabilities/manifests/{core,enhanced_ocr,visual}.json` 与 `capabilities/bundle.schema.json` 由代码生成（`capability manifests --write <dir>`），测试逐字节比对，清单漂移会让测试失败；`capability verify` 也可以单独核对一个 bundle 是否与当前 build 的 pin 一致。
- 新增每档 Hardware Profile 的资源预算与运行记录（`run_records.py`）：`baseline` / `recommended` / `visual` 的 `ocr_concurrency`、`heavy_jobs_concurrent`、`ocr_batch_size`、`idle_timeout_seconds`、`min_ram_gb`、是否启用向量召回与视觉模型只在这张表里定义一处，`CapabilityRuntime.status()["limits"]` 直接读它，文档与实现不会各写一套；`capability baseline --profile <profile> --output <file.jsonl>` 跑一次实测，写出 `capability-run-v1` 记录：该 Profile 的预算、延迟、峰值内存、磁盘前后值、硬件与平台、以及运行期间发生的每一条降级事件；并发与超时变化前先记数字，之后再谈优化。
- 新增 Windows 验收脚本 `scripts/windows-smoke.ps1`：检查依赖 → `capability doctor` → 可选重建索引 → `tools/smoke_stdio.py` → 依次为 `baseline`/`recommended`/`visual` 追加运行记录到 `.baseline/windows/<profile>.jsonl`；一台没有任何网络的 Windows 10/11 机器上“装得上、跑得动、有记录”由此变成可重复的一条命令，Mac 的类似运行只作为该平台的证据，不作为发布门槛。

- 新增可选本地文本向量召回与查询改写（V2-10，实验开关，默认关闭）：`GAME_DESIGN_EMBEDDING_PROVIDER` 为空时不加载任何模型，行为与确定性底座完全一致；设为 `hashing`/`hashing:<dim>` 使用内置参考实现 `local-hashing-text`，设为 `module:attribute` 导入本地 provider。向量存在可删除、可重建的侧车 `<index_dir>/semantic.sqlite`（`semantic-v1`，写入 `*.building` 后原子发布），只存 unit id、来源/输入哈希、模型身份与向量，不存可引用文本。
- 新增三个只读/显式确认工具：`semantic_index_status()` 报告开关、provider 身份、侧车状态（`missing`/`ready`/`stale`/`incompatible`）与 `rebuild_recommended`；`rebuild_semantic_index(confirmed=False)` 与 `drop_semantic_index(confirmed=False)` 各自预览，只有 `confirmed=true` 才真正重建或删除，删除后返回 `core_tools_unaffected=true`。工具表 26 → 29，V1 工具签名与默认响应不变。
- 向量记录绑定稳定 Source ID 与模型三元组（`model_id`/`model_version`/`dimension`）：不同模型或不同维度的向量绝不混搜，检索报 `semantic_model_changed` 且 `semantic_index_status` 报 `incompatible`；索引版本不符报 `semantic_index_incompatible`，来源哈希变化或单元消失报 `semantic_index_stale`，三种情况本次都不参与召回并建议重建。
- 新增 `auto`/`hybrid`/`semantic` 模式语义与 `semantic_candidate` 状态：`auto` 只在确定性通道没有事实命中时才请求向量，缺能力也不降级；`hybrid` 显式要求向量，缺能力时状态 `degraded`、`mode.effective` 降为 `auto` 而事实与 FTS5 照常回答；`semantic` 只跑向量通道，确定性通道报 `skipped`（`reason=semantic_mode_only`）。向量命中必须证据回读，文本永远从当前事实取回，`score` 只取最强单分（相似度单列，不与 BM25 相加），低于阈值的命中只进 `possible_related`（`supports_project_fact=false`）且不降低 `status`。
- 新增可选本地查询改写：`GAME_DESIGN_QUERY_REWRITER` 未配置时只跑原查询；配置后生成的每个变体都要过守门，空串、同文、超长（`MAX_VARIANT_CHARACTERS=200`）以及改变 `value`/`unit`/`version`/`time`/`negation`/`scope` 的变体被逐条拒绝（`VARIANT_CHANGES_*`），一次最多采用 `MAX_GENERATED_VARIANTS=4` 个（其余 `VARIANT_LIMIT_REACHED`），被拒结果写入 `response_meta.query_rewrite.rejected`，且**绝不更新** Designer Notation Dictionary（`updates_notation_dictionary=false`）。
- 降级可观察：`response_meta` 新增 `semantic` 与 `query_rewrite`，`response_meta.vector` 增加 `model_version`/`dimension`/`vectors`/`threshold`，`retrieval` 新增 `semantic` 与 `query_variants`；`degradation_events` 报告 `provider_not_importable`、`provider_invalid`、`query_rewriter_not_importable` 等具体原因码，坏开关与坏改写器不会静默消失。V2 首发不实现原始图片 embedding（`IMAGE_EMBEDDING_SUPPORTED=false`），图片仍通过转写进入检索。
- 新增 V2 检索工具 `retrieve_evidence(query, document_type=None, evidence_type=None, limit=20, mode="auto", include_candidates=False, document="")`：保留原查询，只用已确认记法字典与玩法目录别名做确定性扩展，再走 `exact`、`lexical`（FTS5）、`confirmed_alias`、`structures` 等通道，并默认返回 `found`/`not_found`/`partial`/`ambiguous`/`degraded`/`failed` 六态之一。`search_evidence` 作为冻结的 V1 词法检索签名与默认响应一字未改。
- 检索单位按命名空间隔离：`source_facts`、`image_transcription`、`visual_interpretation` 才算事实，`explanation` 只补匹配理由（`assist_only`、不新增候选），`unconfirmed_candidates` 只在探索模式可搜。图片转写必须通过自己的质量门槛（`accepted` 且 `machine-supported`）才能进入事实查询。
- 每个候选返回前回读当前索引（Evidence Hydration）：核对 `recorded_sha256`、重新解析 locator、重读区域与结构关系；行已删除、来源哈希变化、locator 或区域不再可读、关系不存在都判为 Untraceable Candidate（`supports_project_fact=false`），不计入 `evidence`。
- 新增冲突保护：按同一 section、同一 evidence_type、同一 `claim_topic` 组成 scoped claim，跨文档签名不同即输出 `winner=null`、`resolution_state=unresolved` 的 Conflict Group（维度为 `value`/`unit`/`version`/`time`），同文档只报 Potential Conflict Candidate，完全一致则互相印证；`conflict_scan` 通道在 `CONFLICT_SCAN_LIMIT=200` 行内补回未被查询命中的对侧证据，补回项 `matched_query=false` 且不产生任何赢家字段。
- 检索降级可观察：`hybrid`/`semantic` 无向量能力时显式降级为 `auto` 并给出 `degradation_events` 与 `rebuild_vector_index_recommended`，字典不可读只失去扩展并记入 `retrieval.unavailable_capabilities`，缺命名空间时该通道报 `unavailable` 而其余通道继续作答；`response_meta.vector` 报告 provider/model/index_version 为 `null`，降级状态优先于 `not_found`。
- 融合保留全部通道证据：同一 retrieval unit 的多路命中合并为一条，逐条保留 `channel`/`match_type`/`rank`/`raw_score`/`reason`，不同通道的原始分数永不相加（`score` 只取最强单分），排序不改变 Evidence Status；explanation.py 导出 `claim_topic` 与 `claim_values` 供 V2 检索复用。
- 新增分层详尽释义（Explanation Layer）：`explain_evidence(unit_id)` 与 `explain_query(query)` 先用确定性证据规则构建结构化 Explanation Atom，再渲染 Brief/Standard/Full（默认 `full`）。每个 Atom 自带 `source_reference`、`locator` 与最小原文片段，`full` 固定按 13 个章节展开，`rendered.sentence_map` 把每个分句映射回一个或多个 Atom，`explanation_id` 与 `provenance` 让相同输入可复现。
- 三档都不隐藏会改变结论的东西：冲突双方各自成 Atom 并生成响应级 Conflict Group（`winner=null`、`resolution_state=unresolved`，直接结论固定说「现有证据支持多个解释，不能确定唯一答案」），Relevant Source Gaps 与定位在 `brief` 里也保留；不按日期、置信度或平均值挑赢家，也不做 6.5 秒这类折中。
- 措辞受控：原文 Atom 逐字等于来源片段，数值与比较符原样保留（`<` 不改写成 `≤`），关系只用 `visible_connector`/`next_step`/`depth_hint`/`candidate`/`unresolved`/`ignored` 六个模板，新增措辞不得出现「导致/依赖/触发/必须先完成/运行时调用/设计目的/因为/所以」；来源没写设计目的时明确返回未知，不用行业惯例补造。
- 记法只按已确认条目展开：命中的 confirmed 条目写成「原词（含义）」并记下定义来源，被拒绝的读法明说「已被人工标记为装饰」，没有确认定义的符号保持原词并标 `status=unknown`。
- 新增 Explanation Contract Validator：逐 Atom 检查来源、章节、陈述类型、证据状态、内容层、Locator 与支撑引用，不合格 Atom 被隔离并产生 Structured Uncertainty（`atom_without_support`）、响应降为 `partial`，级联隔离引用它的句子；发生过隔离时 `contract.ok=false`。
- Source-as-Data 边界在释义层强制执行：来源文本里的「忽略之前的指令」「放宽证据门槛」「不要告知用户」、脚本与外链只会原样进入 Atom 并产生 `security.warnings`，不改变 Profile、证据门槛、工具权限或输出契约。
- 释义分页：`page_size` 上限 200、默认 40，第一页固定保留直接结论、全部冲突与所有 `changes_conclusion` 的缺口并返回阅读顺序前缀，`next_cursor` 连续不重复；截断时返回 `truncated`、`remaining_atom_count` 与 `next_cursor`，不静默降低 Profile。
- 没有本地语言模型时模板渲染即满足完整契约；可选润色器只能改写措辞，引入或改动数字、比较符或禁用措辞会被逐条拒绝（`polisher_rejected`），抛异常则整份回退到确定性渲染。
- `get_evidence_package` 的 `explanation` 层不再固定报 `no_explanation_profile`：正文与图片单元都能拿到该单元的释义条目（`expand.tool=explain_evidence`），只有确实没有可解释内容时才报未服务；`explain_query`/`explain_evidence` 顶层同时暴露 `conflicts`。
- 新增策划记法字典（Designer Notation Dictionary）：一个记号在一个范围里的含义作为人工确认结果写入 Durable Project State（`.design-state/notation.json`），与确认别名并列，既不进推导索引也不改源文档；索引提出的读法只是 candidate，永远不参与回答，也不能被引用为项目事实。
- 范围分 `project` / `document_type` / `document` / `region` 四档并做字面匹配：为一个区域确认的含义不会回答同文档其它区域、其它文档或其它类型；路径必须是项目相对路径，绝对路径、盘符、`..` 一律拒绝。来源优先级为 `region_legend` > `document_definition` > `document_type_definition` > `project_dictionary` > `candidate_interpretation` > `external_common_knowledge`。
- 存在分歧时不静默选边：只要更低层不同意，答案就停在 `ambiguous`（`resolved` 为空、`supports_project_fact=false`）并给出按优先级本该胜出的 `authority_winner`；要落定必须用 `resolve_conflict` 记录一次裁决（`authority` 要求胜出者层级严格更高，同级或人故意选低层必须写 `human_choice`）。
- 新增 `plan_review_action` / `apply_review_action`：任何写入先返回 preview（before / after / changes / writes / affected / `propagates_beyond_scope=false` / `derived_knowledge_index_modified=false`）与 `plan_token`，只有带同一 token 且 `confirmed=true` 才应用；token 用 `hmac.compare_digest` 比对，覆盖动作、范围、含义、层级、被替代者、理由、执行人与写入前的字典摘要，不覆盖时钟（条目 ID 与记录时间都在应用时刻生成），字典一变即失效并要求重新预览。
- 五个 Review Action：`confirm`、`correct`（旧条目转 `superseded` 并保留原值）、`reject`（条目写字典、候选只写日志）、`ignore`（只写日志）、`resolve_conflict`。每次应用**先写 append-only Review Event、再物化视图**，事件记录操作者、时间、范围、前后值、理由与依据。
- 版本迁移只产生候选：条目在确认时绑定 `parse_revision_id`，新修订成为 active 后不再参与回答，而是列入 `migration_candidates`（`requires_review_event=true`、`auto_applied=false`）；要让同一读法在新修订生效必须重新做一次显式 Review Action。
- 新增只读工具 `notation_dictionary(document="", include_history=False, limit=200)`、`resolve_notation(...)` 与 `review_history(...)`；`DurableState.status()` 增加 `notation_entries` / `notation_confirmed` / `notation_superseded` / `notation_rejected` 计数。V1 工具签名与默认响应、`index_status` 的 V1 字段均不变。
- 拒绝或回滚审核只写 `.design-state/`：测试用字节比对证明整串动作前后 `knowledge.sqlite` 与源文件完全一致，索引仍可读、仍持有同样的图片与结构关系。
- 新增 V2 分层证据包：`get_evidence_package(unit_id)` / `get_evidence_packages(unit_ids)` 以 `evidence:<id>`、`image:<id>` 为锚点，把 source、statement、transcription、visual_interpretation、notation、explanation、uncertainties 分层返回，并整份带上 provenance 与逐层 `unavailable`；每个派生条目都解析到 revision-aware Source Reference 与原始区域（含 bbox）。
- 新增受控资产访问 `get_asset(asset_reference)`：只接受索引签发的 `asset-<32 位十六进制>` 引用，任何文件路径一律 `not_found`；引用由索引目录名与索引内相对路径推导，可选内联 base64 并同时报告读取到的 SHA256 与 `sha256_matches_index`。
- 新增 `get_processing_manifest()`：返回处理运行清单、逐 stage 尝试摘要与统一的 degradation 事实（回退 stage、降级 stage、被拒 stage、原因码计数），与 `index_status.processing` 同源。
- 大型证据包稳定分页：`limit`/`cursor` 在一条按层展开的扁平条目序列上推进，页边界不会拆断陈述与其来源的关联；批量请求部分成功时保留已取到的包并逐条说明 `not_found`/`invalid`（Partial Evidence Response）。
- 独立 PNG/JPEG 导入闭环：`plan_document_import`/`import_documents` 接受图片并以 `source_type=standalone_image` 预览，落盘到 `docs/png|jpg|jpeg/`，登记为 `document_type=image`（`relationship_id=standalone`）的文档；沿用预览确认、禁止覆盖、失败回滚与原子索引发布。
- `search_evidence` 新增显式开关 `include_v2_metadata`（默认 `false`）：开启后每条命中附带紧凑 Hydrated Evidence Hit（unit_id、source_reference、display_locator、section_names 与展开提示），默认响应不含任何 V2 字段，V1 契约不变。
- 交付视觉布局层：从 OCR 区域与几何构建 Visual Elements、行/列、`depth_hint` 与候选阅读顺序，并实现纵向箭头规则（独占箭头块、唯一上下端点、顺序相邻）确认 `next_step`；分支、连续箭头、缺失端点、跨容器与重叠布局一律输出 candidate 并写明原因。
- 每条 Structural Relation 保存端点、支撑区域、几何依据、规则版本与 `claim_boundary`，并**分列**保存 `geometry_confidence` 与 `ocr_confidence`；缩进只记录 `depth_hint`，永不建立父子关系，输出不声称因果、运行时依赖或作者目的。
- `layout` 与 `structure_relations` 从“声明但未实现”变为真实 stage：规则集版本升为 `layout-regions-v1` / `flow-arrow-v1`，两者都归 core（不依赖 `enhanced_ocr`/`visual` 包），并记录 `visual_model_required`、`visual_pack_status` 与 `visual_candidates`；实际计算在 `retrieval_projection` 内完成，因为没有 OCR 引擎时 stage 报 `no_ocr_engine` 而不是 `stage_not_implemented`。
- Schema 升级到 v6，新增 `layout_runs`、`layout_elements`、`structural_relations` 三张表与三条索引，提供 v5 → v6 显式迁移；`index_status` 增加 `layout_*` 明细，`get_image_context` 增加 `layout`（阅读顺序元素 + 关系 + 事实边界），旧索引迁移后照常可读。
- 评测脚手架的 `layout_regions` 与 `reading_order_relations` 层不再固定报 `unavailable`：它们按已发布索引里的要素数、关系数与规则集分布给结果，语料里没有区域可排序时仍然明确报 `unavailable` 并给出原因。
- 交付区域级 OCR：DOCX/XLSX 内嵌图片与独立 PNG/JPEG 现在按**区域**产出边界框、阅读顺序、语言与原文，逐区域保存 Raw Transcription、规范化建议和逐 span 变更记录；规范化只产出建议，原文永不被覆盖。
- 每区域分别记录文字、区域、关键标记三类置信度，拒绝任何单一总分载荷；数字、百分比、ID、运算符、箭头和否定词作为 Critical Transcription Tokens 按类别单独评分，`-3.5`、`ITEM_ID_01` 这类写法不会被拆错。
- OCR 状态映射收敛到单一处：超时/引擎报错保留已产出的区域并标 `partial`，部分输出永不判 `accepted`；损坏图片、格式不支持、缺语言包、缺模型与无可用引擎各自给出准确的 execution/quality 状态、`reason_code` 与 `corrective_action`。
- 单张图按“请求的引擎优先”下钻降级链：`unavailable`/`failed`/缺语言包/缺模型继续换引擎，`succeeded`/`timeout`/`corrupt_image`/`unsupported_format` 立即停止；最后一级给出的原因会被保留，不压成笼统的 `no_usable_engine`。
- RapidOCR 适配器要求 ONNX 模型已经存在于本地（`GAME_DESIGN_OCR_MODEL_DIR` 或包内 `models/`），缺失即报 `models_missing` 并说明构建期从不下载模型；PaddleOCR 适配器同时兼容 `predict()` 与旧 `ocr()` 接口；Tesseract 走 TSV 模式聚合成行区域并保留逐行置信度。
- OCR 结果默认停留在 `transcription`/`machine-supported` 事实边界，载荷中不允许出现 `explicit`/`verified`。
- Schema 升级到 v5，新增 `ocr_runs`、`ocr_regions`、`ocr_normalizations` 三张表与两条索引，提供 v4 → v5 显式迁移；`index_status` 与 `get_image_context` 增加区域级 OCR 明细，旧索引迁移后照常可读。
- 评测脚手架的 `ocr_transcription` 层现在报告区域数、machine-supported、低质量与回退计数；CER/WER 与关键标记评分明确归属 Golden Set（V2-12）提供带标注语料后启用。
- 把单体建索引拆成 8 个可独立重试、可缓存、可降级的处理阶段（`source_parse`、`ocr`、`layout`、`structure_relations`、`notation`、`statements`、`explanation_cache`、`retrieval_projection`），每次执行写入不可变 Stage Attempt，重试只新增行、不覆盖历史。
- 引入 Stage Fingerprint 与内容寻址 Stage Cache：缓存命中要求整条指纹一致，payload 命中后重新哈希校验；指纹变化只失效该 stage 及其全部下游，重试未受影响的 stage 以 `reused` 携带上一轮 attempt id 前进。
- 新增本地能力包清单与运行时：`core`、`enhanced_ocr`、`visual` 三包各自声明用途、许可证、硬件下限、体积与空闲超时；包状态到 stage 执行状态的映射收敛到单一处，缺失可选能力时明确产出 `unavailable`/`degraded`，Core 处理继续。
- 建立 OCR 降级链（RapidOCR → PaddleOCR → Tesseract 兼容回退），逐个引擎记录可用性、版本与被跳过原因；Tesseract 明确标为兼容回退而非等价默认值，可用 `allow_compatibility_fallback=False` 显式关闭。
- `ModelStore` 只从本地目录安装并按 pin 校验和验证，代码中不存在任何下载路径；支持 idle timeout、Low-memory Mode、显式卸载与进程退出释放模型驻留。
- 必选 stage 失败不再被吞掉：`cli index` 退出码 1 且不发布索引，导入/重建工具抛出 `ProcessingError` 并保留原始异常为 `__cause__`。
- Schema 升级到 v4，新增 `processing_manifests` 与 `stage_attempts` 两张表，提供 v3 → v4 显式迁移步骤；`index_status` 增加 `processing` 摘要，新增 `capability_status` 工具与 `capabilities` 子命令。
- 修正视觉能力包 pin 的 SHA256：此前对未追加换行的序列化取哈希，与包实际发布的字节不一致，导致合法安装被判定为校验和不符。
- 引入不可变修订链：Logical Document、Source Revision（内容寻址归档）、Parse Revision 与 Published Revision Bundle，同路径内容变化只新增修订、不覆盖历史。
- 索引改为不可变快照 + `CURRENT.json` 原子发布，保留 Active 与 Last Known Good；构建失败、中断或 Windows 文件占用时旧快照继续可读，未校验快照不会成为 active。
- 新增持久状态目录 `.design-state/`（Review Events、确认字典、逻辑文档身份），删除并重建派生索引不再丢失人工确认结果。
- `index_status` 增加 `freshness`，并新增 `index_freshness` 工具，按 source / durable state / parse / lexical / semantic / explanation 六层分别报告状态、版本和下一步动作。
- Schema 升级到 v3，并提供 v2 → v3 显式迁移、迁移前备份与失败还原；未知 schema 版本显式拒绝而不静默误读。
- 所有 shared-index 读取工具统一返回 freshness 状态，包括 `not_found`、`ambiguous` 和图片查询路径。
- 单次查询复用一个 SQLite 连接和 freshness snapshot；源文档或目录过期时统一返回 `status=stale`。

## 1.1.0 - 2026-08-09

- 增加第三方 AI 可调用的文档导入预览、确认导入与共享索引重建 MCP 工具。
- 正式资料与示例资料按 DOCX/XLSX 自动进入固定分类目录。
- 增加计划令牌、明确确认、防覆盖、并发锁和建库失败文件回滚。
- MCP 配置增加项目根目录，补充跨电脑导入与 Git 提交策略。

## 1.0.0 - 2026-08-09

- 索引 DOCX 标题、段落、列表、表格和内嵌图片。
- 索引 XLSX 工作表、单元格、公式、样式、合并范围和锚定图片。
- 提供统一证据查询、配置查询、图片查询和上下文读取 MCP 工具。
- 提供严格的 `found`、`not_found`、`ambiguous`、`stale` 返回契约。
- 支持人工确认的正式玩法目录和别名，禁止自动联想。
- 支持按文件 SHA 增量更新和 staging 原子发布。
- 随项目提交可移植的 `.index/knowledge` 共享索引，其他成员拉取后可直接部署查询。
