# mongodb-mcp-server

> 本目录为索引，不镜像项目本体。
> 上游仓库：https://github.com/mongodb-js/mongodb-mcp-server （⭐1.1k，**MongoDB 官方**出品，Apache-2.0）

## 是什么

MongoDB 官方 MCP server，让 AI 直接操作 MongoDB 数据库与 Atlas 集群：文档增删改查、聚合管道、集合/索引管理、schema 推断、连接状态检查。

## 快速开始

```bash
npx -y mongodb-mcp-server@latest --readOnly
```

连接串走环境变量：

```bash
export MDB_MCP_CONNECTION_STRING="mongodb://localhost:27017/myDatabase"
```

`mcp.json` 配置片段：

```json
{
  "mcpServers": {
    "mongodb": {
      "command": "npx",
      "args": ["-y", "mongodb-mcp-server@latest", "--readOnly"],
      "env": {
        "MDB_MCP_CONNECTION_STRING": "mongodb://localhost:27017/myDatabase"
      }
    }
  }
}
```

## 要点

- **默认建议 `--readOnly`**：只读模式防 AI 误写，官方所有示例默认带；需要写操作时才去掉
- 也支持 `--transport http` 远程模式（`--httpHost`/`--httpPort`）
- Atlas 用户可用 Service Account 做集群管理操作
- 生产库**务必只读模式**；写操作走内网测试库

## 文档

- 配置项全表、工具列表、Atlas 认证：https://github.com/mongodb-js/mongodb-mcp-server
