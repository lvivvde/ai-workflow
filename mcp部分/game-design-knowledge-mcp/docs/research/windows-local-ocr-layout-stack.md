# Windows 本地 OCR、版面分析与视觉模型候选栈调研

调研日期：2026-09-09

## 结论摘要

本票据只收敛候选事实，不替后续“选择 Windows 本地处理栈与资源预算”做最终选型。

建议把以下组合带入选型票据：

1. **默认 OCR 候选：RapidOCR + ONNX Runtime CPU**。它能原生返回文本行四边形、文本、置信度和耗时；官方安装路径是 Python wheel，默认推理后端为 ONNX Runtime，适合 Windows CPU 基线。
2. **高精度 OCR 候选：PaddleOCR PP-OCRv5 mobile/server**。它提供更完整的中文、英文、特殊符号与文本方向能力，但 Paddle 运行时、模型和内存成本明显更高，且 Windows 上的高性能推理官方建议 Docker 或 WSL。
3. **结构分析候选：PP-StructureV3 按需启用，而非默认加载**。它能够输出版面检测、阅读顺序、表格/图表等结构化 JSON，但完整流水线更重，不适合作为“任意 Windows CPU 机器默认开启”的未经验证假设。
4. **兼容降级：继续保留 Tesseract**。现有代码只读取纯文本；Tesseract 本身也能输出 TSV、hOCR、ALTO 和 PAGE，因此即使不换引擎，也可先补回文本框与置信度。但它仍不负责 UI 结构、流程关系或设计含义。
5. **箭头和层次：OpenCV 确定性几何层**。OCR 只负责文字与框；箭头、连接线、缩进、垂直邻接和父子候选应由独立几何解析器产生，并将歧义显式保留。
6. **可选本地视觉解释：Qwen2.5-VL 3B 级模型，仅作低信任补充**。官方模型卡声明它能分析文本、图表、图标与布局并输出坐标/JSON；Ollama 提供原生 Windows 运行路径。其数 GB 模型体积和生成式不确定性使它不应成为事实或箭头关系的唯一来源。

最终是否采用 RapidOCR、PaddleOCR 或双后端，必须用项目真实策划截图做开发期 benchmark。官方通用 benchmark 不能替代该语料。

## 当前实现基线

当前 `indexer.py::_run_ocr`：

- 通过 `PATH` 查找系统 `tesseract`；
- 默认语言为 `chi_sim+eng`；
- 调用 `tesseract <image> stdout -l <lang>`；
- 只保存整图纯文本、状态和错误；
- 没有保存文本框、文本行、字词置信度、方向、阅读顺序或版面区域。

因此当前短板分成三层，不能只用“换一个 OCR”概括：

- **Text transcription**：字是否识别正确、是否有坐标与置信度；
- **Visual interpretation**：可见区域、控件、表格、阅读顺序；
- **Vertical flow notation**：文本行、箭头、缩进与相邻节点形成的关系。

## 候选比较

