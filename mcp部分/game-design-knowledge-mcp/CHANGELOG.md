# Changelog

## Unreleased

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
