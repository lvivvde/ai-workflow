# AGENTS.md — AI 搭建执行手册

> 本文件面向 AI agent。任务：在用户机器上搭建本仓库收录的 AI 工作流。
> 执行原则：逐项安装 → 验证 → 报告；任何一步失败就停下来报告，不要硬闯。

## 0. 前置检查

| 依赖 | 检查命令 | 要求 |
|---|---|---|
| Python | `python --version` | ≥ 3.10 |
| uv（可选，推荐） | `uv --version` | 有则 MCP 配置更优 |
| Node.js | `node --version` | ≥ 18（部分 skills 安装器需要） |
| Git | `git --version` | 任意 |

缺前置依赖时：报告用户并停止，不要擅自安装系统级软件。

## 1. MCP：code-review-graph

**用途**：代码库结构图谱，让 AI 只读必要文件，省 token（官方 benchmark：review 6.8×，日常最高 49×）。
**索引**：[mcp部分/code-review-graph/README.md](./mcp部分/code-review-graph/README.md)

### 安装

```bash
pip install code-review-graph      # 或 pipx install code-review-graph
code-review-graph install          # 自动检测并配置当前机器上的 AI 工具
```

### 验证

```bash
code-review-graph --version
```

并在目标代码库目录执行 `code-review-graph build`，能成功建图即完成。

### 使用规则（强制执行）

图谱构建完成后，本规则**永久生效**，优先级高于 AI 的默认行为：

1. **强制走图谱**：查代码、定位符号、分析调用关系，一律使用 code-review-graph 的 MCP 工具查询。
2. **禁止全量扫描**：不得对整个项目做目录遍历 / 全文 grep / 批量读文件。
3. **最小路径例外**：图谱确实答不了、必须读源文件时——
   - 只允许读**最小范围的指定路径**（单文件或单目录，禁止从根目录递归）
   - **每次都必须先向用户弹确认框**，说明要读什么路径、为什么图谱给不了
   - 用户拒绝则停止，不得绕过
4. **图谱保鲜（约 3 天周期）**：
   - AI 无后台定时能力，执行方式为：**每次会话开始时**检查图谱最后构建时间，超过 3 天未更新则提示用户
   - 提示语示例：💡 代码图谱已 N 天未更新，代码可能已变化，建议重新 build。要更新吗？
   - **重建前必须用户确认**；确认后执行 `code-review-graph build`（增量更新）
5. 图谱未构建的项目：先向用户提议建图，确认后执行 build，之后按以上规则走。

### 回退

```bash
code-review-graph uninstall --dry-run   # 先预览
code-review-graph uninstall             # 确认后执行
```

## 1b. MCP：mongodb-mcp-server（官方）

**用途**：AI 直连 MongoDB（查文档、聚合、索引、schema 推断）。
**索引**：[mcp部分/mongodb-mcp-server/README.md](./mcp部分/mongodb-mcp-server/README.md)

### 安装

无需安装，`npx` 直跑。`mcp.json` 片段见索引文档。

### 使用规则（强制执行）

- **生产/线上库一律 `--readOnly`**；写操作只允许指向内网测试库
- 连接串只走环境变量 `MDB_MCP_CONNECTION_STRING`，**不得写进任何提交到仓库的文件**
- 涉及删改操作（非只读模式时）必须先向用户弹确认框

## 2. Skills：mattpocock/skills

**用途**：30+ 个 AI skills（grill-me、handoff、code-review、tdd 等）。
**索引**：[skill部分/mattpocock-skills/README.md](./skill部分/mattpocock-skills/README.md)

### 安装

```bash
npx skills add mattpocock/skills
```

或按需手动复制：从上游仓库 `skills/<分类>/<名称>/SKILL.md` 复制到当前 AI 工具的 skills 目录。

### 验证

在当前 AI 工具中触发任一 skill（如输入 "grill me"），能正常响应即完成。

### 注意

- 不必全装，按索引清单挑选需要的即可
- deprecated / in-progress 分类的不要装

## 2b. Skills：superpowers

**用途**：完整 agentic 开发方法论（spec → 计划 → TDD → subagent 编排），14 个 skill。
**索引**：[skill部分/superpowers/README.md](./skill部分/superpowers/README.md)

### 安装

按当前 AI 工具选择对应方式（见上游 README Installation）。Claude Code 示例：

```bash
/plugin install superpowers@claude-plugins-official
```

### 验证

启动新会话，观察 agent 是否在开发任务时自动进入"先澄清需求、出 spec 再动手"的流程。

### 特别规则

- superpowers **允许自动触发**（其设计即为 session-start 激活），是本仓库「弹确认框」铁律的唯一例外
- 但其产出的 spec/计划仍须经用户确认后才可执行实现

## 3. 工作流规则：Skill 使用指引

**铁律：任何 skill 不得自动启用（superpowers 除外，见 2b 节）。** 匹配到场景时，必须先向用户弹提示确认，例如：

