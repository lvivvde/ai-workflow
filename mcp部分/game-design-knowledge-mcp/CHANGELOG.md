# Changelog

## Unreleased

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
