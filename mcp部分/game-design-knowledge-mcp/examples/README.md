# examples

存放可以提交到仓库的脱敏示例，不放任何真实项目资料。

AI 生成、可以公开上传的完整测试资料放在 `sample-corpus/`：

```text
examples/
└── sample-corpus/
    ├── docx/       # AI 生成的 Word 策划文档
    ├── xlsx/       # AI 生成的配置表、数值表和文案表
    ├── csv/        # AI 生成的 CSV 配置数据
    ├── markdown/   # AI 生成的 Markdown 方案
    ├── pdf/        # AI 生成或转换的 PDF 文档
    └── pptx/       # AI 生成的演示和评审材料
```

这些文件用于演示、手工验证和端到端索引测试。专门构造的边界或异常文件应放到 `tests/fixtures/`，不要混入示例语料。

当前示例验收只索引 `docx/` 与 `xlsx/` 中的 6 份文件；其他目录为后续格式扩展预留。
