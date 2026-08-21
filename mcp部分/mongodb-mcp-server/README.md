# mongodb-mcp-server

> 本目录为索引，不镜像项目本体。
> 上游仓库：https://github.com/mongodb-js/mongodb-mcp-server （MongoDB 官方，Apache-2.0）

## 是什么

MongoDB 官方 MCP server，向 AI 提供 MongoDB 数据库和 Atlas 集群操作，包括文档查询与修改、聚合管道、集合与索引管理、Schema 推断和连接检查。

## 快速开始

```bash
npx -y mongodb-mcp-server@latest --readOnly
```

在启动 MCP 客户端前，通过环境变量提供连接串：

```powershell
$env:MDB_MCP_CONNECTION_STRING = "mongodb://localhost:27017/myDatabase"
```

`mcp.json` 只保存启动命令，不写入连接串：

```json
{
  "mcpServers": {
    "mongodb": {
      "command": "npx",
      "args": ["-y", "mongodb-mcp-server@latest", "--readOnly"]
    }
  }
}
```

## 安全与部署要点

- 生产或线上数据库必须使用 `--readOnly`；写操作只允许指向内网测试库。
- 连接串只通过 `MDB_MCP_CONNECTION_STRING` 传入，不提交到仓库。
- 远程部署可以使用 `--transport http`，并通过 `--httpHost` 和 `--httpPort` 配置监听地址。
- Atlas 集群管理可以使用 Service Account；权限应限制到任务所需范围。

## 文档

- 配置项全表、工具列表、Atlas 认证：https://github.com/mongodb-js/mongodb-mcp-server
