# tests

存放自动化测试及脱敏测试夹具，重点覆盖：

- DOCX 章节、段落、列表、表格和嵌套结构。
- XLSX 多工作表、合并表头、公式、空行和数据类型。
- 文件变更后的增量更新与删除处理。
- 中文全文检索、精确 ID 查询和结果出处。
- MCP 工具的输入校验和错误处理。

两个兼容性安全网：

- `test_v1_contract.py`：直接对运行中的 MCP 服务比对 `src/game_design_knowledge/v1_contract.py` 冻结的 V1 契约（工具名、参数默认值、逐状态响应字段、定位字段），并验证读工具的 `index_status.is_stale` 与写工具的预览/应用字段集。
- `test_evaluation_harness.py`：为五条 Release-blocking invariant 各写一个注入违规的负例，另覆盖网络封锁、语料指纹、冻结语料、双人标注协议、`review_seeds` 复现、以及「已测量的层必须点名支撑它的样本」，并跑通整套评测。

`host_stubs.py` 提供「这台机器上没有 OCR 引擎」这类夹具：进程内的测试替换引擎唯一的宿主机探针 `OcrEngine.installed`，CLI / MCP 的子进程测试则用 `-S`（不带 site-packages）与空 `PATH` 表达同一台机器。凡断言降级路径的测试都必须自带这台机器的描述——否则断言的是开发者本机装了什么，而不是产品行为。

V2-12 评测语料与发布门槛的回归测试：

- `test_evaluation_gates.py`：指标取值形状（比率 / P-R-F1 三元组 / 覆盖率）、绝对下限与上限的通过和失败、`unavailable` 归环境类、样本不足与指标未发布归标注类、模式过滤逐模式判定、相对回退限制（无基线时不判定、小幅回退通过、超过允许跌幅失败、基线没有该模式时归数据类）、门槛文件校验（缺文件、空 gates、缺边界、未知 error_class、非数字边界、往返序列化）、错误分类表与 10 层一一对应、提交的 `quality-gates.json` 只点名词表里真实发布的指标，以及 `tools/evaluate.py` / `tools/judge_gates.py` 两个入口的退出码、错误分类输出与 `--baseline` 回显。

不可变修订与原子发布的回归测试：

- `test_durable_state.py`：修订身份、归档去重、append-only review events、确认字典校验、锁、更新版本状态只读拒绝，以及 bundle 的哈希/清单/定位校验。
- `test_index_snapshots.py`：失败构建不移动指针、未校验快照不可发布、文件被占用时旧索引仍可读、`CURRENT.json` 损坏后的恢复计划与保留策略。
- `test_schema_migration.py`：v2 需显式迁移、迁移结果与备份、迁移失败还原、回滚，以及旧/更新版本的显式拒绝。
- `test_index_freshness.py`：六层新鲜度各自独立报告，以及 `index_status` 保持 V1 字段并附带 `freshness`。
- `test_revision_chain.py`：端到端验收——删除重建派生索引后 Review Events、确认字典和逻辑文档身份仍在；同路径内容变化新增修订且历史 bundle 仍可校验。
- `test_cli_durable_state.py`：`cli index` 的项目根推断与 `--project-root` 覆盖，以及发布后登记持久状态（含无法推断项目根时只保留派生索引）。

分阶段处理与能力包的回归测试：

- `test_processing_pipeline.py`：单个 stage 独立重试只新增 Stage Attempt、指纹变化只失效该 stage 及下游、必选能力缺失时明确降级而 Core 继续、相同指纹安全复用而不同指纹不复用、运行清单能回答运行时/引擎/降级链、缓存与索引目录保持分离。
- `test_capability_runtime.py`：能力包状态到 stage 执行状态的唯一映射、硬件不足的显式报告、`ModelStore` 只从本地目录安装并校验 checksum、idle timeout / Low-memory Mode / 显式卸载 / 真实子进程退出都能释放驻留，以及 OCR 降级链的逐级记录。

区域级 OCR 的回归测试：

- `test_ocr_regions.py`：状态映射表的每一行（成功、部分输出、超时、损坏图片、缺语言包、缺模型、格式不支持、无可用引擎）、`partial` 永不判 accepted、质量门槛的逐条失败原因、三类置信度分列且拒绝单一总分、`evidence_state` 不允许 `explicit`/`verified`。
- `test_ocr_images.py`：用注入 provider 覆盖 DOCX 内嵌图片与独立 PNG/JPEG、损坏图片、超时、缺语言包、缺模型、降级链的停止与下钻、Raw 不被规范化覆盖、逐 span 变更回放、关键标记逐类评分，以及不传新参数时 V1 路径与 `index_status` 字段保持不变。

布局与纵向箭头关系的回归测试：

- `test_layout.py`：规格样例逐条对照——直线流程、空白行、缩进、分支、连续箭头、缺失端点、多列边界；确认的 `next_step` 必须能回溯到箭头和两个 Flow Node，缩进、装饰箭头与跨容器近邻都不产生 confirmed 关系，箭头块不会被当成 Flow Node。
- `test_layout_index.py`：注入 provider 后检查落库结果——`layout_runs` / `layout_elements` / `structural_relations` 的端点、几何依据、规则版本与分列置信度；没有区域时仍写空 run；删除文档连带清掉布局行；`index_status` 的 `layout_*` 明细；缺表的老索引报 0 而不是报错；`get_image_context` 能把顺序与关系读回来。

