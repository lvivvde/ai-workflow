# ai-workflow

个人 AI 工作流仓库，收录 MCP、Skills、Agent 编排器和相关技术调研。仓库同时提供给人阅读的索引，以及 AI 可执行的安装与使用规则。

## 目录结构

```
ai-workflow/
├── README.md                    ← 仓库总览
├── AGENTS.md                    ← AI 搭建与使用规则
├── AI工具Token消耗可观测性调研.md
├── mcp部分/
│   ├── code-review-graph/
│   ├── game-design-knowledge-mcp/  （自建项目）
│   ├── game-mcp-memory/            （自建 POC）
│   ├── mongodb-mcp-server/
│   ├── simple-game-client/         （设计阶段）
│   └── simple-game-server/         （已否决方案的决策记录）
├── skill部分/
│   ├── clear-talk/                 （本地 Skill）
│   ├── mattpocock-skills/
│   └── superpowers/
└── orchestrators/
    └── symphony/                  （OpenAI Symphony 源码快照）
```

## 给 AI agent

搭建或使用本仓库工作流前，先阅读 [AGENTS.md](./AGENTS.md)。其中包含前置检查、安装顺序、验证方法、安全边界和 Skill 启用规则。

## 给人

第三方 MCP 与 Skills 子目录以索引文档为主，不镜像上游项目；安装命令以 [AGENTS.md](./AGENTS.md) 和各目录 README 为准。自建 MCP 包含源码和项目文档。需要本地改造的编排器源码放在 `orchestrators/`。
