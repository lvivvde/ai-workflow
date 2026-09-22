# 不可变修订、原子快照与持久状态

派生索引（SQLite + `assets/`）随时可以删除重建。任何不能被重建的东西——人做过的决定、需要长期稳定的身份——都不放在派生索引里。

## 修订链

```text
project-relative path
        │  document_id(path)                    ← 逻辑文档身份
        ▼
logical document ──► source revision ──► parse revision ──► published bundle
                     hash(文档, 内容SHA256)   hash(来源, schema, 处理指纹)   hash(parse revision, 检索单元)
```

- 身份只由字节和配置推导，不含盘符、用户名或时间戳，所以在另一台电脑上重建会得到同一组 ID。
- 同一份内容重复出现仍会写入新的修订事件；归档对象按内容寻址，同哈希只存一份字节。
- 同一路径的字节变化会新增 Source Revision 与 Parse Revision，历史记录保持可读。
- Parse Revision 绑定 source revision、`database_schema_version` 和 Processing Fingerprint；指纹变化不会被当作命中缓存。

## 目录布局

```text
<项目根>/.design-state/          # 持久状态，可用 GAME_DESIGN_STATE_DIR 覆盖
├── manifest.json                # 小型索引：文档、来源/解析修订列表、active parse revision
├── dictionary.json              # 人工确认字典（含 confirmed_at / confirmed_by）
├── journal/
│   ├── source_revisions.jsonl   # append-only
│   ├── parse_revisions.jsonl
│   └── review_events.jsonl      # 撤销是一次新事件，不修改历史行
├── archive/objects/<sha256>.<ext>
├── bundles/<bundle-id>/{manifest.json,payload.json}
└── backups/                     # manifest 轮转备份与显式 backup()

<项目根>/.index/knowledge/        # 派生索引，可删除重建
├── knowledge.sqlite             # active 快照（V1 读取路径不变）
├── assets/                      # active 资产
├── CURRENT.json                 # current + last_known_good 指针（本机文件，不进 Git）
└── schema-backups/              # 迁移前备份

<项目根>/.index/.knowledge.build-<时间>-<随机>/   # 不可变快照，同级目录，保证相对路径深度不变
├── knowledge.sqlite
├── assets/
└── build.json                   # 状态机：planned → running → validated → published / failed
```

`.design-state` 默认不进 Git：归档字节会让仓库翻倍，而随仓库提交的人工真源是 `knowledge/catalog.json` 与 `docs/`。删除 `.index`（含快照）后重建，Review Events、确认字典和逻辑文档身份都不会丢失。

## 发布协议

1. 创建快照目录，并从 active 索引拷贝数据库与资产，保证增量复用仍然是增量。
2. 在快照内构建。构建失败留在 `failed` 状态，active 指针不动。
3. `validate()` 校验 `PRAGMA user_version`、`integrity_check`、`foreign_key_check`、必需表和 `assets/` 是否齐全。未通过校验的快照不可能被发布。
4. `publish()` 在锁内先替换 `assets/`，再用临时文件 + `os.replace` 替换 `knowledge.sqlite`，最后原子写 `CURRENT.json`。Windows 上文件被占用时报出明确的 `SnapshotError`，旧索引保持可读。
5. 发布后写 `index_builds` 行，把 build id、处理指纹和 parse revision 映射留在索引自身里。
6. 发布成功后才登记持久状态：归档来源字节、记录 Parse Revision、写出 Published Revision Bundle。顺序刻意放在发布之后，所以持久状态出问题不会回滚一个已经发布成功的索引，只会以 `status: not_recorded` 报告。

登记持久状态需要知道项目根。`cli index` 默认从 `<root>/.index/<name>` 的输出布局推断，也可以用 `--project-root` 显式给出；推断不出来时索引照常发布，只是不会被登记（`--project-root` 与来源文档不在同一个根时会打印 note 并跳过）。

`CURRENT.json` 损坏时 `IndexSnapshotStore.recovery_plan()` 会扫描快照目录，挑出仍然通过校验的最新快照给出恢复建议；`recover()` 在确认后才把它重新发布。

## 新鲜度分层

`index_freshness` 与 `index_status.freshness` 逐层报告，不再合并成一个布尔值：

| 层 | 含义 | 典型建议动作 |
|---|---|---|
| `source` | 项目文档字节与已归档 Source Revision 是否一致 | `rebuild_index` |
| `durable_state` | 持久状态是否存在、可读、schema 兼容 | `rebuild_index` / `wait_for_build` |
| `parse` | 每个文档是否有覆盖最新来源的 active Parse Revision | `rebuild_index` |
| `lexical_index` | 已发布索引是否通过校验并覆盖当前 parse revision | `rebuild_index` / `migrate_index` |
| `semantic_index` | 语义检索投影（本版本尚未实现） | `not_configured` |
| `explanation_cache` | 释义缓存（本版本尚未实现） | `not_configured` |

每层都给出 `expected_version`、`active_version`、`last_success_at` 和 `suggested_action`。

## Schema 迁移

```powershell
game-design-knowledge migrate --plan --database .index\knowledge\knowledge.sqlite
game-design-knowledge migrate --database .index\knowledge\knowledge.sqlite
```

- Schema v2 → v3：新增 `schema_migrations`、`index_builds`、`source_revisions`、`parse_revisions` 和 `documents` 的修订列。
- 迁移前备份到 `schema-backups/`；任一步失败或迁移后校验失败都会还原备份并报出原因。
- Schema v1 与比当前更新的版本一律显式拒绝，不会被静默误读。
