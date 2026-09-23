# 视觉布局：阅读顺序、缩进与纵向箭头关系

图片里“文本 → 向下箭头 → 下一行文本”这种键入式记法，是可以从**可见几何**里还原的；它是不是一条规则、是不是因果、作者想表达什么，则不能。这份文档只说第一件事，并说明第二件事为什么被挡住。

规则集与实现：

```text
src/game_design_knowledge/layout.py         # Visual Elements、行/列、阅读顺序、depth_hint
src/game_design_knowledge/flow_notation.py  # Structural Relations（next_step / points_to）
src/game_design_knowledge/ink.py            # 箭头块自己的墨迹：方向需要像素佐证（arrow-ink-v1）
```

## 输入与输出

输入只有一样东西：同一次构建里、同一张图的 `ocr_regions`（边界框、原始文字、逐区域置信度）。没有独立的图像分析通道，也没有可选的视觉模型参与判定。

输出两样东西，分别落在 `layout_runs` + `layout_elements` 和 `structural_relations`：

```text
VisualElement  region_index, kind(text|arrow), direction, reading_order,
               row, column, depth_hint, bbox, text_confidence
ReadingOrder   elements, column_count, order_source, geometry_confidence,
               uncertainty, detail, ruleset_version
Relation       kind, status, source_region, target_region, via_regions,
               direction, geometry_basis, detail, uncertainty,
               geometry_confidence, ocr_confidence, rule_version, claim_boundary
```

## 阅读顺序

顺序完全由几何决定，且每一步都能说出理由：

| 步骤 | 规则 | 说得出的话 |
|---|---|---|
| 行 | 垂直方向重叠 ≥ 较短框的 50% 视为同一行 | “同一行”是测量结果，不是猜测 |
| 列 | 两组区间的水平投影互不相交，**且两列各自不止一个块、还共享至少一条行带** | “两列”只在真的并排时成立 |
| 缩进 | 同一列内，x 起点相差 ≤ 4px 视为同一层 | 输出 `depth_hint`，不输出父子关系 |
| 顺序 | 单列自上而下，多列先读完一列再读下一列 | 列数写进 `column_count` |

左侧缩进、右侧缩进这种“不同左边缘”不会被读成两列：两个条件必须同时成立，否则就是同一列的缩进。缩进只记录 `depth_hint`，**永远不产生 parent/next 关系**——这是 issue #3 的明确要求。

退化情况照实记录，不静默降级：

| 情况 | `uncertainty` | `geometry_confidence` |
|---|---|---|
| 区域没有边界框（全部或部分） | `missing_geometry` | 0.4，且 `order_source = region_index` |
| 同一行内两个框水平重叠 > 50% | `overlapping_boxes` | 0.7 |
| 两列间隙 < 8px | `ambiguous_columns` | 0.6 |

## 纵向箭头规则

一块文字要变成 `next_step`，必须**同时**满足（issue #3、spec #11 §3.5）：

1. 箭头是独立块：整块只有箭头字符，且方向一致（`↓`/`▼`/`↑`/`▲`，以及规范化后的 `->`）。
2. 它在自己那一行里是**唯一**的箭头块。
3. 上方恰好一个、下方恰好一个**与箭头同列对齐**的文字块。
4. 两端之间没有其它箭头块被跨过（跨过就是连续箭头）。
5. 两端同列、不互相重叠。
6. **方向站得住**：箭头块自己的像素指向与转写读出的方向一致（issue #28）。

任一条不成立就输出 `candidate` 并写明原因，而不是硬判：

| `uncertainty` | 触发条件 |
|---|---|
| `branch` | 同行有第二个箭头块；或某一行出现多个对齐块、箭头夹在两个块之间（分叉） |
| `cross_column` | 最近一行有文字，但全部落在另一列；或两端分属不同列 |
| `missing_endpoint` | 只有一侧有对齐文字；或附近根本没有文字行 |
| `consecutive_arrow` | 两端之间夹着箭头块，端点按整段箭头解算 |
| `ambiguous_direction` | 整块都是箭头但方向互相矛盾 |
| `overlapping_layout` | 端点框与箭头框重叠 |
| `direction_conflict` | 结构干净，但块的墨迹指向与转写方向相反 |
| `direction_unverified` | 结构干净，但这次构建没有（或读不出）块的墨迹佐证 |

横向箭头（`→`/`←`）只在**同一行内左右两侧各有一个文字块**时确认 `points_to`，说明“谁指向谁”；否则同样是 candidate。

## 方向为什么需要像素佐证

方向决定了关系的**哪一端是源**，所以一次镜像读法不是噪声，而是把整条流程反过来。语料构造期间实测（Windows 11 / RapidOCR 1.3.24，4 种字体 × 5 个字号、30 多种渲染）：

| 画的是 | 转写读成 | 转写置信度 |
|---|---|---|
| `↓↓` | `↑↑`（镜像） | 0.58 – 0.87 |
| `↑↑` | `↑↑` | 0.77 – 0.89 |

