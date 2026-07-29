# superpowers

> 本目录为索引，不镜像项目本体。
> 上游仓库：https://github.com/obra/superpowers （⭐263k，MIT 协议）
> 作者：Jesse Vincent（obra）

Agentic skills 框架 + 完整软件开发方法论。GitHub 上最火的 agent skills 项目。

## 是什么

不是普通 skill 合集，而是一套让 AI agent 按规范流程开发的方法论：

1. 从对话中提炼 spec，分段展示给你确认
2. 确认后产出实现计划（强调真 red/green TDD、YAGNI、DRY）
3. 说"go"后启动 **subagent 驱动开发**：派多个子 agent 分工执行、互相审查，可连续自主工作数小时不跑偏

**自动触发**：靠初始指令 + session-start hook，agent 启动即激活，无需手动喊触发词（本仓库规则中唯一允许自动触发的 skill 集）。

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

理念同源（澄清需求 → spec → 计划 → TDD），superpowers 额外提供 subagent 编排与 hook 层，更重、更体系化。两者可互补：

- 轻量单点能力（grill-me、handoff 等）→ mattpocock/skills
- 全流程方法论与多 agent 编排 → superpowers
