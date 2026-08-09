# docs

存放需要建立索引的策划原始资料，先按文件格式分类：

```text
docs/
├── docx/       # Word 策划案、需求说明、版本说明
├── xlsx/       # Excel 配置表、数值表、文案表
├── csv/        # CSV 导出表和轻量配置表
├── markdown/   # Markdown 方案、说明和决策记录
├── pdf/        # PDF 需求、参考资料和归档文档
└── pptx/       # PowerPoint 提案、评审和演示资料
```

分类目录只负责保存原始文件，不保存生成的 Markdown、中间 JSON 或 SQLite 索引，避免原文和派生数据混在一起。

当前正式版只解析 `.docx` 和 `.xlsx`。CSV、Markdown、PDF、PPTX 目录是资料分类与后续扩展预留，建立索引时会被跳过；旧版 `.xls` 需要先转换成 `.xlsx`。
