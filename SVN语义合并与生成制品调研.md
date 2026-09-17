# SVN 语义合并与生成制品调研

> 调研时间：2026-09-18
> 范围：多 revision SVN 任务的“最终状态语义整合”、`svn:mergeinfo` / `--record-only` 记账、整文件复制后的候选差异审核、二进制与生成制品重建、Windows BAT 执行边界。
> 资料原则：仅使用 Apache Subversion 官方文档、API、发布说明与 SVN Book，以及 Microsoft 官方进程/批处理文档和可复现构建规范；未在 Windows 或真实 SVN 仓库执行验证。

## 结论

- 现有 `svn-semantic-merge` Skill 的方向成立：把“内容如何落到目标”与“SVN 如何记录来源 revision”分开，能避开多轮提交逐次重放造成的冲突累积。
- **“工作副本 clean”只证明没有本地未提交变化，不证明目标分支自共同基线以来没有独立提交。** `svn status` 默认甚至不访问仓库；`svn status -u` 只补充服务器 out-of-date 信息。是否可整文件替换，必须另外比较 `Baseline -> Target current` 的仓库历史与差异。[SVN `status` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.status.html) · [工作副本四状态](https://svnbook.red-bean.com/en/1.8/svn.basic.in-action.html)
- `--record-only` 不是注释，而是会让后续 merge 把对应 revision 当作已合并；官方明确警告，用它阻断未实际采用的变更，本质上是在“告诉系统该变更已经合并”，且 SVN 无法列出“被阻断而非真正合入”的 revisions。[SVN Book：Blocking Changes](https://svnbook.red-bean.com/en/1.7/svn.branchmerge.advanced.html)
- 因此 `partial`、`unknown` 不能在分支根记录；`rejected` 也不应默认允许 record-only。只有团队明确采用“永久阻断 + 可审计账本”政策时，才可将 `rejected` 视为已结算。
- 整文件复制后的正确验收对象不是“文件看起来相同”，而是三份证据：`Baseline -> Source final`（来源意图）、`Baseline -> Target current`（目标独立变化）、`Target current -> Candidate`（本次实际待提交变化）。候选结果要逐路径、逐意图证明来源变化已落实且目标独立变化仍保留；一般不存在能自动证明语义等价的单条 SVN 命令。
- add/delete/copy/move/property/binary 不能降格成普通文件内容复制。树变化需要 SVN 调度操作，copy/move 还涉及历史；properties 是独立版本化数据；二进制默认不会被 SVN 做上下文合并，也不会在普通 `svn diff` 中展示行级内容。[SVN 基本工作周期](https://svnbook.red-bean.com/en/1.8/svn.tour.cycle.html) · [二进制处理](https://subversion.apache.org/faq#binary-files)
- 生成制品应按“权威输入 -> 固定生成器/版本/环境 -> 预期输出集”重建。BAT 返回 0 只是必要条件，不足以证明生成正确；还要核对输出 allowlist、前后状态、哈希、输入未被反向修改，以及可用时的第二次无变化验证。
- 在 Windows 上运行 BAT 必须显式调用系统 `cmd.exe /c`、固定工作目录、避免从不受信目录搜索 `cmd.exe`，并把仓库中的 BAT 视为会执行任意代码的程序。Microsoft 的 CreateProcess 文档与 MSRC 均提醒了 `.bat/.cmd` 进程启动和当前目录劫持风险。[CreateProcess 文档](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessa) · [MS14-019 分析](https://www.microsoft.com/en-us/msrc/blog/2014/04/ms14-019-fixing-a-binary-hijacking-via-cmd-or-bat-file)
- 当前生产实现应以 Apache 推荐的 1.14.5 LTS 行为为基准，并记录实际 `svn.exe` 版本；1.15 仍是候选版，不应作为规范默认行为。[Apache 下载页](https://subversion.apache.org/download.html)

## 一、建议把流程正式拆成四个账本

现有 Skill 已有 integration envelope、file-decision table、artifact provenance 和 revision ledger，但还可以把它们明确成四份互相校验的账本：

1. **状态账本**：source/target URL、repository UUID、peg revision、BASE revision 范围、working-copy root、depth、switched paths、externals、客户端版本、状态摘要。
2. **路径账本**：每个 changed path 的 source action、target history、候选 action、内容决策、property 决策、验证证据。
3. **制品账本**：权威输入、生成器、版本、环境、命令、工作目录、预期输出、实际输出、哈希、可复现性等级。
4. **revision 账本**：revision 在指定 mergeinfo 路径范围内是否完整结算，以及 `integrated`、`superseded`、`rejected`、`partial`、`unknown` 的理由。

四个账本不能互相替代。例如，文件内容已经正确不代表 copy history 或 properties 正确；测试通过也不代表 mergeinfo 可诚实记录；生成器返回 0 不代表只生成了计划中的输出。

## 二、`update`、`status`、`diff` 与“最新”的可靠边界

### `svn update`

不指定 revision 的 `svn update` 会把所选工作副本范围更新到仓库 `HEAD`，但 working copy 可以天然处于 mixed-revision；一次提交也只提升实际提交项的工作 revision。只有从工作副本根部进行完整更新，且没有 sparse、switched、external 等边界时，才通常得到单一 revision 的完整树。[SVN `update` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.update.html) · [mixed-revision 说明](https://svnbook.red-bean.com/en/1.8/svn.basic.in-action.html)

Skill 应把“更新两边到最新”改写为可验证条件：

- 从声明的 source/target 工作副本根执行完整深度更新；
- 更新结果无 text/tree/property conflict；
- 用结构化信息确认 URL、UUID、revision 范围、depth 与 switched 状态；
- externals 作为独立 working copy 分开检查，不能由父工作副本的“最新”结论覆盖；
- 随后把 source 固定为 URL + peg revision + end revision，不再以浮动 `HEAD` 表示此次输入。

### `svn status`

无参数的 `svn status` 只看本地，**不访问仓库**；`-u` 才会标出服务端存在更新的路径，`-v` 才给出更完整的 revision 信息，`--xml` 适合程序解析。[SVN `status` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.status.html)

还应补两项：

- 源端采集必须检查 unversioned 与 ignored 文件。`svn diff` 不会替你把未版本化输入纳入 patch；`svn status --no-ignore` 才会把 ignored 项显示为 `I`。[Ignoring Unversioned Items](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.special.ignore.html)
- `X`、`S`、`!`、`~`、scheduled add/delete、property `M/C` 都不是普通 clean 状态；尤其 `!` 可能是未通过 `svn delete` 删除或不完整工作副本。[SVN `status` 状态码](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.status.html)

### `svn diff`

工作副本路径上的无 revision `svn diff` 比较 `BASE` 与 working；URL 上的 revision 范围由服务器比较。`--summarize --xml` 能可靠提供 changed-path manifest，但不提供内容；完整 patch 与结构化 manifest 必须分别保存。[SVN `diff` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.diff.html)

重要边界：

- 默认 `svn diff` 忽略 ancestry；需要识别“同名但不同历史”的替换时，应同时保留 `svn log -v` 的 copy-from 信息，并在适用处使用 `--notice-ancestry`。[SVN `diff` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.diff.html) · [SVN `log -v`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.log.html)
- properties 是独立版本化数据，不能只审文本 patch；`svn diff` 可显示 property changes，而 `svn:mergeinfo` 应由 `svn merge` 管理，不能通过普通 patch 或手工属性编辑代替。[Properties](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.html)
- 二进制文件默认不显示行级 diff，也不会在 update/merge 时自动上下文合并；路径清单与哈希只能证明“字节发生变化”，不能说明业务意图。[Apache SVN FAQ：Binary files](https://subversion.apache.org/faq#binary-files)

## 三、working-copy clean 与目标无独立变化是两件事

建议把整文件复制门槛写成：

```text
可直替 = 目标工作副本在路径范围内无本地变化
      AND Baseline -> Target current 在该节点上无独立内容、树或属性变化
      AND 节点 ancestry 与类型兼容
```

“无本地变化”由 target 的 `status` 证明；“无独立提交变化”要由固定 Baseline 与 Target current 之间的仓库 `diff/log` 证明。`svn status` 无法回答第二个问题，因为一个完全 clean 的文件仍可能包含目标分支早已提交的独立实现。[工作副本四状态](https://svnbook.red-bean.com/en/1.8/svn.basic.in-action.html)

如果共同基线无法可信确定，Skill 不应推断“无独立变化”。可继续做人工语义整合，但必须将 whole-file replacement 标为高风险，并保留 Target current 的原始坐标或快照用于恢复。

## 四、复制后 candidate diff 的验收方法

建议使用三角证据，而不是只看复制后的 target diff：

| 证据 | 回答的问题 |
|---|---|
| `Baseline -> Source final` | 来源任务最终改变了什么？ |
| `Baseline -> Target current` | 目标分支独立改变了什么？ |
| `Target current -> Candidate` | 本次最终准备提交什么？ |

逐路径至少做以下检查：

1. source manifest 中每个路径都有明确候选结果；没有“看过但未决定”的路径。
2. target history 中每个独立变化都有保存、被等价实现取代、明确拒绝或待人工决定的结论。
3. candidate 中不存在 manifest 外的未解释路径；生成器输出除外，但必须出现在制品账本中。
4. 对被整文件覆盖的文件，从 Target current 反查删除的 hunk；每个删除都必须关联 source 意图或“恢复 target-only”决策。
5. 同时审查 property diff、tree schedule 与内容 diff；不能因文本正确就忽略 `svn:eol-style`、`svn:keywords`、`svn:executable`、`svn:mime-type` 等属性。
6. 运行目标分支的测试和生成验证；测试是语义证据，不是 mergeinfo 完整性的替代品。

SVN 官方建议 merge 后仔细查看 `svn diff`、构建并测试；同时官方也承认最终只有人能理解冲突意图。[Basic Merging](https://svnbook.red-bean.com/en/1.8/svn.branchmerge.basicmerging.html) · [copy-modify-merge 模型](https://svnbook.red-bean.com/en/1.8/svn.basic.version-control-basics.html)

无法机械证明的地方应明确报告：两个不同实现可能语义等价，也可能只是在现有测试下表现相同。Skill 不应把“diff 为空”“测试通过”或“AI 判断一致”单独当作完整证明。

## 五、mergeinfo、partial revision 与 `--record-only`

### mergeinfo 的实际语义

`svn:mergeinfo` 记录的是“某来源路径的哪些 revision 已合并到此路径范围”。自动 merge 会依据它跳过已记录 revision；`svn mergeinfo --show-revs eligible` 只列出会产生操作且尚未合并的 revisions。[Mergeinfo and Previews](https://svnbook.red-bean.com/en/1.8/svn.branchmerge.basicmerging.html) · [SVN `merge` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.merge.html)

这带来三个规范要求：

- revision 判断必须带**来源路径**和**目标路径范围**，不能只写一个裸 revision 号。
- `--show-revs merged/eligible` 只列 operative revisions，不是某个数字区间的完整审计清单；revision ledger 仍应按任务指定范围独立核对。[Operative and Inoperative Merge Revisions](https://svnbook.red-bean.com/en/1.8/svn.branchmerge.basicmerging.html)
- 在执行 record-only 前后，都要递归检查 explicit/inherited/non-inheritable/subtree mergeinfo，而不只是看分支根属性。

### partial revision

SVN 没有“同一路径同一 revision 的部分 hunk 已合并”这一 mergeinfo 表达。若一个 revision 在同一记录范围内仍有 deferred/unknown hunk，不能把该 revision 在该范围 record-only。

如果一个 revision 修改了互不相交的子树，SVN 可以通过 subtree mergeinfo 表达某个子树已经处理；但完整 merge 记录可能不只存在于分支根，继承与 elision 还会改变显式属性的位置。[Subtree Merges and Subtree Mergeinfo](https://svnbook.red-bean.com/en/1.8/svn.branchmerge.basicmerging.html)

因此建议：

- 默认只在分支根记账；官方最佳实践也建议避免 subtree mergeinfo。[Advanced Merging best practices](https://svnbook.red-bean.com/en/1.7/svn.branchmerge.advanced.html)
- 只有仓库本来就接受 subtree mergeinfo、路径映射稳定、该子树内完整结算时，才允许子树 record-only。
- 不要为了表达 hunk 级 partial 临时制造 file-level mergeinfo。
- 记录前先展示预期 mergeinfo delta；记录后递归审查实际 property delta，因为 mergeinfo inheritance/elision 可能让变化不只出现在预想的一处。

### `rejected` 的政策应收紧

现有 `revision-accounting.md` 把 `rejected` 列为“可 record-only，附审计说明”。从官方风险说明看，更稳妥的默认规则应是：

```text
rejected 默认不 record-only；
只有用户明确确认“永久阻断该 revision”，且仓库有 durable ledger 时才允许。
```

原因是 SVN 无法区分“已经具有等价实现”和“没有采用但不想再看到”；官方明确指出也没有命令可列出 blocked changelists。[Blocking Changes](https://svnbook.red-bean.com/en/1.7/svn.branchmerge.advanced.html)

### 在已有语义修改上执行 record-only

`record_only` 的 API 语义是只修改已有路径上的 mergeinfo property，不应用内容差异。[Subversion Merge API](https://subversion.apache.org/docs/api/latest/group__Merge.html)

但官方一般建议 merge target 起始时 clean。语义整合场景无法在 record-only 时保持“完全 clean”，因为候选内容已经存在。因此 Skill 应把它定义成受控例外：

1. 在 record-only 前冻结完整 candidate status/diff/hash。
2. 单独批准精确 source、target 和 revision scope。
3. 执行后证明内容、树调度与非 mergeinfo properties 没有变化。
4. 只接受预期的 `svn:mergeinfo` delta，并重新跑 `merged/eligible` 查询。

如果实际 property delta 超出计划，停止并让用户审查，不能自动“整理” mergeinfo。

## 六、add、delete、copy、move 与 property 的处理

整文件复制只修改磁盘内容，不表达 SVN 树操作。

| 来源动作 | 候选目标的最低要求 |
|---|---|
| add | 判断目标同名节点是否不存在、未版本化或历史不同；需要显式 `svn add` 或具有历史的 `svn copy` |
| delete | 判断目标节点是否有独立修改/子项/属性；需要显式 `svn delete`，不能只从磁盘删除 |
| copy | 保留 copy-from path/revision，优先用 `svn copy` 表达历史 |
| move | SVN 的工作副本 move 等价于有历史的 copy + delete；不能仅由文件系统移动替代 |
| replace | 明确这是同名新节点，不得仅凭最终字节相同隐藏 ancestry 变化 |
| property change | 单独列出并审核；内容复制不会带过去 |

这些行为由 SVN 官方工作周期和 `svn copy` 参考明确区分。[Make Your Changes](https://svnbook.red-bean.com/en/1.8/svn.tour.cycle.html) · [`svn copy`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.copy.html)

还应增加一条：若 source revision 中同时有 tree change 和内容变化，不能只因为最终文件已出现在正确路径就把 revision 判为 integrated；copy history、删除旧路径和相关 properties 都要结算。

## 七、二进制与生成制品

### 不要按扩展名直接分类

SVN 对 binary/text 的行为受 `svn:mime-type` 影响，且自动检测只是启发式；UTF-16 文本也可能被当作二进制。因此“扩展名”“SVN 是否展示行级 diff”“业务上是否为生成制品”是三个不同维度。[File Content Type](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.file-portability.html)

建议对每个二进制路径同时记录：

- SVN MIME/property 分类；
- 业务来源：权威源文件、配置表生成、第三方工具生成、外部交付、未知；
- 是否可本地重建；
- 生成器是否纯本地、是否有网络/凭据/外部副作用；
- 是否要求 SVN lock 或团队串行编辑。

### 生成制品的可复现性等级

建议不要把所有“可生成”都称为 reproducible，而分成：

1. **deterministic**：相同权威输入、工具版本与声明环境重复运行，输出哈希一致。
2. **normalized**：输出含已知时间戳、路径或顺序噪声，但可由官方/项目规则归一化后比较。
3. **regenerable but nondeterministic**：可重新生成，但输出含无法消除的非确定因素；必须用领域工具或结构化检查验收。
4. **opaque**：无法证明输入、工具或结果关系；保持 unresolved。

若生成器支持 `SOURCE_DATE_EPOCH`，可用它替代当前时间戳；该规范要求值只依赖源代码且在子进程中保留。[SOURCE_DATE_EPOCH 规范](https://reproducible-builds.org/specs/source-date-epoch/)

“运行两次验证无变化”只能用于声明为无外部副作用的本地生成器；不能对会发布、上传、写数据库、消耗许可证或访问生产服务的 BAT 自动重复执行。

## 八、Windows BAT 生成器执行规范

现有规范“显式 `cmd.exe`、固定 working directory、捕获输出”是正确的，还应补充：

- 使用系统目录中 fully-qualified 的 `cmd.exe`，不要让当前目录或 PATH 决定命令解释器。MSRC 曾专门说明从攻击者控制的 CWD 搜索 `cmd.exe` 会形成劫持风险。[MS14-019](https://www.microsoft.com/en-us/msrc/blog/2014/04/ms14-019-fixing-a-binary-hijacking-via-cmd-or-bat-file)
- BAT 要通过 `cmd.exe /c` 运行；Windows CreateProcess 官方文档明确把 `.bat/.cmd` 交给命令解释器。[CreateProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessa)
- 不把用户路径、revision 文本或自由文本直接拼进一条 shell command。`cmd` 的 `& | < > ^ ( )` 等字符有控制语义，简单引号并不足以构成通用安全转义。[Microsoft `cmd` 文档](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd)
- 运行器应限制输入为计划中已验证的路径/枚举参数；复杂或不可信自由文本参数应停止，而不是尝试自造 quoting 算法。
- 捕获 stdout、stderr、退出码和超时；但退出码 0 只能表示 BAT 自己报告成功。BAT 内部应使用 `exit /b <code>` 传播失败，环境修改应尽量由 `setlocal/endlocal` 限定。[Microsoft `exit`](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/cc770929%28v%3Dws.11%29) · [Microsoft `setlocal`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/setlocal)
- 第一次运行未知 BAT 前，检查脚本及其调用链；发现网络、凭据、安装、注册表、服务、仓库提交或工作副本外写入时，需要新的授权边界。
- 运行前后比较 source inputs、expected outputs、所有 versioned paths 和 unversioned/ignored paths；计划外写入即失败。
- 工作目录是生成语义的一部分。尤其不要假设 UNC 路径、长路径、非 ASCII 路径与本地驱动器行为一致；这些必须进入 Windows disposable-repository 验证矩阵。

## 九、提交前远端变化与计划失效

Subversion 会拒绝提交 out-of-date 的修改项，但这只是最后一道保护；不应等到 commit 才发现候选内容基于旧目标。[工作副本四状态](https://svnbook.red-bean.com/en/1.8/svn.basic.in-action.html)

计划应在以下任一条件出现时失效：

- target 范围内出现新的远端修改或 mergeinfo 变化；
- source 使用了浮动 `HEAD`，且 HEAD 已改变；
- 本地 status、revision range、switched/depth/externals、properties 或计划外文件发生变化；
- generator、工具版本、环境或权威输入发生变化；
- 用户又做了手工编辑但未写入路径账本。

提交前建议先做只读 `status -u`/远端差异检查。如果发现相关远端变化，不要在候选工作副本上盲目 `svn update` 后继续提交；`update` 会把远端变化合入本地候选，产生新的候选状态。应将计划标为 stale，保存现有审计材料，再由用户决定重新更新并重做三角验证。

“仓库全局 HEAD 增加”不必然使计划失效；只有 source/target/mergeinfo/生成输入相关路径的变化才有意义。但实现初期可以采用更保守的全范围失效策略，避免漏判。

## 十、对现有 Skill 的差距清单

| 优先级 | 缺口 | 建议 |
|---|---|---|
| P0 | `rejected` 当前默认可 record-only | 改为默认不可；仅在用户确认永久阻断且存在 durable ledger 时允许 |
| P0 | 没有把 target clean 与 target history clean 明确分开 | 增加 `Baseline -> Target current` 仓库 diff/log 门禁 |
| P0 | source manifest 可能漏掉 unversioned/ignored 输入 | 要求 `status --no-ignore`，并把未版本化任务输入列为 unresolved 或显式纳管 |
| P0 | candidate 验收缺少三角证据 | 强制保存 source intent、target-only history、candidate delta 三份材料 |
| P0 | record-only 后只说 review property diff，未要求递归 mergeinfo 检查 | 增加 recursive explicit/inherited/subtree mergeinfo 前后对比及 merged/eligible 复查 |
| P1 | “最新”没有定义 depth/switched/external/mixed-revision 边界 | 将其写成可验证的状态账本，不接受一句“已 update” |
| P1 | tree operations 只被分类，未规定如何保持 ancestry | 为 add/delete/copy/move/replace 建独立决策矩阵 |
| P1 | BAT 安全缺少 fully-qualified system `cmd.exe` 和元字符边界 | 补 MSRC 启动安全、参数白名单、脚本调用链与外部副作用检查 |
| P1 | 生成器成功只关注 exit/output paths | 增加输入未修改、输出 allowlist、hash、工具环境、可复现性等级 |
| P1 | plan digest 未明确定义提交前远端相关变化 | 增加 pre-commit freshness gate 与 stale-plan 状态 |
| P2 | 未固定支持的 SVN 客户端基线 | Windows 实现以 1.14.5 LTS 为默认验证基线，同时记录实际版本 |
| P2 | “SVN 挑拣”容易让人误解为 hunk-level mergeinfo | 明确 hunk 筛选属于候选内容编辑；SVN mergeinfo 仍按来源路径/revision 范围记账 |

## 十一、建议优先级

### 第一批：先改规范，不写自动化

1. 收紧 `rejected` 与 `partial` 的 record-only 规则。
2. 增加 target-history-clean 门禁和三角差异模型。
3. 增加 source unversioned/ignored 扫描。
4. 明确 record-only 前后递归 mergeinfo 验证。
5. 为 add/delete/copy/move/property 建路径决策表。

### 第二批：Windows 原型时实现

1. 状态账本与 plan fingerprint。
2. XML manifest 解析、完整 patch 保存、路径/属性账本。
3. candidate diff 审核报告。
4. BAT runner 的安全进程边界、输出 allowlist 与 provenance report。
5. pre-commit freshness gate。

### 第三批：用 disposable repository 验证

优先验证：partial revision + subtree mergeinfo、record-only 在已有候选内容上的实际 delta、move/copy history、property-only revision、binary MIME、generator unexpected output、远端并发提交导致的 stale plan、switched/sparse/external working copy。

## 十二、不建议加入的规则

- 不要规定“目标 `svn status` clean 就可以整文件覆盖”。
- 不要把“双方都 update 到 HEAD”简化成两个 revision 数相等；SVN revision 是仓库级，工作副本还可能 mixed、sparse、switched 或包含 externals。
- 不要为 partial hunk 在文件上随意创建 mergeinfo；这会扩散 subtree/file-level 记账复杂度。
- 不要默认对 `rejected` 做 record-only；它会让未来自动 merge 跳过该 revision，却无法从 SVN 查询它是“已合入”还是“被阻断”。
- 不要手工编辑 `svn:mergeinfo`；官方建议使用 `svn merge --record-only`。[Advanced Merging best practices](https://svnbook.red-bean.com/en/1.7/svn.branchmerge.advanced.html)
- 不要使用 `--ignore-ancestry` 来绕过历史问题；该选项会禁用 merge tracking。[SVN `merge` 参考](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.merge.html)
- 不要默认使用 `--allow-mixed-revisions`；官方 API 和 Book 都把单一 revision 的完整工作副本作为推荐 merge target。[Subversion Merge API](https://subversion.apache.org/docs/api/latest/group__Merge.html)
- 不要把 BAT exit code 0、生成文件时间更新或文件哈希变化单独当作生成正确的证明。
- 不要把所有 binary 都复制，也不要把所有 binary 都重建；先确定 source of truth 与 provenance。
- 不要在候选内容已完成后发现远端变化时自动 update 并继续；update 会产生新的候选，必须重新验证。
- 不要让 Skill 自动 commit。内容、mergeinfo 和生成制品应在同一 ready-to-commit 报告中供用户最终审核。

## 无法确认与风险

- 本次没有在 Windows 或真实 SVN repository 中运行任何命令；所有命令行为结论来自官方资料，Windows 路径/编码/BAT 调用仍需 disposable repository 实测。
- SVN Book 的当前英文在线版主要描述 1.8；本调研用 Apache 1.14 API/release notes补充当前支持线，但具体 Windows 发行版可能带不同编译选项（例如 MIME 检测）。实现必须记录实际 client build。
- “语义等价”“目标端独立行为已保留”无法由 SVN 元数据完全证明，仍需代码理解、领域测试和人工复核。
- `superseded` 与 `rejected` 在 mergeinfo 中同样只表现为“已记录”；长期可审计性依赖提交信息或仓库认可的外部 ledger。
- BAT 可能调用其他 BAT、EXE、PowerShell、网络服务或修改工作副本外路径；仅检查顶层脚本文本不能证明其全部副作用。
- 哈希能证明字节是否一致，不能单独证明业务正确性或生成来源。

## 一手来源

- [Apache Subversion 1.14.5 LTS 下载页](https://subversion.apache.org/download.html)
- [Apache Subversion release notes 与支持状态](https://subversion.apache.org/docs/release-notes/)
- [Apache Subversion 1.14 release notes](https://subversion.apache.org/docs/release-notes/1.14)
- [Subversion Merge API](https://subversion.apache.org/docs/api/latest/group__Merge.html)
- [Apache SVN FAQ：binary files 与 working-copy troubleshooting](https://subversion.apache.org/faq)
- [SVN Book：工作副本与 mixed revisions](https://svnbook.red-bean.com/en/1.8/svn.basic.in-action.html)
- [SVN Book：`svn update`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.update.html)
- [SVN Book：`svn status`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.status.html)
- [SVN Book：`svn diff`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.diff.html)
- [SVN Book：`svn log`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.log.html)
- [SVN Book：`svn merge`](https://svnbook.red-bean.com/en/1.8/svn.ref.svn.c.merge.html)
- [SVN Book：Basic Merging、mergeinfo、subtree mergeinfo](https://svnbook.red-bean.com/en/1.8/svn.branchmerge.basicmerging.html)
- [SVN Book：Advanced Merging 与 Blocking Changes](https://svnbook.red-bean.com/en/1.7/svn.branchmerge.advanced.html)
- [Apache Subversion：Mergeinfo internals](https://subversion.apache.org/blog/2008-05-06-merge-info.html)
- [SVN Book：Properties](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.html)
- [SVN Book：File Content Type](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.file-portability.html)
- [SVN Book：tree changes](https://svnbook.red-bean.com/en/1.8/svn.tour.cycle.html)
- [SVN Book：Ignoring Unversioned Items](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.special.ignore.html)
- [Microsoft CreateProcess 文档](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessa)
- [Microsoft MSRC：MS14-019 与 BAT/CMD 进程启动](https://www.microsoft.com/en-us/msrc/blog/2014/04/ms14-019-fixing-a-binary-hijacking-via-cmd-or-bat-file)
- [Microsoft `cmd`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd)
- [Microsoft `setlocal`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/setlocal)
- [Microsoft `exit`](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/cc770929%28v%3Dws.11%29)
- [SOURCE_DATE_EPOCH specification](https://reproducible-builds.org/specs/source-date-epoch/)
