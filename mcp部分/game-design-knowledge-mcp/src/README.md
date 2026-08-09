# src

存放可发布的 Python 实现代码，当前包为 `game_design_knowledge`：

- `cli.py`：原子 staging 构建与命令行入口。
- `indexer.py`：DOCX/XLSX 解析、图片提取、SHA 增量更新、SQLite schema 和 FTS5。
- `server.py`：只读 MCP 工具、证据查询、配置查询、目录解析和新鲜度检查。
- `policy.py`：只按文档事实回答、禁止自动联想的运行时规则。

当前不包含 CSV、Markdown、PDF 或 PPTX 解析器；支持范围以根目录 `README.md` 和 `docs/spec.md` 为准。
