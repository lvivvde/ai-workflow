# code-review-graph

> 本目录为索引/笔记，项目主体不镜像到这里。
> 上游仓库：https://github.com/tirth8205/code-review-graph （⭐27.5k，MIT，Python 3.10+）

## 是什么

Local-first 代码智能图谱（MCP + CLI）。用 Tree-sitter 把代码库解析成结构图谱（节点：函数/类/导入；边：调用/继承/测试覆盖），存 SQLite，增量更新。AI 审查时通过 MCP 查询"爆炸半径"与风险分，只读必要文件，大幅省 token。

## 效果（官方 benchmark）

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

卸载（对称、安全、可预览）：

```bash
code-review-graph uninstall --dry-run   # 只预览不写入
code-review-graph uninstall             # 预览 + 确认后执行
```

## 支持平台

Codex、Claude Code、CodeBuddy Code、Cursor、Windsurf、Zed、Continue、OpenCode、Gemini CLI、Qwen、Kiro、GitHub Copilot 等。建议装 `uv`（有 uvx 时 MCP 配置更优）。

## 文档

- 用法：https://github.com/tirth8205/code-review-graph/blob/main/docs/USAGE.md
- 命令：https://github.com/tirth8205/code-review-graph/blob/main/docs/COMMANDS.md
- FAQ：https://github.com/tirth8205/code-review-graph/blob/main/docs/FAQ.md
- 排障：https://github.com/tirth8205/code-review-graph/blob/main/docs/TROUBLESHOOTING.md
- 官网：https://code-review-graph.com

