# superpowers

> 本目录为索引，不镜像项目本体。
> 上游仓库：https://github.com/obra/superpowers （MIT）
> 作者：Jesse Vincent（obra）

Agentic Skills 框架和软件开发工作流，覆盖需求澄清、计划、TDD、并行执行、审查和分支收尾。

## 是什么

它把多个 Skill 组织成一条开发流程：

1. 从对话中提炼 spec，分段展示给你确认
2. 确认后产出实现计划（强调真 red/green TDD、YAGNI、DRY）
3. 用户同意执行后，可以使用 subagent 分工实现和审查

**自动触发**：上游通过初始指令和 session-start hook 激活。在本仓库规则中，它是唯一允许自动触发的 Skill 集；其产出的 spec 和计划仍须用户确认。

## 内含 14 个 skill

| 分类 | Skills |
|---|---|
| 规划 | brainstorming、writing-plans、executing-plans |
| 执行 | subagent-driven-development、dispatching-parallel-agents、test-driven-development |
| 质量 | systematic-debugging、verification-before-completion、requesting-code-review、receiving-code-review |
| 工程 | using-git-worktrees、finishing-a-development-branch |
| 元 | using-superpowers、writing-skills |

## 安装

支持 Claude Code、Codex(App/CLI)、Cursor、Gemini CLI、GitHub Copilot CLI、Kimi Code、OpenCode、Antigravity、Pi 等。各平台安装命令见上游 README 的 Installation 一节。

Claude Code 示例：

```bash
/plugin install superpowers@claude-plugins-official
```

## 与 mattpocock/skills 的关系

两者都覆盖“澄清需求 → spec → 计划 → TDD”。superpowers 还提供 subagent 编排和 hook 集成，可以按任务范围组合使用：

- 轻量单点能力（grill-me、handoff 等）→ mattpocock/skills
- 全流程方法论与多 agent 编排 → superpowers
