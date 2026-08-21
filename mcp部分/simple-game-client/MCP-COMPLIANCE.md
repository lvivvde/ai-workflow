# MCP 合规验证与团队使用规范

> 适用范围：本仓库所有自建 MCP（当前：simple-game-client）。
> 定位：**项目专用 MCP**——协议与具体游戏强绑定（不同游戏需各自实现），**不对外发布**（不发 npm/PyPI、不提 Registry）。
> 核心结论：MCP 层使用官方 SDK，并通过 Inspector 和 conformance suite 验证。自行实现的游戏协议层另做协议、版本和安全测试，不与 MCP 合规测试混为一谈。

这里有两层协议：

- **MCP 协议层**负责 initialize、工具发现、工具调用和错误返回，应使用官方 SDK。
- **游戏协议层**负责登录、心跳、封包、加密和业务消息，需要根据具体游戏实现和验证。

## 第一步：验证是否规范（两个官方工具）

| 工具 | 用法 | 查什么 |
|------|------|--------|
| Inspector | `npx @modelcontextprotocol/inspector <你的启动命令>` | 可视化看 initialize 握手、tools/list、tools/call 全流程是否通 |
| conformance | `github.com/modelcontextprotocol/conformance` 跑一致性测试 | 自动化校验协议细节（字段、错误码、capability 协商） |

### 绕过官方 SDK 手写 MCP 层时的高风险点

1. `initialize` 响应结构（`protocolVersion`、`capabilities`、`serverInfo` 缺一不可）
2. 工具返回必须是 `content: [{type:"text", text:...}]` 数组，不能直接返回字符串
3. 错误要返回 JSON-RPC 标准错误对象（`code`/`message`），不是 HTTP 风格
4. `notifications/initialized` 之前不能发其他通知
5. `tools/list` 的 `inputSchema` 必须是合法 JSON Schema

### 快速自查流程

```bash
# 1. 装 Inspector，对着你的 MCP 启动命令跑
npx @modelcontextprotocol/inspector node dist/server.js   # 换成实际启动命令

# 2. 界面里依次点：
#    Connect → List Tools：确认握手和工具发现
#    选择工具并 Run：确认调用和返回结构
#    检查通知与错误日志
```

Inspector 通过只能证明被检查的交互正常，不能替代自动化 conformance 测试、业务测试或异常路径测试。

## 第二步：团队使用规范（内部交接）

即使不对外发布，也要让团队成员能够安装、配置、验证和接手。交付游戏专用 MCP 时按以下清单检查。

### 接入规范

| 项 | 要求 |
|----|------|
| 一键启动 | stdio 模式，提供 `npx <pkg>`、`uvx <pkg>` 或等效的单命令启动方式 |
| 配置示例 | README 给标准 `mcp.json` 片段（command/args/env），复制即用 |
| 环境变量 | 服务器地址、端口、账号、密钥全部走 env；提供 `.env.example` 模板并逐项注释 |
| 版本声明 | README 写明：支持的 MCP spec 版本、SDK 版本 |

### 游戏协议特有规范

| 项 | 要求 |
|----|------|
| 游戏版本锚定 | README 必须写明**适配的游戏客户端版本号**；协议/opcode/密钥随游戏版本变化 |
| 版本升级流程 | 游戏更新后：重新核对协议表 → 跑 Inspector 自查 → 更新 README 版本号 |
| 协议文档 | 维护协议映射文档（opcode ↔ 含义 ↔ 来源代码位置），新人可接手 |
| 交接验证 | 交接前必须过一遍 30 分钟自查流程，截图或日志留档 |

### 保密红线

- **协议细节、服务器 IP/端口、密钥、内部域名不进公开仓库**（含本 ai-workflow 仓库——它只放通用设计文档与模板）
- 实际实现代码放内部仓库，访问权限限项目成员
- 公开分享仅限脱敏后的通用模板

## 实现原则

MCP 协议层使用官方 SDK（TypeScript：`@modelcontextprotocol/sdk`），并把 MCP 适配层与游戏协议、会话和业务代码隔离。SDK 可以减少手写协议错误，但版本升级后仍需运行 Inspector、conformance suite 和项目测试。

## 工程规范参考（开源项目）

协议合规之上，工程结构/命名/架构参考这三个（规范来源优先级：官方 > 主流框架 > 社区目录）：

| 项目 | 用途 |
|---|---|
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | 官方参考实现；用于核对目录结构、测试和打包方式 |
| [PrefectHQ/fastmcp](https://github.com/PrefectHQ/fastmcp) | Python 框架与示例；TypeScript 生态可参考 [punkpeye/fastmcp](https://github.com/punkpeye/fastmcp) |
| [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) | 社区目录；用于查重和寻找相近实现，不作为协议规范来源 |