| 候选 | 本地 Windows / CPU | 结构化输出 | 主要优势 | 主要限制 | 许可证 |
|---|---|---|---|---|---|
| RapidOCR + ONNX Runtime | 官方 Python 安装为 `rapidocr` + `onnxruntime`；ORT 提供 Windows CPU wheel | 行级四边形、文本、分数、可选 word boxes、阶段耗时 | 轻量、离线、Python 接入直接；RapidOCR wheel 文档称约 27.2 MB 并内含小模型 | 是 OCR 工具箱，不是 UI/流程图语义解析器；准确率仍需真实语料测量 | RapidOCR Apache-2.0；ORT MIT |
| PaddleOCR PP-OCRv5 | 官方提供 CPU/GPU wheel；普通推理可 Python 调用 | 文本检测框、识别、方向及 pipeline JSON | 中文、英文、日文、特殊符号与复杂文本覆盖更强；mobile/server 可选 | 运行时与资源更重；PP-OCRv5 比 v4 慢；Windows 高性能推理路径官方建议 Docker/WSL | Apache-2.0 |
| PaddleOCR PP-StructureV3 | 可本地运行，但完整高性能部署对 Windows 不够轻 | 版面框、阅读顺序、表格/图表等结构化 JSON/Markdown | 能补足 OCR 之外的文档布局 | 训练类别偏文档页面，不等于游戏 UI 组件或箭头语义；完整 pipeline 的内存/GPU成本较高 | Apache-2.0 |
| Tesseract | 已接入；Windows 仍需外部发行包与语言数据 | 纯文本、TSV、hOCR、ALTO、PAGE | 现有兼容性好、离线、稳定；无需新增 Python ML runtime | 当前集成浪费了结构输出；对截图 UI、符号和流程关系没有专门语义 | Apache-2.0 |
| OpenCV 几何解析 | Python wheel 可本地 CPU 使用 | 轮廓、连通域、线段和自定义关系图 | 对箭头/连接线可做可测试、可追溯的确定性处理 | 需要项目自己的规则与样例；不能识别文字或理解设计意图 | Apache-2.0（现代版本） |
| Ollama + Qwen2.5-VL 3B | Ollama 原生支持 Windows 10 22H2+；CPU 可运行但体验待测 | 生成的描述、框/点、JSON | 能补充 UI、图标、布局和“图上是什么”的自然语言描述 | Ollama Windows 安装需要至少 4 GB 空间，模型另计；官方 3B 量化包约 3.2 GB；生成内容不可作为 source evidence | 模型与运行时许可证需在定版时逐项固定、复核 |

## 关键事实与证据

### RapidOCR 与 ONNX Runtime