分层证据包、资产访问与独立图片导入的回归测试：

- `test_evidence_package.py`：unit id 只接受 `evidence:<id>` / `image:<id>`，路径与行选择器一律拒绝；section 过滤、未知层报错；asset reference 由索引目录名与相对路径推导、路径不会被误认、跨索引不相等；Display Locator 文案；cursor 只能来自上一页。
- `test_mcp_evidence_package.py`：正文单元把 statement 与来源引用分层，缺层报 `unit_is_not_an_image` 而不是空数组；图片单元的逐区域 / 逐元素 / 逐关系条目都能回溯到 Source / Parse Revision 与原始区域；分页按层展开且页边界不拆断来源关联；`include_v2_metadata` 是显式开关，默认响应不含任何 V2 字段；`get_asset` 只认本索引签发的引用并校验内容哈希，传路径会被拒；批量部分成功保留已取到的包；`get_processing_manifest` 与 `index_status.processing` 同源并给出 degradation。
- `test_mcp_document_import.py`：独立 PNG 的预览 / 确认 / 落盘 / 登记（`document_type=image`、`relationship_id=standalone`），同名图片一律拒绝覆盖，以及批次失败后来源与旧索引可恢复。

策划记法字典与人工审核的回归测试：

- `test_notation_dictionary.py`：范围规范化与越界拒绝（绝对路径、盘符、`..`）、驳回的箭头始终是候选而从不成为字典条目、单个区域的确认不外溢到别处、`document_type` 范围只回答该类型、两个来源分歧必须留下裁决、更高来源可记为 authority、外部常识与项目读法并排隔离，以及新修订把旧确认变成 migration candidate。
- `test_review_actions.py`：应用必须携带预览返回的那一个 token、字典移动后 token 立即失效、`correct` 把旧值留在记录上、`reject` 之后可被后续事件推翻、拒绝候选只写日志、`ignore` 静默候选而不确认任何东西，以及整串动作前后派生索引与源文件的字节不变。
- `test_mcp_review.py`：五个工具经 MCP 暴露、`confirm` 同时需要 token 与确认标志、token 不再匹配时被拒、候选可被拒绝且不触碰字典。

分层详尽释义的回归测试：

- `test_explanation.py`：词汇与评测不变量对齐、规则句在状态变化词处拆成条件与结果、状态变化缺触发方式时成为 gap、数值与比较符原样保留（`≤` 保留、`<` 不改写、无单位记 `missing_unit`）、只有问题问目的时才列 `missing_design_intent`、记法只在 confirmed 范围内展开而未知符号保持原词、被拒绝的读法写成「装饰」而非事实、关系措辞是受控模板且不含因果词、缩进只出 hint、跨文档同主题不同值形成 conflict group（同文档不算冲突）、三档 profile 都保留冲突、三档都不丢证据状态/定位/不确定性、输入原文只在显式请求时才随 Atom 返回、coverage scope 逐条声明省略与未覆盖、分页覆盖每个 Atom 恰好一次、句子与 Atom 双向映射、`explanation_id` 随输入变化、Source-as-Data 提示注入不改变 Profile 与证据规则、润色器只能改写措辞（改数字被拒、抛异常回退）、以及没有来源的 Atom 被隔离并产生 Structured Uncertainty。
- `test_mcp_explanation.py`：两个工具出现在 MCP 工具表上；单个单元的释义与证据包里的 `sections.explanation` 是同一份（同样的 `explanation_id` 与 Atom 文本）；一个问题的三档释义都保留同一个 conflict group 且顶层 `conflicts` 暴露；`page_size=1` 逐页翻完不重复也不遗漏 Atom；来源里的提示注入样本文本不改变 Profile，只产生 `security.warnings`；未知 Profile 在任何查找之前就被拒绝（命中与否都一样）、不存在的 unit 报 `not_found`、没有命中的问题返回空解释与下一步建议。

V2 确定性混合检索与证据回读的回归测试：