> 💡 当前是功能需求类任务，建议先用 **grill-me** 澄清需求。要启用吗？

用户同意后才执行；用户拒绝则按原任务继续，不得反复提议。

### 场景匹配表（按任务类型选 skill）

**需求与方案阶段**
| 场景 | 推荐 skill | 作用 |
|---|---|---|
| 新功能需求 / 方案设计 / 想法模糊 | grill-me | 连环追问压测方案，摊开隐含假设 |
| 方案讨论需同步沉淀文档（ADR/术语表） | grill-with-docs | grill-me 变体，边问边产出文档 |
| 讨论已充分，要落成正式规格 | to-spec | 把对话直接合成为 spec（不再追问） |
| spec/计划要拆成可执行工单 | to-tickets | 拆 tracer-bullet 工单并标注依赖 |
| 超大工程（单个会话装不下） | wayfinder | 决策工单地图，跨会话逐个解决 |
| 不确定该用哪个 skill | ask-matt | skill 路由器，告诉你该走哪条流程 |

**开发阶段**
| 场景 | 推荐 skill | 作用 |
|---|---|---|
| 按 spec/工单写实现 | implement | 按规格执行实现 |
| 特性开发/修 bug，要先写测试 | tdd | red-green-refactor |
| 设计模块接口 / 提升可测性 | codebase-design | "深模块"设计词汇表 |
| 验证状态模型/UI 感觉对不对 | prototype | 一次性原型，用完即弃 |
| 技术调研 / 查 API 文档 | research | 只信一手来源，结论落 md |
| 领域术语统一 / 架构决策记录 | domain-modeling | 维护项目领域模型 |

**审查与修复阶段**
| 场景 | 推荐 skill | 作用 |
|---|---|---|
| 改完代码要审查（规范 + 是否符合 spec） | code-review | 双轴审查（Standards/Spec） |
| 报错/异常/性能退化，原因不明 | diagnosing-bugs | 疑难 bug 诊断循环 |
| 扫描架构腐化点 | improve-codebase-architecture | 可视化报告 + 逐项 grill |
| 合并冲突卡住了 | resolving-merge-conflicts | 解 merge/rebase 冲突 |
| issue/外部 PR 分流处理 | triage | 状态机流转 + 写 agent 可执行的 brief |

**协作与收尾**
| 场景 | 推荐 skill | 作用 |
|---|---|---|
| 会话太长/要换人（或换 AI）接着干 | handoff | 压缩上下文成交接文档 |
| 想学某个新概念 | teach | 在当前工作区内教学 |
| 想写/改自己的 skill | writing-great-skills | skill 写作规范 |

**环境防护（misc，一次性配置）**
| 场景 | 推荐 skill | 作用 |
|---|---|---|
| 防止 AI 执行危险 git 命令 | git-guardrails-claude-code | 拦 push/reset --hard/clean 等 |
| 配 pre-commit 钩子 | setup-pre-commit | Husky + lint-staged |
| 首次启用 engineering 系列 | setup-matt-pocock-skills | 配 issue tracker/术语/文档布局 |

> 组合示例：功能开发标准链路 = grill-me → to-spec → to-tickets → implement（可选 tdd）→ code-review

## 4. Qoder 部署指引

目标平台为 Qoder 时，按本节执行（与通用流程的差异在此）：

| 项目 | 支持程度 | 部署方式 |
|---|---|---|
| code-review-graph (MCP) | ✅ 官方支持 | 同上：`pip install code-review-graph && code-review-graph install`，会自动检测配置 Qoder |
| mattpocock/skills | ✅ 兼容（手动） | 从上游仓库复制需要的 `skills/<分类>/<名称>/` 整个文件夹到 `.qoder/skills/`（项目级）或用户级 skills 目录 |
| superpowers | ⚠️ 部分（手动） | 复制上游 `skills/` 下各 skill 文件夹到 `.qoder/skills/`；官方插件机制 Qoder 不可用 |

### superpowers 在 Qoder 上的补偿措施

其"session-start 自动激活"依赖插件 hook，Qoder 跑不了。用 Qoder 的 Rules 机制补偿——在项目 Rules 中加入：

```
开发类任务必须遵循流程：先澄清需求并产出 spec → 用户确认 → 写实现计划（TDD、YAGNI、DRY）→ 用户确认 → 再动手实现。
```

### Qoder 原生能力备忘

- Skills：`.qoder/skills/`（项目级），自然语言自动触发 + `/名称` 手动触发
- MCP：官方支持，code-review-graph install 会自动写入配置
- 自定义 Subagent：`~/.qoder/agents/*.md`（用户级）或 `${project}/.qoder/agents/*.md`（项目级），frontmatter 可绑定 skills 与 mcpServers
- Rules：始终生效，适合放全局行为约束（如上面的流程规则、本文件第 3 节的确认框铁律）

## 5. 完成后报告

向用户报告：
1. 每项的安装状态（成功/跳过/失败 + 原因）
2. 验证结果
3. 需要用户手动操作的部分（如重启编辑器）
