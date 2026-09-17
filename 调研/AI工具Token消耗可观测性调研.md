# AI 编程工具 Token 消耗可观测性调研

> 调研时间：2026-08-09  
> 范围：Codex CLI、Claude Code、Qoder，以及 ccusage、TokenTracker、LiteLLM、Langfuse、OpenLIT、Helicone。  
> 资料原则：仅使用项目官方 GitHub 仓库、厂商官方产品/API 文档；未做本机安装验证。

## 结论

- **个人跨工具对比**：使用 [TokenTracker](https://github.com/xiufengsun/TokenTracker)。它覆盖 Claude Code、Codex CLI 和 Qoder，适合做 MCP 开/关的 A/B 实验。采集值来自客户端本地 usage，不是供应商最终账单。
- **Claude Code 精确观测**：使用官方 [OpenTelemetry 监控](https://code.claude.com/docs/en/monitoring-usage)。它按 API 请求导出输入、输出、缓存读和缓存写 token；成本字段仍是估算。
- **团队 API/BYOK 治理**：让请求明确经过 LiteLLM 等网关，再集中统计 usage、spend 和预算。订阅 OAuth 或厂商积分流量如果绕过网关，网关无法观测。
- **官方余额核对**：保留各客户端的 `/status`、Credits 页面或 Usage API。当前没有统一接口能把三种订阅或积分换成口径一致的剩余 token。

最大的风险不是采集工具缺失，而是把不同口径混为一谈：本地 usage、网关 usage 和供应商账单回答的是不同问题。

## 先确认要比较什么

| 类型 | 数据来源 | 优点 | 关键限制 |
|---|---|---|---|
| 1. API 网关真实 usage | 请求经过 LiteLLM/Helicone 等网关，读取上游响应 usage，再按模型价表计费 | 每请求、集中、多用户，适合预算与审计 | 订阅 OAuth/积分客户端若直连厂商后端会绕过网关；网关成本仍可能是价表换算，不等于订阅额度扣减 |
| 2. 本地会话日志/数据库 | Codex、Claude Code、Qoder 在本机写入的 JSONL/SQLite/OTel usage | 不必改变订阅登录链路；可覆盖封闭的积分客户端 | 客户端升级可能改 schema；准确度取决于客户端是否完整写入 usage；不是官方结算记录 |
| 3. tokenizer/价表估算 | 对提示、回复或上下文重新分词，再乘公开价格 | 没有 usage 字段时仍可得到近似量级 | 隐藏 system prompt、工具结果、缓存、推理 token、重试通常不可见，误差可能很大；积分和 token 的换算也可能不公开 |

## 项目比较

| 方案 | 类型 | Codex CLI | Claude Code | Qoder | 实时性 | 判断 |
|---|---|---|---|---|---|---|
| [TokenTracker](https://github.com/xiufengsun/TokenTracker) | 本地日志/SQLite + 结束钩子；成本按 LiteLLM 价表换算 | 是：`config.toml` notify hook | 是：`settings.json` SessionEnd hook | 是：被动读 `Qoder/SharedClientCache/cache/db/local.db` 的 assistant `token_info` | Claude/Codex 会话结束后近实时；Qoder 取决于本地数据刷新/同步 | 跨工具本地对比；不是供应商账单真值 |
| [ccusage](https://github.com/ccusage/ccusage) | 读取 coding-agent 本地数据 | 是 | 是；还有 statusline 与 5 小时窗口 | 当前官方 README 的统一来源清单未列 Qoder | 手动运行/状态栏刷新；更偏报表 | 成熟的 CLI/JSON 报表工具，适合 Codex + Claude；若必须覆盖 Qoder，不如 TokenTracker |
| [Claude Code OTel](https://code.claude.com/docs/en/monitoring-usage) | 官方客户端从 API usage block 导出 OTel | 否 | **是** | 否 | 每个请求/近实时 | Claude Code 的官方工程观测口径；可按 user/team/model/skill/plugin/agent 分组。成本仍标为估算 |
| [LiteLLM](https://docs.litellm.ai/) | API 网关 | 条件式：必须让 Codex 明确走兼容网关/API key | **是**：Anthropic 官方给出 [Claude Code + LiteLLM 配置](https://docs.anthropic.com/en/docs/claude-code/llm-gateway) | 条件式：仅 BYOK/可改 API endpoint 的流量；Qoder 自有积分流量不可据现有资料确认 | 每请求/近实时 | 最适合团队 API 调用、集中 spend/budget；**不能旁路观察订阅 OAuth 流量** |
| [Langfuse](https://langfuse.com/docs/observability/features/token-and-cost-tracking) | SDK/集成接收 usage；缺失时 tokenizer 推断 | 无原生客户端接入证据 | 无原生客户端接入证据 | 无原生客户端接入证据 | 取决于接入链路 | 适合 LLM 应用可观测，不是现成的 AI 编程 CLI 采集器。响应中的 usage 比 tokenizer 推断更可靠 |
| [OpenLIT](https://github.com/openlit/openlit) | OpenTelemetry SDK/应用埋点 | 无原生客户端接入证据 | 可作为 OTel 后端思路，但本次未确认官方即插即用配置 | 无原生客户端接入证据 | 近实时 | 更偏自有 AI 应用的全栈 OTel；不是三客户端通吃的本地采集器 |
| [Helicone](https://github.com/Helicone/helicone) | AI Gateway + LLM observability | 条件式：请求必须经过其 base URL | 条件式：请求必须经过网关 | 条件式；自有积分流量通常不可见 | 每请求/近实时 | 与 LiteLLM同类：适合 API/BYOK 流量，不适合被订阅客户端绕过时的旁路统计 |

## 三个目标客户端的实际边界

### Codex CLI：本地 usage 为主，官方余额为辅

- TokenTracker 官方表格写明通过 `config.toml` 的 notify hook 自动接入 Codex CLI；其架构说明是“工具先生成日志，钩子触发同步，本地解析 token”。因此它不是拦截请求，而是会话结束后读取客户端数据。
- ccusage 官方 README 当前也明确支持 `ccusage codex daily`，可输出 daily/weekly/monthly/session/JSON 等聚合报表。
- OpenAI 官方文档明确区分“使用 ChatGPT 登录的订阅访问”和“使用 API key 的按量访问”。当前 Codex 定价文档还说明 Credits 按每百万 input/cached-input/output token 换算，CLI 内可用 `/status` 查看剩余额度；因此 `/status` 适合看官方剩余额度，TokenTracker/ccusage 更适合拆分本地实验的实际 token。[认证](https://learn.chatgpt.com/docs/auth) · [定价与用量](https://learn.chatgpt.com/docs/pricing)
- 只有把实际模型请求指向网关时，LiteLLM/Helicone 才能计数；不能假设安装一个网关后 ChatGPT/Codex 订阅流量会自动经过它。Codex 官方配置确实允许定义 `model_providers.<id>.base_url`，但当前自定义 provider 的 `wire_api` 只支持 Responses 协议，部署前需要验证网关兼容性。[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)
- 本次未找到 OpenAI 官方提供与 Claude Code OTel 对等的 Codex CLI 通用 token OTel 文档，因此 Codex 侧先以本地 session usage 为主，并保留“客户端版本改变日志 schema”的风险。

### Claude Code：官方 OTel 为主

- 官方 OTel 的 `input_tokens` 来自 API usage block，同时导出 output/cache-read/cache-creation token、request id、模型和请求上下文，因此比重新 tokenizer 更可靠。
- TokenTracker 与 ccusage 适合个人本地查看历史趋势；其中 ccusage 官方定位就是“Analyze coding (agent) CLI token usage and costs from local data”。
- Claude Code 官方也给出 LiteLLM 网关接法：设置 `ANTHROPIC_BASE_URL` 和认证 token；统一 endpoint 的好处包括一致的 cost/end-user tracking。但这意味着主动改成网关/API 调用链路，不是无侵入观察既有订阅流量。
- Claude Code 支持 Claude App Pro/Max 订阅登录，也支持 Console/API 与企业平台。订阅模式下，本地 OTel/token 日志可观测“模型实际处理量”，但不能据公开模型单价反推“还剩多少订阅额度”。

### Qoder：token 实验与 Credits 对账分开

- Qoder 官方说明 Credits 由模型和总输入/输出 token 共同决定，但展示的每任务 Credits 只是统计估值，实际按实时使用扣除；这说明 Credits 与 token 相关，却不是公开、恒定的一比一换算。[官方 Credits 文档](https://docs.qoder.com/Credits)
- Qoder 的组织 [Usage API](https://docs.qoder.com/account/teams/openapi/usage) 返回 timestamp、source、operation、model tier、credits/cost；当前文档响应字段没有 input/output token。因此它适合核对积分扣减，不足以直接做 token 级 MCP A/B。
- TokenTracker 官方 README 明确声称被动读取 Qoder 本地 `local.db` 中 assistant `token_info`、区分 cached input，并读取本地会话中的 Plan Credits/免费调用额度。这是目前找到的唯一同时覆盖三者的现成方案，但属于第三方解析内部 SQLite schema，必须用 Qoder Usage 页/Usage API 定期校验。
- Qoder CLI 官方文档说明支持通过 API key 接入第三方 provider；这种 BYOK 流量才有机会放到 LiteLLM/Helicone 后面。Qoder 自带积分模型的请求路由是否可替换、是否完整经过用户网关，本次官方资料不足，不能宣称支持。

#### Qoder 官方 API / SDK 的边界

| 能力 | 可观测数据 | 不应据此推断 |
|---|---|---|
| [Agent SDK](https://docs.qoder.com/cli/sdk/references) | 通过个人 PAT、Service Account 或本机登录态调用 Qoder Agent | 不等于普通 IDE 活动的全量历史 usage |
| [Cloud Agent](https://docs.qoder.com/cli/sdk/cloud-agent) | Cloud turn result 中的 usage、model usage 和 `total_cost_usd` | 仅覆盖经 Cloud Agent SDK 发起的 turn；该能力仍是 experimental/unstable |
| [Teams Usage API](https://docs.qoder.com/account/teams/openapi/usage) | 组织维度的 credits/cost | 公开响应没有 input/output/cache token，不能直接用于 token 级 A/B |
| Service Account scopes | `models.read`、`chat.completions` 等组织认证能力 | 不能证明个人订阅提供通用 OpenAI-compatible 模型端点 |
| `getUsageInfo()` | plan、总使用百分比和 quota bucket | 不是个人 Qoder IDE 的逐请求 token API |

## 落地路径

### 路径 A：个人跨工具对比

1. 先安装 TokenTracker，仅启用本地模式；运行 `tokentracker doctor`、`tokentracker status --json` 确认 Claude、Codex、Qoder 数据源都不是 skipped。
2. Claude Code 同时开启官方 OTel，以 OTel 的每请求 usage 作为 Claude 侧校验基线。
3. 每周抽样把 Qoder 的 Credits 汇总与官方 Usage 页/API 对账；不要把 TokenTracker 按公开价表计算的 USD 当作 Qoder 实际收费。
4. 保留 ccusage 作为无 GUI、可脚本化的 Codex/Claude JSON 报表备用工具。

### 路径 B：团队 API/BYOK 治理

把能配置 endpoint 的 Claude Code、Codex/API 客户端、Qoder BYOK 统一指向 LiteLLM，使用 virtual key 区分用户、工具和实验组，并由 LiteLLM 集中记录 usage、spend 与预算。再把 LiteLLM 数据送到 Langfuse、OpenLIT 或 Helicone 做长期分析。此路径只统计经过网关的 API 流量，不会自动覆盖厂商订阅 OAuth 或积分流量。

## MCP A/B 实验设计

目标应是比较“同等任务交付质量下的 token”，而不是只比较某次聊天总 token。

1. 固定同一工具、模型、版本、仓库 commit、系统/项目指令和任务文本；分别运行 MCP-off 与 MCP-on。
2. 每个条件至少重复 5 次，并交替顺序（ABBA 或随机）以减小缓存、服务波动和首次索引成本偏差。
3. 每次使用全新会话；MCP 预建索引成本单独记账，报告“首次摊销前”和“稳态”两套数据。
4. 最少记录：uncached input、cache read、cache creation/write、output、请求次数、耗时、是否完成、测试结果，以及 MCP 工具调用次数。
5. 主指标建议为 `完成任务的总 token / 成功次数`；另报 output token、缓存 token 与耗时。不要只看客户端显示的 context window 占用。
6. Claude Code 用官方 OTel 的 `claude_code.api_request` 按 session/run id 聚合；Codex/Qoder 用 TokenTracker 的时间桶/项目归属，实验前后记录时间戳并避免并行使用同一客户端。
7. 对 Qoder 额外记录官方 Credits 变化。token 下降但 Credits 不降，可能说明模型 tier、积分换算或后台多代理调用不同；不能把两者强行等价。

## 无法确认与风险

- 没有发现统一的供应商官方接口，能把 Codex ChatGPT 订阅、Claude Pro/Max、Qoder Credits 全部换算为同一“剩余额度 token”。这些产品的限额/积分并不等同公开 API 单价。
- TokenTracker、ccusage 的 token 可信度取决于各客户端落盘 usage 的完整性；项目 README 的“accurate”是项目方自述，不等于供应商审计承诺。
- TokenTracker 对 Qoder 的实现依赖内部 SQLite 路径与 `token_info` schema，Qoder 更新可能导致暂时失效。
- 公开价表只能给出“API 等价值”，不能代表订阅成本、积分实际扣减或厂商内部推理成本。
- Langfuse 官方明确区分 ingested usage 和 inferred usage，并指出 reasoning token 不可见时无法正确推断成本；任何 tokenizer-only 工具都应标记为估算。

## 一手来源

- [TokenTracker 官方 GitHub README](https://github.com/xiufengsun/TokenTracker)
- [ccusage 官方 GitHub README](https://github.com/ccusage/ccusage)
- [OpenAI 官方 Codex 认证文档](https://learn.chatgpt.com/docs/auth)
- [OpenAI 官方 Codex 定价与用量文档](https://learn.chatgpt.com/docs/pricing)
- [OpenAI 官方 Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Claude Code 官方 OpenTelemetry 监控文档](https://code.claude.com/docs/en/monitoring-usage)
- [Claude Code 官方 LiteLLM 网关配置](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [Claude Code 官方认证方式](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- [LiteLLM 官方文档](https://docs.litellm.ai/)
- [Langfuse Token & Cost Tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
- [OpenLIT 官方 GitHub README](https://github.com/openlit/openlit)
- [Helicone 官方 GitHub README](https://github.com/Helicone/helicone)
- [Qoder Credits 官方文档](https://docs.qoder.com/Credits)
- [Qoder Usage API 官方文档](https://docs.qoder.com/account/teams/openapi/usage)
- [Qoder CLI Model/BYOK 官方文档](https://docs.qoder.com/en/cli/model)
