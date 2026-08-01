# MCP 合规验证与分发指引

> 适用范围：本仓库所有自建 MCP（当前：simple-game-client）。
> 核心结论：**不合规风险 90% 来自手写协议层**。传输层用官方 SDK 兜底，业务逻辑不受影响。

## 第一步：验证是否规范（两个官方工具）

| 工具 | 用法 | 查什么 |
|------|------|--------|
| Inspector | `npx @modelcontextprotocol/inspector <你的启动命令>` | 可视化看 initialize 握手、tools/list、tools/call 全流程是否通 |
| conformance | `github.com/modelcontextprotocol/conformance` 跑一致性测试 | 自动化校验协议细节（字段、错误码、capability 协商） |

### 自测最容易翻车的点（手写协议层时）

1. `initialize` 响应结构（`protocolVersion`、`capabilities`、`serverInfo` 缺一不可）
2. 工具返回必须是 `content: [{type:"text", text:...}]` 数组，不能直接返回字符串
3. 错误要返回 JSON-RPC 标准错误对象（`code`/`message`），不是 HTTP 风格
4. `notifications/initialized` 之前不能发其他通知
5. `tools/list` 的 `inputSchema` 必须是合法 JSON Schema

### 30 分钟快速自查流程

```bash
# 1. 装 Inspector，对着你的 MCP 启动命令跑
npx @modelcontextprotocol/inspector node dist/server.js   # 换成实际启动命令

# 2. 界面里依次点：
#    Connect → List Tools（能列出 = 基本握手 OK）
#    选个工具 Run（能返回 = 调用链 OK）
#    看右侧通知/错误日志有没有红
```

## 第二步：让别人能用（分发规范）

| 项 | 做法 |
|----|------|
| 用官方 SDK 做传输层 | TS/Python SDK 处理协议细节，合规性自动达标——最省事的路 |
| 一键启动 | stdio 模式打包成 `npx your-mcp` 或 `uvx your-mcp`，用户零安装 |
| 配置示例 | README 给标准 `mcp.json` 片段（command/args/env），复制即用 |
| 环境变量 | 密钥/配置走 env，文档列清楚每个变量 |
| 发布 | npm/PyPI 发包 + 提交到官方 MCP Registry（modelcontextprotocol/registry） |
| 版本声明 | 写明支持的 spec 版本（如 2025-11-25）和 SDK 版本 |

## 分享前红线

**公司代码不得直接发公开仓库/Registry**——先脱敏（去掉内部接口、域名、密钥），或只发脱敏版模板。

## 长期建议

传输层用官方 SDK（TS：`@modelcontextprotocol/sdk`），业务代码不动，合规性由 SDK 兜底；Inspector 过一遍，基本就能放心分享。

## 工程规范参考（开源项目）

协议合规之上，工程结构/命名/架构/部署参考这三个（规范来源优先级：官方 > 主流框架 > 社区目录）：

| 项目 | ⭐ | 用途 |
|---|---|---|
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | 89k | **官方**参考实现合集：目录结构、测试、打包的标准范式，照抄即可 |
| [PrefectHQ/fastmcp](https://github.com/PrefectHQ/fastmcp) | 27k | 主流 Python 框架，examples 即工程模板（TS 生态看 [punkpeye/fastmcp](https://github.com/punkpeye/fastmcp)） |
| [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) | 92k | 社区目录：动手前查重、找同类高星实现参考、做完后提交收录引流 |
