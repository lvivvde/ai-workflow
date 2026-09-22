# tests

存放自动化测试及脱敏测试夹具，重点覆盖：

- DOCX 章节、段落、列表、表格和嵌套结构。
- XLSX 多工作表、合并表头、公式、空行和数据类型。
- 文件变更后的增量更新与删除处理。
- 中文全文检索、精确 ID 查询和结果出处。
- MCP 工具的输入校验和错误处理。

两个兼容性安全网：

- `test_v1_contract.py`：直接对运行中的 MCP 服务比对 `src/game_design_knowledge/v1_contract.py` 冻结的 V1 契约（工具名、参数默认值、逐状态响应字段、定位字段），并验证读工具的 `index_status.is_stale` 与写工具的预览/应用字段集。
- `test_evaluation_harness.py`：为五条 Release-blocking invariant 各写一个注入违规的负例，另覆盖网络封锁、语料指纹、冻结语料和双人标注协议，并跑通整套评测。

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
