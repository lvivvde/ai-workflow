# 能力包清单与离线包

这个目录保存**版本锁定**的能力包清单，以及离线安装包的格式定义。它不含任何二进制，也不含模型文件：清单只是把代码里声明的 pin 固化成可 diff、可校验的文件。

```text
capabilities/
├── manifests/          # 由 capabilities.py 生成的能力包清单（core / enhanced_ocr / visual）
├── locks/              # 一次真实解析的记录：完整 wheel 闭包 + 每个 wheel 的 SHA256
└── bundle.schema.json  # 离线安装包 bundle.json 的格式定义
```

## manifests/

每个 `<包名>.json` 是 `CapabilityPack.as_payload()` 的原样输出：用途、许可证、CPU/内存/磁盘下限、体积、Python 依赖的**发行版名 + 精确版本**、模型文件的相对路径与 SHA256。

清单由代码生成，不允许手改：

```powershell
uv run game-design-knowledge capability manifests --write capabilities\manifests
```

测试会逐字节比对这三个文件与 `capabilities.py` 当前声明，任何漂移都会失败，所以"版本锁定"是仓库状态而不是文档承诺。

## locks/

`<包名>-<平台>-<ABI>.json` 记录该包闭包的**来源**：顶层 pin、解析出的每一个 wheel（发行版名、版本、文件名）以及文件的 SHA256，并在 `platform` 里写明是哪台机器、哪个解释器上解析的。有了它，"pin 指向一个索引里不存在的版本"这类错误就不再只能在联网解析时才发现：清单里的闭包必须与 lock 逐条相同，测试比对，多一个少一个都失败；只带 wheel 不带 sdist 也由测试保证，因为离线安装用 `--only-binary :all:`。

## bundle.schema.json

描述离线安装包的 `bundle.json`：包内装的是哪个平台、哪个 Python、哪些 wheel（发行版名、版本、文件名、SHA256）和哪些模型文件（相对路径、SHA256）。

离线包的目录结构：

```text
<bundle>/
├── bundle.json
├── wheels/<包名>/<文件名>.whl
└── models/<包名>/<相对路径>      # 与模型仓库内的相对路径一致
```

安装、校验、卸载与排障流程见 [`../docs/capabilities.md`](../docs/capabilities.md)。
