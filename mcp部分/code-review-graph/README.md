# code-review-graph

> 本目录为索引/笔记，项目主体不镜像到这里。
> 上游仓库：https://github.com/tirth8205/code-review-graph （MIT，Python 3.10+）

## 是什么

Local-first 代码结构图谱（MCP + CLI）。它使用 Tree-sitter 解析函数、类和导入，并记录调用、继承和测试覆盖关系。图谱保存在 SQLite 中并支持增量更新；AI 可以先查询符号和影响范围，再读取必要的源文件。

## 上游 benchmark

以下数据来自上游项目公布的 benchmark，适用仓库、任务和测量方法以其原始报告为准：

- Code review：token 减少约 6.8×
- 日常编码任务：最高 49×
- 6 个真实仓库实测 38×–528×
- 500 文件项目初次建图约 10 秒

## 快速开始

```bash
pip install code-review-graph      # 或 pipx install code-review-graph
code-review-graph install          # 自动检测并配置所有支持的平台
code-review-graph build            # 解析当前代码库建图
```

指定平台：

```bash
code-review-graph install --platform codebuddy   # 也支持 cursor / claude-code / codex / copilot 等
```

卸载前先预览：

```bash
code-review-graph uninstall --dry-run   # 只预览不写入
code-review-graph uninstall             # 预览 + 确认后执行
```

## 支持平台

上游列出的平台包括 Codex、Claude Code、CodeBuddy Code、Cursor、Windsurf、Zed、Continue、OpenCode、Gemini CLI、Qwen、Kiro 和 GitHub Copilot。`uv` 是可选依赖；存在 `uvx` 时，安装器会生成相应的 MCP 配置。

## 文档

- 用法：https://github.com/tirth8205/code-review-graph/blob/main/docs/USAGE.md
- 命令：https://github.com/tirth8205/code-review-graph/blob/main/docs/COMMANDS.md
- FAQ：https://github.com/tirth8205/code-review-graph/blob/main/docs/FAQ.md
- 排障：https://github.com/tirth8205/code-review-graph/blob/main/docs/TROUBLESHOOTING.md
- 官网：https://code-review-graph.com
