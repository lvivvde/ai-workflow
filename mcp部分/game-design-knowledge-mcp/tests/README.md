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
