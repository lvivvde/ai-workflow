# game-mcp-memory

`simple-game-client` 的可选长期记忆 Sidecar。它把 Mem0 OSS 作为 Python 库嵌入本地 HTTP 服务，记忆数据保存在本机 Qdrant 和 SQLite 中，模型调用通过 OpenAI-compatible API 完成。

## 当前范围

- 本机监听：`127.0.0.1:18765`
- 本地持久化：`data/qdrant` 与 `data/history.db`
- 默认模型：阿里百炼 `qwen3.7-flash`
- 默认向量模型：`text-embedding-v4`，1024 维
- 游戏 MCP 接入方式：HTTP，可选依赖
- 生产服自动执行、团队共享服务、Web 管理后台不在当前 POC 范围内

## 安全边界

- `.env`、`data/`、`logs/` 均被 Git 忽略。
- 不要把 API Key、游戏服 Token、密码、Cookie、私钥或完整连接串写入请求。
- `command-results` 会再次执行脱敏，但它不能替代调用方的最小化数据原则。
- POC 仅使用模拟玩家和测试服数据。
- Memory 不可用时，游戏 MCP 应继续工作，不能阻断或自动重放游戏指令。

## 快速启动

1. 复制配置模板：

   ```powershell
   Copy-Item .env.example .env
   ```

2. 在本地 `.env` 中填写 `MEMORY_API_KEY`。只使用公司批准的 API Key。

3. 安装锁定依赖并运行离线验证：

   ```powershell
   uv sync
   .\verify.ps1
   ```

4. 启动服务：

   ```powershell
   .\start.ps1
   ```

5. 打开：

   - 健康检查：<http://127.0.0.1:18765/health>
   - API 文档：<http://127.0.0.1:18765/docs>

## API 示例

写入一条经验：

```powershell
$Body = @{
    text = "测试服1.8版本的add_currency必须提供reason参数"
    metadata = @{
        environment = "test"
        command = "add_currency"
        server_version = "1.8"
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Post `
    -Uri http://127.0.0.1:18765/memories `
    -ContentType application/json `
    -Body $Body
```

搜索：

```powershell
$Body = @{
    query = "测试服增加金币需要注意什么？"
    filters = @{
        environment = "test"
        server_version = "1.8"
    }
    limit = 5
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Post `
    -Uri http://127.0.0.1:18765/memories/search `
    -ContentType application/json `
    -Body $Body
```

## simple-game-client 接入

参考 `examples/simple_game_client.py`：

- 执行命令前调用 `/memories/search`。
- 执行命令后调用 `/command-results`。
- Sidecar 超时或不可用时返回空记忆，不影响游戏连接与指令执行。

## 在另一台公司电脑部署

```powershell
git pull
uv sync
Copy-Item .env.example .env
# 填写公司批准使用的 API Key
.\verify.ps1
.\start.ps1
```

`.env` 和本地记忆不会通过 Git 分发，必须在每台电脑上单独配置。
