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