- `test_retrieval.py`：exact 命中带 locator、section_path 与 V2 source_reference，短中文查询退回 `instr` 仍能命中，同一单元被 exact 与 lexical 同时命中时只出一条候选且 `match_type=exact`（exact 排在 lexical 前）；目录别名与记法条目双向扩展（带 `rule`）、范围不覆盖则不扩展、扩展数受 `MAX_ALIAS_EXPANSIONS` 限制且与 `confirmed_alias.expansions` 一致；XLSX 配置单元格可命中；跨文档冲突组成 Conflict Group（含被冲突扫描补回、`retrieved=false` 的一侧）且不产生任何赢家字段，单位差异只报单维度、版本与时间差异可见、同值两种措辞只报 Potential、完全相同互相印证；`not_found` 不填充弱候选；探索模式候选不进事实且计入 `held_back_unconfirmed`；缺命名空间报 `degraded` 而 FTS5 仍答，调用方传入的 `degradations` 同样降级；四种 mode 的语义（`auto`/`lexical` 正常，`hybrid`/`semantic` 降级）；回读失败（行消失、来源哈希变化、locator 或关系不再可读）判为 untraceable；词汇表与 `CHANNELS`/`NAMESPACES`/`explanation.EVIDENCE_STATUSES` 对齐；空 query、未知 mode、越界 limit 被拒。
- `test_mcp_retrieval.py`：`retrieve_evidence` 出现在 MCP 工具表上且参数默认值与契约一致；确认别名经 MCP 返回产生它的规则；exact 命中回传可人工核对的行号定位；V1 `search_evidence` 的响应键集不变；请求 `hybrid` 时降级为 `degraded` 且仍然作答；确认字典不可读时记为 `unavailable_capabilities=["notation_dictionary"]` 并降级；缺命名空间时降级但仍返回 FTS5 命中；未知 mode 作为工具错误被拒而不是猜测；探索模式候选经 MCP 返回且仍非事实。

V2-10 可选本地向量召回与查询改写的回归测试（实验开关默认关闭）：

- `test_semantic_recall.py`：参考 provider 完全确定性且向量 L2 归一化、身份三元组与维度校验、provider 只在显式开关下加载（`module:attribute` 导入失败给出 `provider_not_importable`）；变体守门逐条核对空串/同文/超长/改数值/改单位/改版本/改时间/改否定/改范围（`VARIANT_CHANGES_*`）与 `MAX_GENERATED_VARIANTS` 上限，改写器抛错只影响该通道；侧车记录绑定 unit id、来源/输入哈希与模型身份，跨模型与跨维度不混搜、来源变化报 `stale`；`drop` 之后事实与 FTS5 照常可读；hybrid 命中回读为 `semantic_candidate` 且带相似度，同一 unit 多通道只出一条且分数不相加；弱命中只进 `possible_related` 且响应维持 `not_found`；`semantic` 模式下确定性通道全部 `skipped`；`auto` 只在词面没命中时才用向量；坏通道降级但确定性答案照旧；生成变体不修改字典（字典与 `.design-state/notation.json` 字节不变）；默认响应含新的 `semantic`/`query_rewrite`/`possible_related` 字段。
- `test_mcp_semantic.py`：`semantic_index_status`、`rebuild_semantic_index`、`drop_semantic_index` 出现在工具表上；开关为空时状态 `unavailable` 且 `provider_not_configured`、不产生侧车文件；`hashing:64` 时状态 `missing`、`model.dimension=64` 且 `rebuild_recommended=true`；未确认的重建/删除只返回 `confirmation_required`；hybrid 经已建侧车作答（`semantic_candidate` + 回读原文）、删除后 V1 `search_evidence` 仍返回 `exact` 且 hybrid 变 `degraded`；坏 provider 开关降级（`provider_not_importable`）且 V1 不受影响；换维度后报 `semantic_model_changed` 与 `incompatible`、重建后可再作答；坏 rewriter 开关报 `query_rewriter_not_importable` 且仍答题。

V2-11 离线能力包、校验与资源预算的回归测试：

- `test_offline_bundle.py`：一个合成的离线包逐字节验证（wheel 与模型的 SHA256、平台/解释器、pin 漂移、多出来的 wheel 与模型一律拒绝而不是顺手装上）；bundle 目录缺失、manifest 非法、bundle 版本与包名不认识时各自报专属原因；安装全程不启动任何子进程也不联网（在 `block_network` 内把 `subprocess.run` 换成断言），模型文件按 pin 拷贝、Python 侧只打印固定的 `--no-index --find-links ... --only-binary :all: --require-hashes` 命令与哈希 requirements；卸载需要确认、只删模型文件、保留 `facts_untouched` / `lexical_index_untouched` / `requires_reindex=false`，文件被占用时以 `capability_removal_failed` 停下并列出幸存文件；`capability_doctor` 报出磁盘、路径与 Tesseract 结论（缺 `chi_sim` 说缺哪个语言包、完全没有 Tesseract 报 `tesseract_missing`、`GAME_DESIGN_OCR_LANG` 会移动必需语言集合），相对路径与超长路径分别以 `model_root_not_absolute` / `model_root_path_long` 阻断；随包发布的清单与 `manifest_bytes()` 逐字节一致，`bundle.schema.json` 的 required 集合、bundle 版本与包名 enum 与本 build 对齐。
- `test_run_records.py`：三档 Profile 的预算各自独立且返回副本（改一份不影响另一份）、未知 Profile 在 `start()` 前就被拒绝；运行记录带该 Profile 的预算、延迟、峰值内存与磁盘前后值，降级事件逐条保留，异常路径（已降级后抛错）同样写出 `status="failed"` 的记录；JSONL 记录追加、缺失文件读成空、损坏行报出行号；没有能力包的机器上 `capability_baseline()` 仍然产出 `succeeded` 的记录，并把每个不可用包写成一条降级事件。
