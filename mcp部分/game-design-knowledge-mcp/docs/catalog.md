# 人工玩法目录

`knowledge/catalog.json` 是正式玩法名称与别名的人工确认真源。索引和 MCP 工具只读取该文件，不会自动增加、修改或推断别名。

## 填写示例

```json
{
  "$schema": "catalog.schema.json",
  "version": 1,
  "features": [
    {
      "key": "由项目约定的稳定ID",
      "canonical_name": "策划确认的正式名称",
      "source": "确认该正式名称的文档或流程",
      "aliases": [
        {
          "name": "策划确认的外号",
          "source": "确认记录或文档",
          "confirmed_at": "2026-08-09",
          "confirmed_by": "确认人"
        }
      ]
    }
  ]
}
```

## 规则

- `key` 在项目生命周期内保持稳定，不因改名而变化。
- `canonical_name` 必须由策划确认，不能由 AI 从相似名称推断。
- 每个 alias 都必须记录来源、确认时间和确认人。
- 未确认外号不写入目录；查询时应返回 `not_found`。
- 修改目录后重新运行索引命令，全部校验通过后才会原子发布。