- RapidOCR 官方安装文档推荐 `pip install rapidocr onnxruntime`；2.0.6 以后 ONNX Runtime 不再被自动依赖，但仍是默认推理引擎。文档还说明 wheel 约 27.2 MB，内含检测、文本行方向分类和识别三个小模型。[RapidOCR 安装指南](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/install/)
- RapidOCR 的结果对象公开 `boxes (N,4,2)`、`txts`、`scores`、可选 `word_results` 以及分阶段耗时。这恰好覆盖当前索引缺失的文本行坐标和置信度。[RapidOCR 使用教程](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/)
- RapidOCR 当前统一包支持 ONNX Runtime、OpenVINO、MNN、Paddle、TensorRT 和 PyTorch 等后端，仓库声明支持离线、多平台、中英文并采用 Apache-2.0。[RapidOCR 官方仓库](https://github.com/RapidAI/RapidOCR)
- ONNX Runtime 官方为 Python 提供 `onnxruntime` CPU wheel 和 `onnxruntime-gpu` CUDA wheel；Windows 需要 Visual C++ 2019 runtime。DirectML 仍受支持但处于 sustained engineering，微软建议新 Windows 项目考虑 WinML。[ONNX Runtime 安装文档](https://onnxruntime.ai/docs/install/)
- ONNX Runtime 仓库采用 MIT 许可证。[ONNX Runtime LICENSE](https://github.com/microsoft/onnxruntime/blob/main/LICENSE)

**研究判断**：RapidOCR/ORT 是最贴合“Windows CPU 默认可用”的候选，但它只能成为文字层。它对本项目的真实中文 UI 字体、低分辨率截图和符号的准确率没有一手证据，必须开发期测量。

### PaddleOCR 与 PP-StructureV3

- PaddleOCR 3.x 快速开始列出 PP-OCRv5、文本检测、文本识别和 PP-StructureV3 的 CLI/Python 接入，并要求 PaddlePaddle 3.0+。[PaddleOCR Quick Start](https://paddlepaddle.github.io/PaddleOCR/main/en/quick_start.html)
- PP-OCRv5 官方说明同时覆盖简体中文、繁体中文、英文、日文及复杂文本。官方模型表显示 mobile 与 server 模型在存储、速度和准确率间有明显梯度；例如 mobile 检测模型约 4.7 MB，mobile 识别模型约 16 MB，而 server 模型更大。[PaddleOCR OCR Pipeline](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/OCR.html)
- PP-OCRv5 官方性能说明明确指出其更大字典会比 PP-OCRv4 增加推理时间；官方 CPU 结果的内存数字达到 GB 级，但该测试硬件与本项目机器不同，只能用于识别资源风险，不能直接当作目标性能。[PP-OCRv5 Introduction](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5.html)
- PP-StructureV3 公开版面检测阈值、检测框合并、区域检测、图表识别等参数，并能保存结构化 JSON；官方介绍显示其版面类别主要面向标题、正文、表格、页眉页脚、公式、图表等文档对象。[PP-StructureV3 使用教程](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/PP-StructureV3.html)
- PaddleOCR 官方高性能推理文档说明完整 HPI 的 CPU/GPU支持以 Linux x86-64 为主，对 Windows 建议 Docker 或 WSL。这是 Windows 原生基线的重要部署风险。[高性能推理](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/deployment/high_performance_inference.html)
- PaddleOCR 官方仓库采用 Apache-2.0。[PaddleOCR 官方仓库](https://github.com/PaddlePaddle/PaddleOCR)

**研究判断**：PaddleOCR 应作为高精度/高级结构候选，而不是在未经验证前成为唯一强制依赖。PP-StructureV3 的类别不包含“游戏 UI 按钮”或“策划箭头关系”；将其输出解释为这些概念属于项目自定义映射，必须单独验证。

### Tesseract

- Tesseract 官方仓库声明支持纯文本、hOCR、TSV、ALTO 和 PAGE 等输出，并采用 Apache-2.0。[Tesseract 官方仓库](https://github.com/tesseract-ocr/tesseract)
- Tesseract 命令文档说明 hOCR 可以编码 bounding boxes 与 confidences，也能生成 TSV、ALTO、PAGE 以及字符框。[Tesseract 命令文档](https://github.com/tesseract-ocr/tesseract/blob/main/doc/tesseract.1.asc)
- 官方安装说明把引擎和语言 traineddata 视为两个独立部分；这意味着 Windows 分发必须同时锁定可执行文件来源与 `chi_sim`/`eng` 数据版本。[Tesseract 安装文档](https://github.com/tesseract-ocr/tessdoc/blob/main/Installation.md)

**研究判断**：短期可以把现有 plain-text 调用升级为 TSV 或 hOCR 来恢复框和置信度。但 Tesseract 仍只解决文字层，不能承担游戏 UI、流程图或设计意图理解。

### 箭头、连接线与层次关系

- OpenCV 提供轮廓、连通域与形状分析 API，也提供 Hough line / probabilistic line 检测，可用于候选线段和连接关系的确定性几何处理。[OpenCV Shape Analysis](https://docs.opencv.org/4.x/d3/dc0/group__imgproc__shape.html)、[OpenCV Hough Lines](https://docs.opencv.org/4.x/d9/db0/tutorial_hough_lines.html)
- OpenCV 当前仓库采用 Apache-2.0。[OpenCV 官方仓库](https://github.com/opencv/opencv)

**研究判断**：对“文字 → ↓ → 文字”这种 typed vertical flow notation，先用 OCR 框确定文本节点，再用 OpenCV 找箭头/线段和空间邻接，比让 VLM直接声明父子关系更可追溯。输出应保留几何证据、规则命中和不确定状态。

### 可选本地视觉模型

- Qwen2.5-VL 官方模型卡宣称模型能分析文本、图表、图标与布局，并可用 bounding boxes/points 定位对象、输出坐标及结构化 JSON。[Qwen2.5-VL 3B 模型卡](https://huggingface.co/Qwen/Qwen2.5-VL-3B-Instruct)
- Ollama 官方文档说明其原生 Windows 版本要求 Windows 10 22H2 或更新版本，可在本机提供 HTTP API；安装本体至少需要 4 GB 空间，模型还需额外空间。[Ollama Windows 文档](https://docs.ollama.com/windows)
- Ollama 官方模型库列出的 Qwen2.5-VL 3B 量化包约 3.2 GB，7B 默认包约 6.0 GB。[Ollama Qwen2.5-VL tags](https://ollama.com/library/qwen2.5vl/tags)

**研究判断**：3B 级本地 VLM 可以作为“图片大致是什么、有哪些区域”的可选 derived interpretation 后端。它不能替代 OCR 原文、确定性箭头图或用户确认的 designer notation dictionary；每次输出都应记录模型、版本、prompt、置信/不确定项，并允许完全关闭。

## 推荐带入后续选型的架构候选

### 候选 A：轻量 Windows 默认

- RapidOCR + `onnxruntime` CPU；
- 统一输出文本行 polygon、文字、分数、方向和耗时；
- OpenCV 解析箭头、线段、缩进和节点邻接；
- Tesseract 作为已安装环境的兼容降级；
- 不加载布局大模型或 VLM。

适合先满足“多数机器可离线运行”，但 UI 区域分类和复杂表格结构能力有限。

### 候选 B：双层能力包

- 候选 A 作为 `basic`；
- 可选 `enhanced` 安装 PaddleOCR PP-OCRv5 和 PP-StructureV3 的必要模块；
- 只在需要复杂版面/表格时调用，不默认加载完整 pipeline；
- 两层统一映射到同一 evidence package。

这最符合当前范围，但 Windows 原生安装、模型缓存、内存和启动时间必须先做 spike。

### 候选 C：可选视觉解释包

- 候选 A 或 B 提供 source evidence；
- 用户显式安装并启用 Ollama + Qwen2.5-VL 3B；
- 只生成 visual interpretation，不改变 source evidence 或确定性关系；
- 失败时降级为 OCR + geometry，不影响建索引。

该候选满足“图上大概是什么”，但应与默认 OCR 发布包解耦。

## 开发期必须验证的问题

1. RapidOCR 与 PP-OCRv5 mobile/server 在真实中文游戏 UI 字体、描边、阴影、低对比和小字号上的字符错误率。
2. 对 `↓`、`→`、`↑`、`+/-`、百分号、数值单位及中英混排的保真度。
3. OCR 行框排序能否正确还原纵向流程；表格、双栏和浮动文本框是否需要单独阅读顺序算法。
4. OpenCV 箭头解析在细线、短箭头、截图缩放、带背景纹理、分支/汇合下的精确率与召回率。
5. PaddleOCR/PP-StructureV3 在原生 Windows CPU 上的安装成功率、冷启动、峰值内存、单图耗时和离线模型分发。
6. RapidOCR 模型与 PaddleOCR 官方模型之间的版本/转换差异，以及固定模型 hash 后的可重建性。
7. Qwen2.5-VL 3B 在 CPU-only Windows 上是否有可接受等待时间；若不可接受，应维持 GPU-only optional 状态。
8. 所有第三方模型权重的具体版本、来源、hash 和许可证；不能只依据框架许可证推断每个模型文件的授权。

## 给后续决策票据的约束

- 不要把“OCR 后端”与“版面分析”“箭头关系”“视觉解释”合并为一个不可替换组件。
- 所有后端必须映射到统一结构；缺失能力应返回 `unavailable` 或 `partial`，不能伪造字段。
- source evidence 必须来自原文、OCR 框或确定性几何证据；VLM 输出只能进入 derived interpretation。
- Windows CPU 是安装和运行验收基线；GPU、PP-StructureV3 和本地 VLM 都应可选。
- 在真实 Golden Set 建立前，不宣称某候选“准确率最高”。