两个总体重叠，所以**任何**转写置信度阈值都分不开「读对了」和「读反了」。墨迹能分开：箭头块里最宽的一条墨迹带落在箭头上，于是“最宽墨迹带在框里的位置”是画面自身的事实（`arrow-ink-v1`，4 字体 × 5 字号实测）：

| 画的是 | 纵向 profile 带位置（占框高） | 横向 profile 带位置（占框宽） |
|---|---|---|
| `↑` / `↑↑` | 0.16 – 0.33（头靠上） | 0.45 – 0.50 |
| `↓` / `↓↓` | 0.65 – 0.82（头靠下） | 0.45 – 0.50 |
| `→` / `→→` | 0.24 – 0.46（**不干净**） | 0.56 – 0.80（头靠右） |
| `←` / `←←` | 0.24 – 0.46（不干净） | 0.34 – 0.43（头靠左） |

两条边界必须一起看，否则会得到漂亮的错答案：

- **轴由转写定，极性由像素定。** 实测到的误读都在同一族内（纵向读成另一个纵向），从不跨轴；而横向块的**纵向** profile 是「上重」的（头三角坐在杆上方），交叉轴读数不干净。所以轴取 `layout.arrow_axis()`，块的形状要和轴相符（纵向块至少不比它宽矮，横向块至少不比它窄高），极性才去量。
- **分不清就不表态。** 最宽墨迹带必须离框的中线超过一个余量（`INK_MARGIN = 0.10`）才给方向，否则（装饰、色块、不巧的框）这次构建不给读数，关系按 `direction_unverified` 记，而不是半确认。

结果是：转写与墨迹一致 → `confirmed`，并且 `geometry_basis` 里写出“最宽墨迹带在哪、与转写一致”；两者相反 → `candidate` + `direction_conflict`，关系的 `source_region`/`target_region` 仍按转写给出并**明确说明可能是反的**；没有读数（图片解不开、块没有边界框、块形状与轴不符、框里读不到形状）→ `candidate` + `direction_unverified`。结构性问题（分叉、跨列、缺端点、连续箭头、重叠）优先级更高：结构不干净时先报结构原因，方向的分歧仍会写进 `geometry_basis`。

## 置信度分列

每条关系同时带两个数，含义不同、不合并、不取平均：

- `geometry_confidence`：这次几何判断有多确定。确认=1.0，有明确竞争、跨列或两端都在但方向没证的 candidate=0.5，缺端点=0.2，整块箭头自相矛盾（`ambiguous_direction`）=0.1。
- `ocr_confidence`：这条关系依赖的区域里**最低**的文字置信度；引擎没给分数时是 `null`，不会被当成 1.0。

布局清楚但文字读得差，会明确表现为 `geometry_confidence = 1.0` 且 `ocr_confidence` 偏低——这正是需要人来核对的那种情况。

## 事实边界

`claim_boundary` 随每条关系一起返回并入库，内容固定为：可见布局只说明“哪块在箭头哪一侧”，**不是**因果、运行时依赖、前置条件或设计意图。

其他被明确禁止的推断：

- 缩进不产生父子关系；`depth_hint` 只是一个层号。
- 装饰箭头、跨容器近邻、连续箭头不产生 confirmed 关系。
- 单张图里的相邻关系不会升级为跨图的流程或依赖。

## 可选视觉模型

`visual` 能力包（Ollama + Qwen2.5-VL 3B）是可选的，且本轮**不参与**布局判定：

- 关闭或不可用时，确定性几何能力照常工作：文本块、行、列、阅读顺序和箭头规则全部可用（`layout_regions` 与 `reading_order_relations` 是 core 能力）。
- 视觉模型即便存在，也只能补充低信任候选对象或布局描述，不能覆盖确定性几何，也不能直接建立 Project Fact。
- `layout` 与 `structure_relations` stage 的载荷里记 `visual_model_required = false`、`visual_pack_status` 与 `visual_candidates = not_produced`，让这件事在运行清单里可查。

## 运行与降级

`layout`（`layout-regions-v1`）与 `structure_relations`（`flow-arrow-v2`）是流水线第 3、4 个 stage，**实际计算发生在 `retrieval_projection` 内部**：只有那里同时持有本次解析的 OCR 区域，在 stage 里重算就只能读到上一版索引的区域。

因此这两个 stage 记录的是**决策**：跑的是哪套规则集、读的是哪个引擎产出的区域、需不需要视觉模型。没有选中任何 OCR 引擎时它们报 `unavailable` + `reason_code = no_ocr_engine`，并说明这一层不承诺任何结果；核心处理继续。

## V1 兼容边界

`images.ocr_status` / `ocr_text` / `ocr_error` 与 `index_status` 的 `documents_indexed`、`images_indexed`、`ocr_succeeded`、`ocr_failed`、`ocr_unavailable` 保持 V1 含义与取值；布局只做**追加**，并且只在 V1 路径产出了区域之后才有内容。没有 OCR 引擎的机器上重建索引，13 张样例图仍然是 `ocr_unavailable = 13`，只是现在每张图会多一行空的 `layout_runs`。

相关文档：[`ocr.md`](ocr.md)、[`data-model.md`](data-model.md)、[`processing.md`](processing.md)。
