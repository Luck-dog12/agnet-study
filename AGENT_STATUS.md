# Coding Agent 构建状态与新会话交接

2026-11-27 沙箱状态接口验收与 CI/CD：固定生产 Canary 的三道真实云门禁全部通过，安全摘要为 `status=success`、`exit_code=0`、`completed_gates=3`，其中 OSS RAM prefix 最小权限、双 Sandbox 安全边界、默认 remote 新会话与显式 local 回滚均为 `passed`。随后在既有真实云 CodingAgent A→B 恢复及运行中取消用例中补充 `get_status()` 断言，两项均通过，耗时 **104.470 秒**：初始空闲、审批等待、远端 handle 绑定时运行中、完成轮与后台任务统计、跨实例恢复后的最近轮次、取消后的迟到结果仍保持 cancelled，以及关闭后 stopped 均一致。用例使用受控模型脚本和真实云 SDK/执行/OSS，保留宿主未被修改、精确清理和 provider `active=0` 的原验收条件；不能把这些结果描述为真实模型自主规划云任务，也未重新验证空闲回收和续期的所有真实时序。

新增 GitHub Actions 配置：CI 在 main、codex 分支、PR 或手动入口运行 Windows/Ubuntu × Python 3.11/3.13 四组合，均创建并使用项目 `.venv`，执行离线 unittest、编译和依赖检查；通过后提供同一提交的源码 ZIP 与 SHA-256 校验文件。真实模型和云门禁保持关闭，不读取云 Secrets。CD 在 `v*` 标签触发，复用同标签提交的完整 CI，校验标签与项目版本后发布 GitHub Releases 源码包；保留运行所需的 code.md、工具策略和 Skills，不冒充可独立运行的 wheel，也不配置未知服务器部署。Action 固定完整提交 SHA，发布写权限仅限发布任务，不覆盖已有 Release。操作说明见 `docs/CI_CD.md`。

提交前本地验证：使用与 CI 一致的离线环境开关，全量 **958 项：933 通过、25 跳过，耗时 30.433 秒**；`compileall -q app tool tests scripts`、`pip check`、`git diff --check` 通过。25 项跳过包含新增的 3 项显式真实模型状态门禁。提交候选文件检查未发现当前配置中的真实密钥，`.env` 和运行会话仍被 Git 忽略。云端 Actions 结果应以仓库实际运行记录为准。

2026-11-27 统一状态查询真实验证：新增默认关闭的 `tests/test_agent_status_live_e2e.py`，仅 `RUN_AGENT_STATUS_LIVE_E2E=1` 时调用配置的真实模型。使用 `deepseek-v4-flash` 完成 **3/3 项，通过，耗时 11.603 秒、5 次真实请求**：真实流式文本经历 `idle → running → idle`，最近轮次为 `completed`，关闭后为 `stopped`（1 次请求、1.673 秒）；收到真实文本后主动停止，最终保持 `stopped`、没有伪造完成轮或自动续请求（1 次请求、1.538 秒）；模型实际调用本地受限 `code_interpreter`，固定表达式 `sum(range(10))` 返回 `45`，后台任务统计为 1 个 completed、0 个 active，真实持久事件前缀投影依次为 `accepted → running → completed`（3 次请求、8.387 秒）。另外既有真实模型连通性测试 1/1 项通过，耗时 5.356 秒。

本次观测范围：真实轮询捕捉到 `running`，流式回调也验证运行状态；短计算未被现场轮询捕捉到 `waiting_async`，因此该阶段仍以既有离线测试为依据，不将事件回放冒充现场观测。测试使用内存会话、临时工具日志、真实 SDK 响应与真实本地工具；请求适配器限制为 60 秒超时、零重试、每次最多 512 输出 token，并在首个工具请求指定唯一允许的计算工具，派发前精确核对固定代码。返回结果和模型响应均未伪造，其他工具在执行前被阻断。没有创建云 Sandbox，也没有执行真实云审批或恢复测试。此轮没有修改生产逻辑；真实用例的默认关闭检查与编译检查均通过。

2026-11-26 统一状态查询：新增 `app/agent_status.py` 的不可变状态快照与纯事件投影，宿主通过 `CodingAgent.get_status()` 查询 `run_state`、`mode`、当前/最近持久轮次、后台任务元数据与统计、待核验数量和 `last_event_seq`，可用 `model_dump(mode="json")` 转为 JSON 数据。前台阶段包含 `idle`、`running`、`waiting_approval`、`waiting_plan`、`waiting_async`、`stopped`、`failed`；主动关闭与会话故障分别标识，普通单轮错误仍可返回空闲接收下一请求。终端 `/status` 已移除独立维护的运行阶段，改用统一接口；Agent 尚未初始化时使用同一事件投影，保留模型、队列与沙箱信息。

查询只读已有内存事件快照，不调用模型、不创建沙箱、不刷新事件、不获取后台任务执行锁，也不返回请求、结果或错误正文。最近轮次的 `completed` 只表示该轮回复结束，不代表目标已验收；前台空闲时后台任务仍可能运行，需同时查看 `task_counts.active`。Plan 成功前没有持久轮次，取消 Plan 不改写最近轮次；`/interrupt` 继续保持中断后重新评估的行为。恢复未知结果和取消后迟到结果沿用已有事件语义，尚未提交的进度可能稍有延迟。

2026-11-26 验证：新增 12 项状态测试覆盖运行/结束、普通错误与会话故障、审批/Plan/异步等待、中断续跑、停止、恢复、迟到结果、只读与不可变性、任务锁被占用时查询，以及 CLI 统一接线与延迟初始化。项目 `.venv` 中定向 **84 项通过**；统一 UTF-8 并关闭全部 `RUN_*` live 门禁后，全量 **955 项：933 通过、22 跳过，耗时 41.118 秒**。`compileall -q app tool tests`、`pip check` 和 `git diff --check` 均通过；独立 API 检查也确认初始空闲、完成后空闲、主动停止及查询不触发执行。本轮没有调用真实模型或创建真实云资源。使用示例已写入 README。

2026-11-08 云沙箱生命周期与成本时长控制：在既有 execution/persistence 双实例隔离上，新增默认 **120 秒空闲回收、300 秒云端租期、3600 秒同会话累计驻留上限**。首次远端工具仍按需创建；普通空闲回收同时销毁两实例，下次新的工具操作重新创建并从 OSS 恢复已提交工作区，不重放旧用户命令。审批 reservation、待派发 capability 和异步 handle 等待期间均保留活动占用；续期受累计额度约束，初始化阶段不续期且必须在首个租期内完成。达到硬上限、租期过期、续期失败或无法确认安全清理时，阻断远端执行并拒绝迟到结果，不自动退回本机执行。

预算记录：新增 `app/sandbox_budget.py`，CLI 将 `sandbox-budget.json` 绑定持久会话目录；创建/续期前保存最坏租期预留，含有界网络请求余量。确认清理后按驻留时间结算；创建响应丢失或销毁未知时保守计入完整预留，重启/恢复不清零。独立 OS 文件锁、原子写入、会话身份和 owner 校验防止旧进程结算覆盖后继账本；损坏、路径不安全或写入失败均禁止新增云资源和续期。会话存储精确允许预算账本、锁和规定格式的遗留临时文件，并保留普通文件安全校验与未知文件拒绝。这里统计的是一组双实例的驻留时间，不是两个实例时长之和，也不是金额或账号级总预算；OSS 费用独立存在。

资源隔离与终端：每次创建的实例组拥有独立 generation metadata，故障/退出的独立清理只针对该组，旧进程迟到清理不能误删后继组。CLI 在持有会话 writer lock 时先完成全会话残留清理，交给 Agent 后的 lazy 创建与空闲重建不再进行全会话扫描。SDK 创建显式指定到期销毁、关闭自动恢复及请求超时。`/status` 显示沙箱状态、已用/剩余时间和限制；未初始化时不会为了状态查询创建资源或接管账本。生命周期通知与流式终端输出同步，退出后停止通知；没有新增手动解锁命令。配置及限制见 `docs/SANDBOX_LIFECYCLE.md` 和 `.env.example`。

2026-11-08 验证：新增预算、生命周期、CLI 和会话恢复离线测试覆盖空闲销毁/OSS 恢复、审批及异步任务占用、有限续期与网络余量、超限迟到结果拒绝、未知创建保守预留、跨进程账本接管及跨实例组清理隔离。项目 `.venv` 中统一 UTF-8 并显式关闭全部 live 门禁，全量 **943 项：921 通过、22 跳过，耗时 47.921 秒**；`compileall -q app tool tests`、`pip check` 均通过。本轮未调用真实模型或创建真实云资源；真实 provider 续期/回收及账单效果尚未进行本轮 live 验证。

2026-11-04 待执行命令可见性修复：普通 act 执行期间收到的新需求仍按 FIFO 排队，现在立即显示“已排队 #编号：需求预览”和完整待执行列表；轮到该需求时显示“开始执行 #编号”并更新剩余列表。编号在当前进程内递增，排队提示只属于终端呈现，未轮到的需求不提前写入 `user/message` 或提交模型，实际执行保留完整原文。运行中 `/status` 立即返回运行状态、等待数量和列表；审批回复与 Plan 选择不进入普通命令队列。模型服务失败时列出已取消的排队命令，故障期间的新输入继续直接丢弃；工具轮次上限不清空队列。退出或会话停止也为剩余队列显示取消结果。

终端显示同步：队列通知与模型输出按块串行，通知前结束当前输出行，后续模型文本重新显示回复前缀；默认输出动态使用终端的 stdout/stderr 代理，避免绕过 prompt_toolkit 重绘。命令预览转义换行、控制字符和 Unicode 行分隔符，并限制显示长度，完整需求内容不受影响。新增 `tests/test_command_queue.py` 的 8 项测试覆盖连续入队可见性、FIFO 精确执行、实时状态、故障取消不重放、工具上限后执行排队需求、审批状态查询不冒充审批、退出取消、文本显示边界与终端输出代理。

2026-11-04 验证：上述 8 项离线场景通过；旧工具轮次上限测试改为等待“重新尝试”的 Future 完成再发送退出，避免把主动退出导致的队列取消误判为重试失败。项目 `.venv` 中统一 UTF-8、显式关闭全部 live 门禁后，全量 **902 项：880 通过、22 跳过，耗时 32.711 秒**；`compileall -q app tool tests`、`pip check`、`git diff --check` 均通过。本轮没有调用真实模型或云 Sandbox。

2026-11-02 模型故障处理修正：按用户要求撤销此前的手动 `/model reset` 和持久暂停锁，改为交互启动时先检查模型服务。独立后台检查对当前配置的模型发送一个无历史、无工具的小请求，SDK 超时设为 10 秒、禁用 SDK 重试、关闭 thinking、最多 16 个输出 token，不初始化 Agent 或 Sandbox。检查中或不可用时不接受用户需求；检查失败提示“模型服务失败”，每隔 15 秒自动重查，健康时不持续轮询。`--help` 和 `--list-sessions` 不启动检查，退出不等待检查网络请求。

输入与恢复：故障期间提交的普通输入原文在进入输入队列前即被替换为丢弃标记，不追加 `user/message`，也不交给模型；已经排队但未执行的需求在发生模型故障时清空。自动检查成功后提示服务可用，只接收新提交的需求，不重放旧任务；用户无需输入恢复命令。输入边界以完整行提交时的状态为准，避免吞掉等待恢复后提交的第一条新命令。`/status` 显示 `model_state=checking|unavailable|ready`。已有运行实例中仍可用 `/tasks`、`/task ID`、`/cancel ID` 和 `/recovery` 管理已派发任务，退出始终可用。

错误分类与历史：仅模型请求失败或模型响应协议错误进入服务不可用状态；截图中的“工具调用超过最大轮次”属于任务上限，保留任务上下文，下一条“重新尝试”可正常执行。模型失败轮的原始需求保留在不可变审计记录中，但从后续 SDK 消息中隔离；失败的中断重评估链同样撤销原始未完成需求。已发生的 assistant/tool 配对、工具结果及未知修改核验规则保留，旧 assistant 文本与推理不再作为继续执行依据；包含撤销轮的自由摘要回退到事实投影。失败 Plan 不写用户需求或暂停事件。旧版本留下的 `model_guard` 控制事件仅作为审计记录，不会继续锁住会话或传给模型。后台任务继续记录真实终态，已执行操作不自动回滚。普通模型请求原有重试配置、权限与执行后端保持不变。

2026-11-02 验证：在项目 `.venv` 中使用 Fake client、受控健康检查和受控 runner，覆盖启动失败、故障期输入丢弃、自动恢复后的第一条新命令、旧排队命令清除、迟到检查结果、检查中退出、工具轮次上限正常重试、失败历史隔离及后台任务真实终态。既有 CLI 测试显式替换健康检查，模型探测另有独立测试；本轮不调用真实 DeepSeek 或云 Sandbox。统一 UTF-8 并显式关闭全部 live 门禁后，定向 **84 项通过**，全量发现 **894 项：872 通过、22 跳过，耗时 36.007 秒**；`compileall -q app tool tests`、`pip check` 和 `git diff --check` 均通过。

> 最后核对日期：2026-11-27<br>
> 项目根目录：`F:\新项目\agnet-study`  
> 当前阶段：在既有单 Agent、会话、权限、审批、异步任务和 Sandbox/OSS 基线上，已完成 **5C1 + 5C2 + 5C3 + 5C4 + 5C5** 的产品接线，并于 2026-10-14 第十次 5C6 真实云运行打通 partial-control 核心 Coding Agent 闭环，第十一次又在同一 A→B 流程中通过 `full-access` 无回调写入/编辑与持久化恢复；随后独立 5C4 live 用例先后通过真实运行中取消、真实结构化超时、“已派发且真实 handle 已绑定后，等待结果不可确认”、“provider 已返回 raw handle、但绑定前包装失败”，以及“provider kill 已成功但成功确认丢失”的安全受控注入。2026-10-15 又完成安全 GC 第一阶段：只有 manifest 写入明确失败、探测明确不存在且 active 已精确回滚时，才会删除本事务确认新建并逐字节复核的 content object；该路径已由真实 CodingAgent 和真实 OSS 删除通过。同日完成两步迁移开关收敛：恢复会话省略 `--execution-backend` 时采用经严格验证的持久绑定；尚未建立身份的新会话可由 `AGENT_DEFAULT_EXECUTION_BACKEND` 选择默认 local/remote，显式 CLI 仍优先且可回滚。已有会话永远不受新默认影响，竞态出现相反绑定会 fail closed，远端配置失败也绝不降级到 local。安装缺省仍为 local，因此本地受限执行尚未下线。此前七个 execution Sandbox、三次 orphan-cleanup Sandbox、maintenance A/B 两个 Sandbox、跨进程 stale 接管 Sandbox、2026-10-19 资源压力 Sandbox、2026-10-20 审批并发 Sandbox、4 个默认 remote canary Sandbox 与 3 个 kill 异常 harness Sandbox 均在结束后由 provider active-list 外部确认对应 session 的 `RUNNING/PAUSED` 数量为 0。固定可信 Linux venv、Agent 远端异步 unittest、`write_file` / `editor_file` 精确事务、统一 remote workspace transaction gate，以及 OSS v2/v3 immutable generation 的恢复链均已完成产品接线。local 路径保持原行为，remote 不会 fallback 到宿主，也不会写宿主 `data/pending`；Agent-facing 删除、清空和自由 shell 仍保持关闭。**截至 2026-10-20，`format_json` v3 提交/恢复/随机 staging 清理、direct-kill=False 与受控真实 SDK/transport kill 异常后的真实 provider 独立 cleaner、sealed-secret provider 前硬门、崩溃 orphan 的真实 OSS 引用扫描精确回收、控制面退出后的跨 OS 进程 stale Sandbox 接管、内存/CPU/墙钟/输出上限压力、审批并发串行化与精确绑定，以及默认 remote 新会话金丝雀、显式 local 回滚与固定三会话 fail-fast 批次均已通过显式授权 live 验收。两个观测时点累计 4/4 canary 成功且 4/4 active=0；这仍不代表长期配额、持续流量或未受控 provider 服务端故障已稳定。**

2026-10-16 补充：已完成独立于 permission mode 的 sealed-secret/account 安全域。`.env*`、密钥、证书、credentials、token 等路径在 local/remote 和 `full-access` 下均不能被读写、编辑、删除、清空、扫描、上传或持久化；远端输入、provider 调用前和返回结果均有硬门且绝不 fallback。未来账号动作即使在 `full-access` 下也必须取得独立可信批准。该安全域当时先通过离线回归，remote `.env` provider 前拒绝后于 2026-10-18 maintenance live 增量通过。该日记录的跨进程清理和多文件命令事务后续已于 2026-10-17 完成离线接线；当前优先级以本文件最新段落为准。

2026-10-17 补充：已完成跨进程 remote 资源所有权恢复与独立清理的离线产品接线。每次 Aliyun backend 创建新 Sandbox 前，先按固定 owner + 严格 32hex session metadata 有界查询、逐项复核并清理 `running` / `paused` 残留，最终必须再次证明 active=0；直接 `kill()`、candidate 初始化清理返回非严格 `True` 或抛出普通异常时也走同一独立通道。恢复持久 remote 会话时，若原始事件历史存在 `started` 且无 durable terminal 的后台任务，CLI 会在持有 writer lock 时先完成资源清理，成功后才把任务保守记录为 `outcome_unknown`；失败不写恢复终态，下一次 resume 可重试。旧命令级 provider handle 和结果不会被伪造或自动重放。该路径先通过离线回归；其中 direct `kill()` 返回严格 `False` 后的独立 cleaner 已于 2026-10-18 maintenance live 受控通过，控制面退出后的跨进程残留也于同日由独立进程 B 接管并确认 active=0。

2026-10-17 再补充：已完成首个受限多文件命令事务。remote 同步 `bash_shell(format_json)` 最多接受 8 个已存在 JSON 文件；Registry 在 provider 调用前完成白名单、规范 POSIX 路径、重复路径、后缀、sealed/hard-excluded 和 permission policy 校验。可信 Sandbox runner 只把规范化候选写入随机 `.agent-internal/transactions/<token>`，active 全树在候选前后保持与当前 generation 完全一致；workspace gate 从快照穿过唯一一次真实 diff 审批直到 commit/discard。批准后所有文件以一个 schema v3 full-state manifest 发布，v2 清单仍可解析和恢复；确定失败会逆序回滚全部已写 active 文件并只回收本事务明确新建的 object，不确定结果会 fence/关闭 runtime。local `format_json`、其他同步远端 Bash、删除、清空、包管理和自由 shell 均继续 fail closed。该路径先通过严格离线 adapter/failure-injection 回归，后于 2026-10-18 maintenance live 通过双文件 v3 提交、staging 清理和跨 Sandbox 恢复。

2026-10-18 补充：已完成宿主维护面的 OSS orphan 引用扫描与保留协议。普通恢复继续忽略未发布 object，避免损坏 orphan 阻断已有会话；只有 CLI 持有 `SessionEventWorkflow` 单写者租约、先完成纯本地 seed/checkpoint 预检并通过 session-scoped provider active=0 清理后，才为新建的唯一 Sandbox 启用 GC。协调器以当前 v1 baseline digest 为锚，严格解析连续、唯一、无分叉的全部 v2/v3 full-state generation，并保留所有历史 manifest 的 `files` 引用并集；两次完整扫描的 inventory、链、引用集和 orphan 集必须完全一致，才会通过独立的宿主内部 exact-delete 原语删除未引用的 `objects/<sha256>`，之后再做第三次全量复核。截断、未知路径、gap/split-brain、baseline/transition/tamper、扫描漂移、删除失败或删除后复核异常均 fail closed。generation manifest、任一历史代仍引用的 object、当前及旧 v1 immutable checkpoint root 均不删除；该能力没有注册给 Agent。离线接线与当日受控真实 E2B/OSS 增量验收均已通过。

2026-10-18 当日 live 补充：新增独立 `RUN_ALIYUN_SANDBOX_LIVE_MAINTENANCE_E2E` 进程门，默认 skip/零云调用，显式授权后一次通过，耗时 **57.856 秒**。同一 session 的 Sandbox A 通过真实 CodingAgent 对两个已存在 JSON 执行一次 diff 审批的 `format_json`，发布唯一 v3 generation，并确认随机 `transactions/<token>` staging 子目录已精确清理；`.env` 读取在 transport/provider 前固定拒绝。Harness 只对该具体 Sandbox 注入 direct `kill()` 严格返回 `False`，随后真实独立 provider cleaner 按 owner/session 重查、重连、销毁并外部证明 active=0。在已证明 active=0 后，Sandbox B 以 `collect_orphans=True` 恢复 v3，对一个真实 OSS 未引用 content object 完成双扫描、exact delete 和第三次复核，同时逐字节保留 manifest 及 3 个已引用 object，Agent 精确读回两个格式化文件。B 关闭后同样外部证明 active=0；本次不调用 DeepSeek，不保存凭据、Sandbox ID 或 OSS 原始路径。

2026-10-18 跨进程 stale live 补充：新增独立 `RUN_ALIYUN_SANDBOX_LIVE_CROSS_PROCESS_STALE_E2E` 门，默认 skip，并在 gate、基础 probe 与 recovery probe 三者均显式开启前保持零云调用。进程 A 只以产品固定 owner + 当前随机 32hex session metadata 创建 Sandbox、验证返回 ID 的安全形状，然后不调用 `kill()` 退出；父进程在 A 已退出后按同一 exact metadata 有界轮询并只保留 active count，必须观察到恰好 1 才会启动独立 Python 进程 B。B 调用产品 `cleanup_session_sandboxes` 重新查询、重连和销毁，返回后父进程再次外部确认 active=0，最后仍执行同 session 的幂等 emergency cleanup。首次 live 于约 3.4 秒内安全失败：旧 harness 在 A 内创建后立即查询 metadata 索引，因 provider 索引存在短暂不可见窗口而只返回固定失败边界；父进程 emergency cleanup 未报告失败。修正为 A 退出后的最长 45 秒、1 秒间隔 `0 → 1` 有界收敛后，第二次 live **通过，耗时 6.102 秒**，真实证明 A 退出后 active=1、不同 OS 进程 B 接管、B 后 active=0。全流程不调用 DeepSeek，worker 只输出固定 token，父进程不记录凭据、Sandbox ID、OSS 原始路径或 provider 原文；PID 不同仅作为附加证据，核心证据是两个外部 exact-session count。

2026-10-19 资源上限 live 补充：新增独立 `RUN_ALIYUN_SANDBOX_LIVE_RESOURCE_LIMITS_E2E` 门，默认 skip/零云调用，不创建模型客户端或调用 DeepSeek。单个真实 Sandbox 依次执行有限的 300 MiB 字符串分配、有限超大 `range` 循环、4 秒合成 unittest 配 1 秒墙钟上限，以及 20,000 个固定字符输出，分别验证 Code Interpreter 的 256 MiB `RLIMIT_AS` / 1 秒 `RLIMIT_CPU`，以及结构化命令的 wall timeout / 16,384 字符输出边界；每次受控失败后都在同一 Sandbox ID 上运行 `6 * 7` 或快速 unittest 证明 runtime 可复用。首次 live 在前三类压力及复用均通过后安全暴露真实产品缺口：runner 的 `truncated=true` 在 transport 解码时被丢弃。产品现以严格布尔校验将 runner 截断标志和宿主二次字节裁剪做 OR 合并，并同时修复 restricted-code 与 structured-command 两条协议；exact-boundary、宿主超限推断和恶意标志均有离线回归。修复后第二次 live **通过，耗时 27.194 秒**，关闭、session cleaner 与外部查询确认 active=0。公开 Code Interpreter AST 禁止 import/进程 API，因此未伪造进程压力；当时的 live 也未验证 2026-10-21 新增的结构化命令 AS/CPU/NPROC/FSIZE 固定 rlimit。两类 runner 的 `RLIMIT_NPROC` 建立均有精确离线单测，但不能解释成 live cgroup/VM 级进程隔离。

2026-10-20 审批并发 live 补充：新增独立 `RUN_ALIYUN_SANDBOX_LIVE_APPROVAL_CONCURRENCY_E2E` 门，默认 skip/零云调用，不创建模型客户端或调用 DeepSeek。实现与审查过程中发现 `_InputBroker` 在“检查未关闭”和“请求入队”之间释放锁，`close()` 恰好介入时会先排空空队列，随后 waiter 入队并永久等待；现将检查与入队合并为同一锁内原子步骤，审批与 Plan 选择两条路径均有确定性回归。单个真实 Sandbox 验证：同一 `FileApprovalWorkflow` 的第二个并发请求立即拒绝且不覆盖首请求；独立 workflow 的第二个写入和已完成可信授权的 `compileall` 都被 workspace gate 阻断，首写批准提交后第二写精确进入自己的 callback 并被拒绝，命令随后执行；审批异常、执行前 capability 取消和审批期间 runtime close 均不会泄漏 reservation 或复用 permit。显式 live **一次通过，耗时 21.159 秒**，finally 中关闭 runtime、调用 session cleaner 并由 provider 查询确认 active=0；固定诊断不记录凭据、Sandbox ID、OSS 原始路径或 provider 原文。

2026-10-20 默认 remote 小流量 canary 补充：新增独立 `RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_E2E` 门，默认 skip/零云调用，不创建真实模型客户端或调用 DeepSeek。单个合成新会话通过真实 `app.main`、实际 `AGENT_DEFAULT_EXECUTION_BACKEND=aliyun-sandbox` 解析、持久 `SessionEventWorkflow` 绑定、真实 `CodingAgent`/Registry/runtime 和真实 Sandbox，在默认 `partial-control` 下完成普通文件 overwrite 与 read；审批 callback 和宿主 read/write handler 均为一旦调用即失败。canary 只记录有界阶段和整数计数，确认本次 v2 content object 与 generation manifest 各新增 1，不保存路径、摘要、Sandbox ID 或 provider 原文。随后在 remote 默认仍生效时创建显式 `--execution-backend local` 新会话，证明默认 loader、Sandbox 配置、runtime/client 均不调用，local 绑定成功且原 remote `execution.json` 字节不变。显式 live **一次通过，耗时 15.144 秒**，finally 精确清理并确认 remote session active=0；环境变量只作用于测试进程，没有改写 `.env` 或安装默认值。该结果只是一条真实金丝雀，不代表持续流量或长期配额已经稳定。

2026-10-20 默认 remote 重复 canary 批次补充：同一模块新增独立 `RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_BATCH_E2E` 门和不可由环境或模型扩大数量的固定三次串行批次。任一 attempt 失败会立即停止后续放量；每次仍执行完整的默认 remote CLI、真实 Agent/Registry/runtime、显式 local 回滚、finally cleaner 与 provider active=0。不可变聚合报告只保存严格整数：attempt/success/failure、active_zero、object/generation 增量及每次/总/min/max 毫秒耗时；bool、负数、越界或不一致汇总均拒绝。首个真实批次 **3/3 通过，耗时 42.609 秒**，单次为 **14.308、13.889、14.412 秒**，active_zero=3，v2 object/generation 各累计 +3。连同此前单次 canary，两个观测时点累计 **4/4 成功、4/4 active=0、object/generation 各 +4**；未调用 DeepSeek，未改写 `.env`。这完成了首批有界重复观测，但不代表并行流量或长期配额趋势。

2026-10-20 受控真实 SDK/transport kill 异常补充：新增独立 `RUN_ALIYUN_SANDBOX_LIVE_NATURAL_KILL_EXCEPTION_E2E` 门，默认 skip/零云调用，不创建模型客户端或调用 DeepSeek。Harness 只包装当前具体 Sandbox 的一次原始 SDK `kill()`，以临时本机非响应端点、非秘密凭据、禁用重试和 0.25 秒正超时触发真实传输异常；产品随后必须用未改写的真实 settings 按 exact owner/session 重查、重连和销毁，并由独立查询证明 active=0。首轮 **2.337 秒**安全暴露“固定无效凭据未必触发异常”的 harness 假设；第二轮已取得真实 SDK timeout，但因新建资源的 provider metadata 索引短暂不可见，初次 cleaner 扫描为 0，随后 finally cleanup 与 active=0 仍成功。补上最长 30 秒、0.25 秒间隔的精确可见性屏障后，第三轮 **通过，耗时 3.856 秒**：direct_calls=1、sdk_exceptions=1、cleaner_kills=1、backend_closed=1、active_zero=1。三轮均无 active 残留，未改写 `.env` 或生产代码；该结果只证明受控真实 SDK/传输异常恢复边界，不声称未受控 provider 服务端故障已经自然发生。

2026-10-20 网络出口收紧补充：新增严格布尔配置 `ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS`，默认为 `false`；唯一 `Sandbox.create` 边界始终显式传入 `allow_internet_access`，覆盖 E2B SDK 的默认开启行为，非 `true` / `false` 环境值均 fail closed。缺省、显式开关、严格类型与 SDK 创建参数已通过离线回归；真实阿里云 Sandbox 在公网出口关闭时的 OSS 动态挂载兼容性尚待独立 live 验收。

2026-10-21 结构化命令 rlimit 补充：远端 `unittest` / `compileall` / `validate_json` 的可信 POSIX runner 现于 `preexec_fn` 中固定收紧 `RLIMIT_AS=256 MiB`、`RLIMIT_CPU=30/31 秒`、`RLIMIT_NPROC=4`、`RLIMIT_FSIZE=16 MiB`，由实际结构化命令子进程及其后代继承，同时保留既有 wall timeout、输出上限、`shell=False` 和进程组清理。四项边界任一缺失、继承值类型非法、`getrlimit` / `setrlimit` 失败或平台非 POSIX，均在执行项目命令前 fail closed；严格 payload 不接受资源字段，模型、工具参数和环境数字均不能扩大固定上限。精确离线测试还证明不会放宽继承的更严 soft/hard limit。本轮定向 **125 项 OK（1 项 live 默认 skip）**，`compileall` 与 `pip check` 通过；尚未运行真实云压测。该 rlimit 是单个子进程树的内核约束，不是 cgroup/VM 级聚合内存或进程配额，且 `RLIMIT_NPROC` 仍受 real UID 计数语义影响。

2026-10-21 OSS 挂载 runtime-user 隔离补充：默认 E2B transport 在任何 workspace initializer 或项目命令可运行前，先以已验证的系统 Python 和 root 对 `persistence_root` 执行 `root:root`、`0700` 固化与精确 `lstat`/mode/root 访问复核，再以固定非特权 `runtime_user` 真实探测目录列举、子路径读取和 create-only 写入均被内核以 `EACCES` / `EPERM` 拒绝；探测使用随机内部文件名，任何意外创建都由 root `finally` 精确删除并复核不存在。chmod/chown/stat、root/runtime 探测、cleanup 或后续 SDK root 列举任一失败，transport 构造均以固定净化错误 fail closed，路径、凭据和 provider 原文不进入模型。v1 checkpoint 与 v2/v3 object/generation 的 Files API 元数据、列举、建目录、读、写和精确删除均显式 `user="root"`，因此宿主控制面仍保留 checkpoint 能力；这不是只依赖词法路径限制，能覆盖 `unittest` 内任意 Python 直接猜测 OSS 路径的场景。主审定向 **217 项 OK（8 项 live 默认 skip）**，全量、`compileall` 与 `pip check` 通过；本轮未运行真实云。挂载 chmod/chown 与 E2B root Files API 的云侧兼容仍待显式 live 验收；RAM Role 是否严格最小权限只能由云侧 IAM/RAM Policy 与信任策略审计证明，仓库代码不能证明，当前不得宣称该项已完成。

2026-10-21 security-boundaries live harness 补充：新增独立、默认关闭的 `RUN_ALIYUN_SANDBOX_LIVE_SECURITY_BOUNDARIES_E2E` 门；只有该门、基础 probe、recovery probe 同时开启，且严格配置 `allow_internet_access is False` 时才会加载真实 runner/provider，普通回归保持零云且不构造模型客户端或调用 DeepSeek。单个真实默认 backend/transport 必须先通过 persistence root 的 root/runtime_user ready 前隔离，再由实际 `runtime_user` 的结构化 `unittest` 证明项目代码不能 list/read/create OSS root；网络探测不做 DNS，只对两个公网数字地址、阿里云元数据 `100.100.100.200:80` 及额外 link-local `169.254.169.254:80` 做短时 `connect_ex`，不读取、打印或持久化响应，任一连接成功即失败，并与产品 `Sandbox.create(..., allow_internet_access=False)` 的精确参数证据共同判断，不能把单一 DNS/服务故障冒充网络隔离。相同 Sandbox 还会验证结构化命令 AS=256 MiB、NPROC=4、FSIZE=16 MiB 的越界操作均失败且每项之后 quick unittest 可复用，以及 root 控制面的 v2 小写对象读回；任何路径 finally 关闭 backend、执行 exact-session cleaner 并查询 active=0。冻结报告只含固定阶段、3 次 persistence 拒绝、4 次网络拒绝、3 次资源拒绝、3 次复用、1 次 v2 读回及布尔终态，不含 Sandbox ID、OSS 原始路径或 provider 原文。新模块 **10 项 OK（1 项 live 默认 skip）**，修正阿里云元数据地址后的关联回归口径为 **181 项 OK（2 项 live 默认 skip）**；本轮没有运行真实云。剩余风险是阿里云模板实际网络策略、动态挂载 chmod/chown、runtime_user 内核拒绝、root Files API 与结构化命令 rlimit 的组合语义尚待该 gate 验收；为避免固定约 30 秒成本，本 harness 未加入 CPU live 压测，且这些 rlimit 仍不是 cgroup/VM 聚合配额。RAM Role 最小权限继续只能由云侧 IAM 审计证明。

2026-10-21 security-boundaries 首次真实云验收结果：在四个严格门控均显式开启、`allow_internet_access=false` 且不构造模型客户端的前提下，live gate 于 **4.104 秒**在 `stage=create` 安全失败。受控固定令牌诊断把原因收敛到 OSS 动态挂载的真实 POSIX 语义：挂载初始 owner 是 `root:root`，但 mode 不是 `0700`；root 对挂载根执行 `chown` 与 `chmod` 均失败；不修改挂载时，实际 `runtime_user` 对 list/read/create 的拒绝数为 **0/3**，并成功创建测试对象，随后由 root 精确删除。阿里云当前 Agent Sandbox 动态 OSS mount 的公开配置只提供 `bucketName`、`mountDir`、`bucketPath`、`endpoint`、`readOnly`，没有暴露 ossfs 的 `uid`、`gid`、`dir_mode`/`umask` 或 `allow_other`；因此不能通过删除产品校验、忽略 `chown`/`chmod` 错误或仅依赖词法路径规则来伪造 root-only 隔离。原 live gate 与后续 5 个实际云诊断 session 均完成 backend/direct kill、exact-session cleaner 和 provider active 查询，最终全部为 `active=0`；另一次本地常量导入错误在任何云调用前停止。**该结果把默认 remote 生产迁移明确置为阻断：安装缺省必须继续保持 local，不得扩大 remote canary、不得开放更多 Agent-facing 命令。解除条件只能是 provider 在创建时提供并实测可验证的 root-only mount 参数，或把持久化控制面移出运行不可信项目代码的 Sandbox（例如独立控制面存储通道/无 OSS mount 的执行 Sandbox）后重新完成整套 live gate；本轮未修改生产代码。**

2026-10-22 双 Sandbox 安全边界改造：默认 Aliyun backend 现为同 session、不同 `sandbox_id` 的两个物理资源。execution 角色 metadata 只含 owner/session/role，不含 OSS mount 或 RAM Role；它独占 active workspace、可信 runtime 与全部项目命令。persistence 角色才携带 OSS/auth metadata，并只通过宿主私有的窄 Files API transport 暴露 v1/v2/v3 读写、列举和精确 object 删除；它不创建 workspace、不发现系统 Python、不建立 venv/runtime user、不执行项目命令，也不再尝试云挂载不支持的 chmod/chown。组合 transport 显式把 active 操作路由到 execution，把 durable 操作路由到 persistence，现有 manifest、generation、rollback、fencing 和 GC 协议未改。backend 对两个资源执行原子创建、物理分离校验、失败全量回收、execution-first 关闭和 exact-session cleaner 兜底；同对象异常返回会去重清理，不同对象但重复 ID 会 fail closed。安装缺省仍为 local，没有扩大 Agent-facing 能力。

2026-10-22 离线与真实验收结果：全量发现为 **835 tests**，812 项通过、20 项跳过、3 项失败；3 个失败仍是既有 `skills/grilling` Skill catalog 消息序列旧断言，本轮没有新增失败。双 Sandbox backend 与 security-boundaries 定向 **45 项 OK（1 项 live 默认 skip）**，运行时/Registry/事务/transport 定向 **134 项 OK**，更新后的旧 live harness 离线协议 **76 项 OK（9 项 live 默认 skip）**；`compileall` 与 `pip check` 通过。随后显式运行 security-boundaries 真实门禁并于 **70.190 秒通过**：execution 对 OSS 路径 list/read/create 3/3 不可用且 `ismount=false`，4 个固定网络目标全部拒绝，AS/NPROC/FSIZE 3 项越界全部失败且 3 次 quick reuse 成功，persistence 控制面完成 1 次 v2 小写对象往返，backend close、cleaner 与 provider 查询共同确认同 session 最终 `active=0`。同日又按双资源可见性更新并复跑真实 SDK/transport kill-timeout 门禁，**6.190 秒通过**：execution direct kill 异常不阻止 persistence direct close，独立 cleaner 接管残留并确认 `active=0`。两项均未调用 DeepSeek，未输出凭据、Sandbox ID、OSS 原始路径或 provider 原文。2026-10-21 的默认 remote 阻断条件已由“持久化控制面移出不可信执行 Sandbox”这一路径解除；但安装缺省继续保持 local，RAM Role 最小权限仍必须由云侧 IAM/RAM Policy 审计和跨前缀拒绝 live 单独证明。

2026-10-22 RAM Role 最小权限增量验收：先复跑双 Sandbox security-boundaries 真实门禁并于 **68.590 秒通过**，固定报告仍为 persistence_unavailable=3、network_denials=4、resource_denials=3、reuse_checks=3、v2_roundtrips=1、backend_closed=1、active_zero=1。随后新增默认关闭的 `RUN_ALIYUN_SANDBOX_LIVE_OSS_RAM_POLICY_E2E` 门：正向对配置 prefix 的随机 marker 做 write/read/remove；负向保持同 Bucket、同 RAM Role，只把 `bucketPath` 改为规范化相邻 sibling prefix，并在任何路径精确清理 marker、kill 所有测试 Sandbox、执行 exact-session cleaner 与 active=0 复核。真实门禁两次均安全失败，第二次固定分类为 **`code=list_and_write`**（4.779 秒），证明相邻 prefix 的列举和写入都成功，当前 RAM Role/挂载组合权限过宽；测试标记与 Sandbox 均已清理。安装缺省必须继续保持 local，不得把 RAM Role 最小权限标记为通过，也不得正式签收 remote 生产迁移。新模块 13 项中 12 项离线通过、1 项 live 默认 skip；全量发现更新为 **848 tests**：824 通过、21 跳过、3 个既有 `skills/grilling` 失败，本轮无新增离线失败；`compileall` 与 `pip check` 通过。本机没有 RAM OpenAPI 凭据或 `aliyun` CLI，因此策略 JSON、全部 attached policies、trust policy 与 Bucket Policy 的静态审计仍需云侧只读权限或控制台脱敏导出；解除阻断必须先收紧策略，再让该跨前缀门禁真实通过。

2026-10-23 RAM 静态审计补充：控制台证据确认该角色唯一展示的权限为账号级系统策略 `AliyunOSSFullAccess`，内容是 `Action: oss:*`、`Resource: *`；信任策略允许 `functionai.fc.aliyuncs.com` 与 `fc.aliyuncs.com` 执行 `sts:AssumeRole`，用户确认目标 Bucket 没有 Bucket Policy。越权来源因此收敛到 FullAccess。已依据阿里云读写挂载官方动作集合生成 `config/aliyun-oss-persistence-minimal-policy.json`：Bucket 级 `oss:ListObjects` 由 `oss:Prefix` 精确限制，object 级仅允许 `GetObject`、`PutObject`、`DeleteObject`、`ListParts`、`AbortMultipartUpload` 到配置 prefix；无 `oss:*`、全局 Resource、Role ARN 或凭据。新增 `tests/test_aliyun_oss_ram_policy_document.py` 动态对照 `.env` 校验策略结构及 sibling 不匹配，定向 1 项通过。当前策略文件尚未应用云端，阻断不变；完整发现为 **849 tests**：825 通过、21 跳过、3 个既有失败，`compileall` 与 `pip check` 通过。

2026-10-23 RAM 最小权限替换后真实复验：操作者确认已把 `AliyunOSSFullAccess` 替换为上述自定义策略。OSS RAM prefix 门禁于 **9.667 秒通过**，固定报告为 `positive_roundtrips=1 sibling_denials=1 active_zero=1`，证明配置 prefix 的 write/read/remove 正常、同 Bucket 相邻 sibling prefix 的 list/write 均被拒绝，且测试对象与 Sandbox 全部清理。随后完整双 Sandbox security-boundaries 门禁于 **70.564 秒通过**，固定报告为 persistence_unavailable=3、network_denials=4、resource_denials=3、reuse_checks=3、v2_roundtrips=1、backend_closed=1、active_zero=1；权限收紧没有破坏 v2 持久化或资源回收。RAM Role prefix 最小权限阻断已解除；安装缺省仍保持 local，跨日可用性、并发流量、长期配额/成本和告警处置仍作为生产正式签收前的运营门槛。

2026-10-23 即时运营验收补充：最小权限替换后，固定三次串行默认 remote 批次再次 **3/3 通过**，总耗时 **49.240 秒**，单次 **17.717、14.861、16.662 秒**，`active_zero=3`，v2 object/generation 均精确 +3。随后新增默认关闭且不可由环境扩大的固定三会话并发 gate；三个独立 spawn 进程经跨进程 Barrier 同时放行，每个都复用完整默认 remote、v2 提交、显式 local 回滚和双 Sandbox 清理路径。真实并发波次 **3/3 通过**，总耗时 **18.609 秒**，单次 **17.434—17.902 秒**，`active_zero=3`、object/generation 均精确 +3、`delta_complete=1`。这证明当前账户在本时点至少可承受 3 个逻辑双 Sandbox 会话并发，但没有探测或推断账号最大配额。

同日 maintenance 复验先在 `runtime_path_check` 固定失败；根因已收敛为 test-only `_BackendStageRecorder.wrap_trusted_runtime` 仍使用旧签名、漏转发产品新增的 `discovered=` 参数，不是 OSS、RAM 或 GC 故障。修正后 A/B 两阶段都严格绑定 `SplitSandboxTransport`，各捕获 execution+persistence 两个不同物理资源，并以 session-scoped active=0 覆盖双角色关闭；真实 v3 恢复、direct-kill=False cleaner、crash orphan 精确删除及 manifest/3 个引用 object 保留门禁于 **62.932 秒通过**。

同日新增 `scripts/production_canary.py` 与 `docs/PRODUCTION_CANARY_RUNBOOK.md`：入口固定依次执行 RAM prefix、安全边界及“默认 remote 新会话→显式 local 回滚”三道精确门禁，900 秒总时限、强制禁网、子进程原文丢弃，只输出一行固定 JSON 和非零故障退出码。一次真实运维演练返回 `status=success`、`alert_required=false`、三道 gate 全部 `passed`、退出码 0。当前完整发现为 **874 tests**：849 通过、22 跳过、3 个既有 `skills/grilling` 消息序列失败；本轮无新增失败，`compileall` 与 `pip check` 通过。可立即编码和单次验证的验收项已完成；剩余仅是跨日可用性、长期配额/成本、自然 provider 故障与告警响应时效的持续运营证据，安装缺省继续保持 local。

## 置顶交接：当前核心缺陷与下一阶段主线

### 结论校正

2026-10-10 的真实云结果只证明了远端安全基线、OSS immutable baseline 恢复和当时的资源清理；2026-10-12 在离线测试中接通真实 `CodingAgent` 的远端异步执行与取消生命周期；2026-10-13 至 2026-10-14 的十次 5C6 运行逐步修复 harness 终态、E2B 缺失路径判断、Linux venv symlink 身份检查和 v2 清单上限冲突，第十次最终完成“模型编写代码 → 沙箱编辑文件 → 沙箱运行测试 → 保存修改 → 新沙箱恢复”的核心闭环。

该状态可以解释为“核心 Coding Agent 沙箱写入、编辑、测试、运行中取消、结构化超时、wait 结果不确定、handle 绑定前断线与 kill 成功确认丢失时的 runtime fence、跨 Sandbox v2/v3 恢复、事务级 object 回收、direct-kill=False 与受控真实 SDK/transport kill 异常后的独立 cleaner 接管、崩溃 orphan 引用扫描回收、控制面退出后的跨 OS 进程 stale 接管、内存/CPU/墙钟/输出资源压力、审批并发串行化和精确请求绑定，以及默认 remote 金丝雀/显式 local 回滚/固定三会话批次已经通过真实云，且本轮新建资源结束后无 provider-active 残留”，但仍不能解释为“所有生产安全场景均已验收”。更准确的说法是：**partial-control、`full-access`、运行中取消、结构化超时、两种 handle/result 不确定、kill 成功确认丢失、受控 SDK/传输 kill 异常、事务级与扫描式 orphan cleanup、v3 `format_json`、sealed-secret provider 前硬门、跨进程 stale 接管、资源压力、审批并发压力、首批迁移 canary 和 provider active=0 核验主路径已通过；未受控 provider 服务端故障没有被人为制造，命令级 handle/result 重附着仍受 provider 接口限制，长期迁移趋势属于持续运营观测而非一次构建任务。**

### 当前真实路由矩阵

| 能力 | local 会话 | aliyun-sandbox 会话当前状态 | 关键位置 |
| --- | --- | --- | --- |
| `read_file` / `list_files` / `grep` | 本地 Registry | 已路由到同一远端 active workspace | `tool/registry.py::_execute_through_runtime` |
| 同步 `code_interpreter` | 本地受限子进程 | 可经双重策略校验和一次性 permit 在远端执行 | `tool/registry.py`、`app/execution_runtime.py` |
| Agent 实际 `code_interpreter` | 本地后台任务 | 已通过 foreground policy + 精确一次性 capability 在远端后台执行 | `tool/registry.py::prepare_async_tool_call`、`app/execution_runtime.py` |
| `write_file` / `editor_file` | 本地候选、diff/审批、提交 | 已接独立远端 mutation transaction：候选仅在宿主内存，审批绑定严格 32 hex，稳定 operation id 为 64 hex；提交必须先建立 OSS object，再写 active、发布 create-only manifest 并回读验证 | `tool/registry.py`、`app/execution_runtime.py`、`app/workspace_versions.py` |
| `bash_shell` 的 `unittest` / `compileall` / `validate_json` | 本地后台受限进程 | 已通过 foreground 白名单/路径/确认策略在远端后台执行；可信 POSIX runner 另固定施加 AS/CPU/NPROC/FSIZE rlimit，缺界或非 POSIX 均在执行前拒绝 | `tool/registry.py::prepare_async_tool_call`、`tool/_sandbox_command_runner.py` |
| `bash_shell` 的 `format_json` | 固定拒绝，避免宿主工作区直接修改 | 同步远端多文件事务：Sandbox 隔离 staging 生成候选、宿主 diff 审批、单个 OSS v3 generation 提交；最多 8 个已存在 JSON 文件 | `tool/registry.py`、`app/execution_runtime.py`、`app/workspace_versions.py`、`tool/_sandbox_format_json_runner.py` |
| 其他 workspace 破坏性命令 | 按本地权限与审批策略 | 未开放；不得直接开放自由 shell | `tool/bash_shell.py`、`app/aliyun_sandbox_tools.py` |
| `list_pending_changes` / `apply_pending_change` | 宿主 `data/pending` | **固定 denied**；远端不能读取或复用宿主候选目录 | `tool/registry.py::_REMOTE_HOST_PENDING_TOOLS` |

远端异步执行使用 `PreparedRemoteInvocation` 一用对象：foreground 完成策略/用户确认后签发与 runtime generation、工具名、规范参数摘要及授权类型绑定的 capability；runner 消费后由 transport 在真正调用 provider background dispatch 的前一刻标记未知结果边界。cancel-before-dispatch 只撤销 capability；dispatch 后无 handle、kill/cleanup 失败、wait/输出不确定或 start-before-handle 断线都会同步 fence runtime 并尝试销毁 Sandbox。read/code/bash/mutation 共享同一 workspace gate；write/editor 与 command mutation 都从生成候选跨用户审批一直持有 reservation，commit/discard 后幂等释放，而 fence/close 不等待该 gate。`format_json` 不进入异步队列，避免后台任务中途再请求审批。核心闭环和真实运行中取消已通过，当前阻断收敛为其余故障边界及 provider 侧资源终态证据。

远端 workspace 工具被拒绝时**不会 fallback 到宿主本地 handler**。这个 fail-closed 性质必须永久保留：一旦持久会话绑定 `aliyun-sandbox`，项目代码写入、编辑、编译、测试和模型生成代码执行都只能发生在该会话的 Sandbox active workspace 内。

### 权限模式与执行后端是两个独立维度

新会话不得把 `partial-control` 误解为“自动走云端 Sandbox”，也不得把 `full-access` 误解为“自动回到宿主执行”。实际执行位置只由会话持久绑定的 execution backend 决定；权限模式只决定已选后端内哪些操作放行、拒绝或需要审批。

| 会话绑定 | `partial-control` 当前行为 | `full-access` 当前行为 | 尚未完成 |
| --- | --- | --- | --- |
| `local` | 继续使用宿主本地受限实现；普通写入/编辑直接执行，敏感本地 mutation 和 `unittest` 等策略命中操作走审批 | 已支持的普通操作不走外部审批并可使用宿主绝对路径；sealed-secret 路径仍固定拒绝 | 尚未自动迁移到 Sandbox；账号类工具尚未提供，未来接入时必须使用独立可信批准入口 |
| `aliyun-sandbox` | 所有 workspace 读写与执行只走 Sandbox；普通 write/edit 可直接执行，`unittest` 与多文件 `format_json` 需要审批；控制面和 sealed-secret 路径直接拒绝而不是交给审批 | 已开放的 write/edit/unittest 等普通操作不调用外部审批；多文件 `format_json` 仍要求一次真实 diff 审批。sealed-secret 硬门、Sandbox 根目录、结构化命令、资源上限和禁用自由 shell 约束始终有效 | sealed-secret 与 `format_json` 已有离线覆盖和一次合并 live 增量验收；账号类工具尚未提供 |

因此，目标策略中的“密钥和账号权限永不随 full-access 放开”已有代码拥有、不可由外置 policy 放宽的离线基线；但“低权限自动走 Sandbox”仍未实现，执行位置仍只能通过持久 backend binding 或新会话默认开关选择。账号登录、授权、密钥读取/轮换等工具目前尚未提供；未来接入时必须调用现有独立账号授权决策入口并绑定真正的可信宿主批准，不能把模型参数或 `full-access` 当作批准。

### 目标职责边界

- **宿主控制面保留：**DeepSeek/OpenAI 模型调用、工具 schema、参数验证、路径/能力策略、用户审批、一次性 capability 签发、SessionEvent、任务状态、审计、Sandbox 创建/销毁和 OSS checkpoint 协调。
- **Sandbox 数据面承担：**项目文件读取/写入/编辑、受限 Python、`unittest`、`compileall`、`validate_json`、`format_json` 候选生成及未来明确白名单内的构建命令。选择远端后不得调用宿主对应 workspace handler，也不得在宿主进程执行模型生成或项目内代码。
- **路径隔离：**所有远端操作固定在 `/workspace/agnet-study/<session_id>`；OSS persistence root、checkpoint namespace、宿主 `.env`、`.agent`、`data`、`skills`、`.git`、`code.md` 和凭据永远不作为模型可写工作区。
- **OSS 不是 active cwd，且不再进入 execution Sandbox：**execution 角色只承载临时 active workspace、可信 runtime 和项目命令，其 create metadata 不含 OSS mount/auth；persistence 角色独立挂载 OSS，只接受宿主窄 Files API 的固定 namespace 操作，不初始化 runtime 或执行项目代码。项目 runner 对 `/mnt/oss` 的 list/read/create 必须全部不可用且 `ismount=false`；持久化端的 v1/v2/v3 操作继续显式使用 root，并保持路径、大小、摘要、create-only 与删除后不存在复核。
- **无自由 shell：**继续使用结构化命令白名单、固定参数和有界输出；Sandbox 隔离不能替代宿主授权、路径限制、资源限制和审计。

### 已确认的架构限制与未完成项

1. **低权限不会自动选择 Sandbox。**permission mode 和 execution backend 是独立配置；现有会话依赖持久 backend binding，新会话只有在显式选择 `aliyun-sandbox` 或设置 `AGENT_DEFAULT_EXECUTION_BACKEND=aliyun-sandbox` 后才使用云端。安装缺省仍为 `local`，本地受限子进程尚未下线。
2. **密钥/账号永久安全域已完成离线基线和一次 remote live 前置门验收，但账号工具仍未接入。**`app/security_domain.py` 以纯词法、大小写不敏感的代码内规则识别 `.env*`、密钥/证书后缀、credentials/token 文件和常见凭据目录；local 文件工具、目录清空、扫描、remote Registry、Aliyun 工具组、workspace seed/version 均在 `full-access` 下继续硬拒绝，`allow_protected=True` 也不能绕过。2026-10-18 maintenance live 已证明 `.env` 读取在 transport/provider 前拒绝且没有秘密正文进入输出；未来账号工具仍必须把独立可信批准绑定到具体动作、身份和时效，当前只有决策入口，没有登录、授权、轮换或账号变更工具。
3. **多文件命令事务只开放了严格白名单切片。**`write_file` / `editor_file` 继续生成 OSS v2 generation；remote `format_json` 已按 snapshot → Sandbox 隔离候选 → 全局有界 diff → policy/审批 → 全量回滚或单个 OSS v3 generation 提交闭环，且 v2/v3 可连续恢复。目前仍没有通用文件系统 watcher、任意命令运行后 collector，其他会改文件的 build/codegen/package 命令也未开放；新增命令必须复用同一候选/差异/审批/提交协议，不能直接在 active cwd 后补扫描。
4. **跨进程资源恢复清理已完成离线接线与真实跨 OS 进程验收，但不会重附着旧命令。**控制面重启后会以固定 owner/session 标签有界重查并清理 stale Sandbox；持久 `started` 且无终态的 remote 任务只有在清理证明 active=0 后才记录 `outcome_unknown`，失败可在下次 resume 重试。provider 命令级 handle/result 没有可验证的持久重建接口，因此不会自动续跑或伪造结果；2026-10-18 maintenance live 已验证当前具体 Sandbox 的 direct `kill()` 返回严格 `False` 后，独立 provider cleaner 能按标签重查、重连、销毁并证明 active=0，同日独立跨进程 live 又验证进程 A 退出后仍 active=1、进程 B 按 exact owner/session 接管后 active=0。
5. **通用删除/清空与自由 shell 仍未开放。**generation 发布确定失败时，只针对本事务确认新建的 content object 执行 exact size/hash rollback delete；初始化期 GC 也只接受经双扫描证明未被任何 v2/v3 generation 引用的规范 content object。两条删除原语均为宿主内部能力，不注册给 Agent，不接受 manifest、目录或任意目标。`format_json` 只修改已存在文件，不创建或删除项目路径。
6. **v1/v2/v3 引用扫描与保留协议已完成离线与一次真实 OSS 增量验收，但仍有明确保留边界。**每会话最多 1000 代，单文件 1 MiB、最多 1000 文件、总量 32 MiB；GC 以当前 v1 baseline 为锚，保留所有 v2/v3 历史 full-state manifest 的 object 引用并集，只删除双扫描稳定的未引用 object。普通恢复仍忽略 orphan；GC 模式才校验全部 orphan 正文。maintenance live 已对一个真实未引用 object 完成双扫描、exact delete 与第三次复核，并逐字节确认 manifest 和 3 个被引用 object 保留；所有 generation manifest 和全部 v1 checkpoint roots 仍继续 immutable 保留，本轮不做跨 snapshot digest 的 v1 目录递归清理。
7. **真实云覆盖仍有限。**可信 venv、Agent 创建/编辑、失败测试→修复、两种权限模式、运行中取消、迟到结果隔离、结构化超时、handle 已绑定后的 wait 结果不确定、provider dispatch 后的 pre-bind 断线、kill 成功确认丢失、v2/v3 跨 Sandbox 恢复、受控 direct-kill=False 与真实 SDK/transport kill 异常后的独立 cleaner、sealed-secret provider 前拒绝、事务级与扫描式 orphan cleanup、控制面退出后的跨进程 stale 接管、内存/CPU/墙钟/输出资源压力、审批并发精确绑定与串行化、默认 remote 新会话 canary/显式 local 回滚、固定三会话串行批次、固定三个逻辑会话并发下界，以及对应 provider active=0 均已真实验证；但 SDK/transport 异常是受控注入，不等价于未受控 provider 服务端故障自然发生，单次并发 3/3 和近时点串行样本也不能证明长期配额、持续流量与跨日趋势。公开 AST 禁止进程创建，所以真实进程数压力未伪造，`RLIMIT_NPROC` 只具备离线精确设置证据。active=0 证明没有 running/paused 资源，不等价于 provider 历史对象或全部 OSS immutable 数据已删除。

### 下一阶段：5C Sandbox Coding Loop

新会话应把以下工作作为阿里云方向的唯一主线，按顺序实施，不能只扩大权限后就宣布完成：

1. **5C1 远端运行环境（真实云闭环已通过）：**固定 venv 位于 active workspace/OSS 外；缺失时才由 root 创建并固化，已有不合格环境 fail closed 且绝不 `--clear`；项目命令以固定非特权用户运行，每次执行前复查 realpath、版本、owner/mode、解释器 SHA-256 和 runtime metadata digest。2026-10-14 的第十次 5C6 运行已真实通过创建、冻结、非特权探测及身份复核。
2. **5C2 异步远端执行（核心 Coding loop 已真实通过）：**Agent 的 `code_interpreter`、`unittest`、`compileall`、`validate_json` 已经 `ExecutionToolRuntime` 派发到 Sandbox；保留 foreground 双重策略/确认、一次性 capability、输出/时间/内存/进程限制、observability、SessionEvent 和绝不本地 fallback。第十次 5C6 已真实验证同一 Agent 内的失败 unittest 与修复后成功 unittest，以及 Sandbox B 的恢复后 unittest。
3. **5C3 远端 mutation 与审批（partial-control 与 full-access 主路径均已真实通过）：**`write_file` / `editor_file` 已接入专用 remote transaction，保持两种宿主决策语义；远端候选不访问宿主 `data/pending`，提交后会复核原文件摘要与 workspace generation。第十次已真实验证 partial-control create/edit 与拒绝矩阵；第十一次在同一 Sandbox A 的空闲边界切换 `full-access`，对 partial-control 会要求审批的合成 `AGENTS.md` 执行 create/edit/unittest，外部 confirmation callback 计数未增加，Sandbox B 又恢复并精确读回最终内容。删除/清空继续关闭。
4. **5C4 远端取消与故障边界（运行中取消、结构化超时、wait/pre-bind/kill-ack 不确定性、受控 direct-kill=False、真实 SDK/transport kill 异常 cleaner、跨进程 stale 接管和关闭后 active=0 已真实通过）：**`cancel_async_task`、runtime close 与远端 handle 终止已关联；2026-10-14 独立 live 用例在任务状态为 running 且真实 provider run handle 已绑定后，由 Agent 调用取消，确认主终态保持 cancelled、主结果为空、迟到 error 仅进入 audit，随后同一 runtime/Sandbox 的短 unittest 成功。另一独立 live 用例验证长 unittest 在内部 2 秒超时后返回净化的 `exit_code=1` 与固定超时文案，并在同一 Sandbox ID 上成功复跑短测试。首次超时运行暴露内部 runner deadline 与 provider deadline 相同的收尾竞态；产品修复只给 provider handle 增加固定 5 秒、有界至 125 秒的收尾宽限，不放宽 payload 内用户代码执行时限，复验随后通过。第三个独立 live 用例在真实 dispatch 和 `_bind_handle` 原实现成功后，test-only 将一次 `_E2BRunHandle.wait()` 替换为固定异常；第四个用例在真实 provider raw handle 已返回、`_E2BRunHandle.__init__` 原实现完成后且 `_bind_handle` 调用前注入固定异常；第五个用例在 wait 不确定触发 fence 后，只包装当前具体 Sandbox 的 `kill()`，先要求真实调用严格返回 `True`，再注入成功确认丢失异常。三者都只给 Agent 严格净化的 failed/error 终态，runtime/backend 在模型看到终态前 closed，宿主 handler 没有 fallback，provider 查询确认各自 session active=0；kill-ack 用例还证明受管 cleanup 不会二次 kill。2026-10-18 maintenance live 又只对当前具体 Sandbox 注入 direct `kill()` 严格返回 `False`，随后真实独立 provider cleaner 接管并证明 active=0；独立跨进程 live 则让创建进程退出且不 kill，再由第二个 OS 进程按标签接管并外部证明 active `1 → 0`。2026-10-20 新增受控真实 SDK/transport timeout：在 exact provider 可见性屏障后让原始 `kill()` 抛出真实传输异常，独立 cleaner 随后精确 kill 一次并证明 active=0。execution Sandbox 创建 metadata 现加入固定 owner 与 32hex session 标签；该证据不等价于未受控 provider 服务端故障自然发生。
5. **5C5 可恢复修改层（单文件 v2、多文件 v3、事务级与扫描式 orphan cleanup 均已真实增量通过）：**immutable baseline 之上已有严格 v2 namespace、内容寻址 object 与 create-only full-state generation manifest；schema v2 表达 write/editor 单文件变化，schema v3 表达一次结构化命令的多文件变化，二者可位于同一连续链。无 mutable latest pointer，恢复只接受唯一连续无分叉链并校验 path/size/hash、baseline digest、声明的 before/after 转移和完整 active 清单。第十次 5C6 已由不同 Sandbox B 恢复 v2 修改并通过 read/list/unittest；2026-10-15 又真实验证确定 manifest 失败后的 active 回滚与本事务新建 object 精确删除。2026-10-18 maintenance live 以一次 diff 审批发布双文件 `format_json` v3 generation，由 Sandbox B 恢复并读回两个格式化文件，同时对一个真实 OSS crash orphan 完成双扫描、exact delete 和第三次复核；manifest 与 3 个被引用 object 均逐字节保留。generation manifest 与全部 v1 checkpoint roots 继续保留，不做目录级 GC。
6. **5C6 真实 Coding Agent E2E（2026-10-14 至 2026-10-23 分门控增量通过）：**脚本化 OpenAI-compatible client 不调用 DeepSeek，但所有工具都由真实 `CodingAgent` 循环发起；核心 A→B 已完成错误代码→失败 unittest→审批修复→通过 unittest、拒绝矩阵、full-access 无回调 create/edit 和 OSS v2 恢复。sentinel、宿主指纹和本地执行 mock 均证明这些用例没有使用 host workspace handler、项目子进程或宿主 `data/pending` 候选。测试要求独立 live process gate、Sandbox probe 和 recovery 三个显式门，默认 skip；固定诊断不保存 raw Sandbox、路径、命令、provider 正文或凭据。独立门控又补上运行中取消、结构化超时、wait 结果不确定、pre-bind 断线、kill 成功确认丢失、事务级 orphan cleanup，以及双文件 v3、staging 清理、sealed provider 前拒绝、direct-kill=False cleaner、扫描式 GC 和 v3 恢复；非 Agent 的资源维护 live 又完成跨进程 stale Sandbox 接管、有限资源压力、审批并发串行化/精确绑定与受控真实 SDK/transport kill 异常后的 cleaner 接管。2026-10-23 最小权限下串行 batch 3/3、固定三逻辑会话并发 3/3、双资源 maintenance 复验和生产 canary 三道门禁均通过；默认 remote 的显式 local 回滚、v2 增量与 active=0 继续成立。未受控 provider 服务端异常未被人为制造；长期迁移趋势仍需跨日持续观测。

7. **sealed-secret/account 安全域（2026-10-16 离线完成，真实云增量验收待补）：**新增代码拥有的纯词法安全域，逐组件识别 `.env*`、密钥/证书/keystore 后缀、常见 credentials/token 文件与凭据目录。local direct file、mutation、删除/清空、目录递归和 list/grep，remote Registry 输入、Aliyun 工具组二次校验、provider 返回，以及 workspace seed/v2/v3 generation 均不能被 `full-access`、mutation workflow 或外置 policy 放宽；普通 full-access 绝对路径仍保持原能力。账号动作有独立可信批准决策入口，但当前没有注册任何账号工具。新增离线覆盖证明 sealed 输入不会触发 provider 或本地 fallback，恶意远端 scan 结果不能进入模型可见输出。

8. **跨进程 session 资源恢复清理（2026-10-17 离线完成，2026-10-18 真实跨 OS 进程增量通过）：**新增独立、延迟导入 SDK 的 session cleaner，以固定 owner/session metadata 查询 active Sandbox，固定限制页大小、页数、对象总数、单请求和总时限；provider 返回的 metadata、state、ID 会在任何 connect/kill 前再次复核并去重，结束时必须重新扫描证明 active=0。backend 创建前强制清 stale，直接/候选 kill 未明确返回 `True` 时走独立 cleaner；两个通道都失败才返回固定净化错误。持久 remote 会话的 started-without-terminal 任务在 writer lock 内先清资源、后写 `outcome_unknown`，失败不落恢复终态以保留下次重试机会；accepted-only、已有 durable terminal、local、新会话和干净恢复均不触发启动清理。live harness 由进程 A 创建并退出、父进程外部确认 exact-session active=1、独立进程 B 调产品 cleaner、父进程复验 active=0；任何失败仍按同一随机 session 尽力应急清理。

9. **多文件结构化命令事务（2026-10-17 离线完成，真实 E2B/OSS 增量验收待补）：**新增同步 remote-only `format_json`。可信 runner 读取最多 8 个已存在普通 JSON 文件并只写内部 staging；transport 对 runner path/size/hash/staging 绑定和清理结果做二次验证；coordinator 复核规范 JSON、完整 active 快照和候选摘要。Registry 的唯一 diff 审批通过后，v3 generation 才按确定顺序写 active、逐项回读并 create-only 发布；任一确定失败逆序恢复所有已写文件，不确定结果 fence runtime。测试覆盖 v2 兼容、v3 canonical/重复键/伪造转移、审批拒绝、workspace gate、staging 恶意返回、object 准备失败、active 多写失败、manifest 确定失败及各种不确定边界。

10. **引用扫描式 orphan GC（2026-10-18 离线完成，真实 E2B/OSS 增量验收待补）：**CLI 在首次 remote 模型请求时先做宿主 seed/checkpoint 的纯本地预检，再以 session owner metadata 清理残留并证明 provider active=0；已有 started-without-terminal 恢复清理成功时不重复调用。新 Sandbox 初始化完成 v1 baseline 和 v2/v3 恢复后，协调器做两次稳定扫描，保留全部历史 full-state 引用并集，只通过单独 transport 原语精确删除未引用 object，最后第三次扫描复核。默认构造和普通恢复不启用 GC，损坏 orphan 不会阻断恢复；开启 GC 时损坏 orphan、任何链异常或扫描漂移都在删除前失败。该能力不向 Agent 暴露，且明确保留 generation manifest 和所有 v1 checkpoint root。

### 5C 完成定义

只有同时满足以下条件，才能把 `aliyun-sandbox` 标记为可用的 Coding Agent 执行后端：

- 真实 Agent 而非测试直接调用 Registry，成功完成远端写入、编辑、代码执行和测试；
- 所有项目代码执行均发生在 Sandbox 的受控 Linux venv，宿主只运行控制面；
- `partial-control`、`full-access`、敏感路径和审批竞态行为有离线及真实云覆盖；
- A 中已提交修改在 A 销毁后可由 B 完整恢复，恢复后测试仍通过；
- 取消、超时、异常、CLI 紧急退出和恢复路径无宿主 fallback、无未报告 Sandbox 泄漏；
- OSS baseline/overlay 的保留与 GC 规则明确，不会无限增长或误删仍被引用版本；
- 全量离线回归无新增失败，真实云报告明确区分安全边界测试和 Coding Agent 功能验收。

> 2026-10-23 更正：下一段是截至 2026-10-21 的历史基线快照，其中 `829 tests` 与旧阻断原因均已被后续结果取代；当前有效基线以本文件开头的双 Sandbox、RAM Role/并发/运维增量验收结果及测试矩阵合计行（874 tests）为准。

当前可复用基线：2026-10-14 核心 5C6 live 用例第十次 **通过，耗时 50.818 秒**，第十一次 full-access 扩展 **通过，耗时 64.739 秒**；独立 5C4 运行中取消 live 用例 **通过，耗时 24.954 秒**；独立结构化超时 live 用例在首次安全暴露 provider deadline 竞态并修复后 **通过，耗时 22.237 秒**。接通 provider active-list 后又复验 timeout **23.196 秒**、cancel **24.098 秒**、完整 A→B **60.871 秒**，四个新建 Sandbox 对应 session 均为 active=0；已绑定 handle 后的 wait 结果不确定 live 用例又于修正 harness 文案后 **通过，耗时 18.594 秒**，provider dispatch 后的 pre-bind 断线用例 **通过，耗时 18.439 秒**，kill 成功确认丢失用例 **通过，耗时 19.107 秒**，对应 session 均为 active=0。2026-10-15 事务级 orphan cleanup live 用例在两次只暴露 harness 断言错误且核心清理/active=0 已完成后，第三次 **通过，耗时 26.140 秒**。2026-10-17 跨进程 session 清理核心定向 **56 项全部通过**，相关 runtime/CLI 定向 **76 项全部通过**，多文件命令事务主审定向 **158 项全部通过**；2026-10-18 引用扫描式 orphan GC 相关主审定向 **211 项全部通过**。同日跨进程 stale live 首次因 A 内 metadata 索引即时可见假设而安全失败，父进程 emergency cleanup 未报告错误；修正为 A 退出后的有界可见性轮询后第二次 **通过，耗时 6.102 秒**。2026-10-19 资源压力 live 首次在前三类压力和复用已通过后暴露 runner 截断标志丢失，修复 code/command 两条 transport 协议后第二次 **通过，耗时 27.194 秒**，对应 session active=0。2026-10-20 审批并发 live **通过，耗时 21.159 秒**；默认 remote 单次 canary **通过，耗时 15.144 秒**，固定三会话 batch 又 **3/3 通过，总耗时 42.609 秒**，两个观测时点累计 v2 object/generation 各 +4、显式 local 回滚 4 次并逐次 active=0。受控真实 SDK/transport kill 异常 live 在前两轮分别安全暴露无效凭据假设与 metadata 可见性窗口且均由 finally 确认 active=0；加入精确可见性屏障后第三轮 **通过，耗时 3.856 秒**，确认 direct exception、独立 cleaner kill、backend close 和 active=0 各一次。最新默认关闭真实云门禁的全量发现为 **829 tests**：806 项通过、20 项跳过、3 项失败；3 个失败仍是既有 `skills/grilling` Skill catalog 消息序列断言，本轮没有新增失败。security-boundaries 新模块 **10 项 OK（1 项 live 默认 skip）**，相关配置、transport、rlimit、backend、v2 及既有 live harness 离线协议回归 **181 项 OK（2 项 live 默认 skip）**；`compileall` 与 `pip check` 均通过。新增 security-boundaries 真实云验收已于 2026-10-21 执行并在 transport ready 前安全失败，确认当前 OSS 动态挂载不能建立 root-only persistence 隔离；remote 生产迁移因此阻断。

## 新会话先做什么

1. 先阅读本文件，再阅读 [`AGENTS.md`](AGENTS.md)。
2. 所有 Python 命令和测试必须使用项目虚拟环境：

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   ```

3. 不要读取、打印或提交真实 `.env` 内容；配置字段只参考 [`.env.example`](.env.example)。
4. 不要下载 Chrome、Chrome CLI、Bash 或 WSL。项目当前 Windows 环境没有可用的 WSL 发行版，`bash_shell` 已实现为不依赖 Bash 的跨平台受限调度器。
5. 修改工具时必须同步更新：
   - `.agent/tool_policy.json` 中的路径与命令白名单；
   - `app/schemas.py` 中的输入输出模型；
   - `tool/registry.py` 中的注册、输出校验和 `TOOLSCHEMAS`；
   - 相应单元测试、权限测试与本文档。
6. 当前目录还没有初始化为 Git 仓库；`.git` 仍按敏感目录永久对 Agent 工具隐藏。

## 当前能力

- 使用 OpenAI Python SDK 的 Chat Completions 接口连接 DeepSeek 兼容服务；
- 每个 `CodingAgent` 实例默认从固定项目根的 `code.md` 读取系统提示词，用户可通过宿主编辑器修改角色、性格和行为约束；
- Chat Completions 固定使用 `stream=True`，模型正文按 chunk 实时输出，并严格重组分片的 function tool call；
- 从只追加的 SessionEvent 事件流重建 user、assistant、tool 上下文，并加载当前 `code.md` 作为 system 消息；CLI 无参数时仍恢复 legacy `current`，也可安全新建、列出或恢复独立命名会话，因此多个终端可同时持有不同会话的单写者锁；运行期抢先读取遇到临时 EOF 不再导致首个任务后关闭进程；
- 完整会话事件采用严格 JSON 快照、连续编号、深度冻结和可靠写入检查点；保存失败停止工作流，进程恢复时补齐未知工具结果和中断边界；
- 宿主可以显式追加摘要并替换完整旧轮的上下文投影；原始事件保留，自动摘要阈值尚未启用；
- CLI 在显示 banner 和读取首条输入前只加载轻量配置与会话存储；选择 remote 时还会在新目录创建前加载并校验 Sandbox 配置。OpenAI SDK、客户端、Agent、Observability 和维护线程直到首个模型请求才创建；remote backend/runtime 也到该时刻才组装内存对象，真实 Sandbox 仍只在首次远端工具操作时惰性创建。`exit`/`quit` 不创建客户端或 runtime；`--help` 和 `--list-sessions` 不加载模型/Sandbox 配置，也不导入 OpenAI/E2B SDK；
- 严格解析并校验 function tool call，默认最多执行 8 个“单一用户轮内、含工具调用的 assistant step”；没有总会话轮次上限；
- 注册 `read_file`、`editor_file`、`list_files`、`grep`、`write_file`、`list_pending_changes`、`apply_pending_change`、`inspect_recovery`、`resolve_recovery`、`load_skill`、`read_skill_resource`、`bash_shell`、`code_interpreter`、`web_fetch`、`web_search`、`get_async_task`、`cancel_async_task`；
- Agent 初始化时只从固定 `skills/` 扫描每个 `SKILL.md` 的有界 front matter，把 name/description 目录作为临时 user 级消息加入 SDK 请求而不写入 SessionEvent；模型选中后才加载完整说明，并在后续 model step 用会话运行时令牌分页读取必要资源；
- `code_interpreter` 以及 `bash_shell` 的 `unittest`、`compileall`、`validate_json` 默认转为后台任务；原始调用只返回一次带 `task_id` 的 `accepted` 占位结果，真实终态通过新事件注入，并由 Windows Job Object 或 POSIX 进程组提供物理取消句柄；
- 同一个用户 turn 可包含多个 model step；同一 assistant step 可包含多个 tool call，全部调用先各自闭合结果/占位符，再开始下一 step。后台终态重评估保持在原 turn 内，不再伪造新的用户消息；
- 普通事件按 32 条、256 KiB 或 100 ms 批量提交；屏障事件先排空旧普通事件，紧急事件作废旧 generation，并保证紧急事件位于本次批次末尾；
- 所有文件和路径操作数共享外置白名单与代码内敏感路径底线；
- CLI 由可信终端宿主选择 `partial-control` 或 `full-access`：前者普通文件修改不确认、敏感文件修改只确认一次 diff，后者本机普通文件修改和受控网络工具均不请求人工审批；空闲主提示符可用 `/permissions` 查看或切换，提权必须额外输入 `yes`；
- 紧急退出把取消标志和“禁止迟到提交”作为硬边界，主线程不再同步等待可能卡住的模型流、SDK `close()`、事件排空或维护刷新；
- Code Interpreter 在空临时目录、净化环境、AST 能力白名单和操作系统资源限制下执行纯 Python 计算；
- `bash_shell` 的 `python` 操作会委托 Code Interpreter，不会开放自由 Python CLI；
- 每次工具调用都有稳定的工具 UUID 和独立的调用 UUID，并生成不含敏感数据的调用/结果 JSON 日志；
- 首个模型运行时初始化成功后启动脱敏日志维护，并立即及每小时执行保留维护和缓冲重试；候选清理只由新建、列出或应用候选触发；
- 配置、客户端、Agent、注册表、Schema、所有工具和权限边界均有离线自动化测试；
- 提供默认跳过、只能显式启用的真实 DeepSeek API 端到端测试。
- 提供与主 Agent 解耦的阿里云 Sandbox + OSS 动态挂载探针：默认命令不加载配置、不导入 E2B SDK、不创建云资源；只有 `sandbox-probe --execute` 且配置中的探针开关显式开启后，才会创建一个 Sandbox、写入/回读/删除唯一 OSS marker，并在成功或异常时尝试销毁 Sandbox；阶段 2 还要求恢复开关和 `sandbox-recovery-probe --execute`，按 A→kill→B 的顺序验证 OSS 检查点恢复。
- 提供独立的 `RUN_ALIYUN_SANDBOX_LIVE_AGENT_E2E=1` 显式门控；普通回归默认跳过该真实云测试。该测试仅上传 3 个小型合成文件，通过 Agent 实际 Registry/runtime 路径验证 checkpoint→active、跨 Sandbox 恢复和工具权限。
- Sandbox 配置使用严格 Pydantic 模型，要求 API URL、Domain 与 OSS Endpoint 同属一个阿里云地域，拒绝 Bucket 根目录、`/mnt` 外挂载、只读探针和含凭据 URL；E2B API Key 不进入 `repr`/JSON，CLI 不展示第三方异常请求上下文；
- 当前 Sandbox 代码包含阶段 1/2 独立探针、阶段 3 生命周期适配器及 session-scoped Agent/runtime 接缝；DeepSeek/OpenAI 控制面始终留在宿主进程。remote Agent 把 `read_file`、`list_files`、`grep`、`write_file`、`editor_file`、异步 `code_interpreter` 以及 `bash_shell` 的 `unittest` / `compileall` / `validate_json` 路由到同一 Sandbox workspace；写入/编辑只能走专用 mutation transaction，删除/清空、自由 shell 和宿主 pending 工具明确拒绝。
- 阶段 4 建立未注册的 `AliyunSandboxToolGroup`、可注入 `SandboxTransport` 协议和惰性 `E2BSandboxTransport` 窄适配器：read/write/edit/list/grep/command/code 共享一个 lease workspace 和 cwd；OSS mount 只作为 persistence root，不作为活跃工作区。5C 第一门在这层增加固定可信 venv、非特权 runner、background handle 与精确 dispatch 边界；适配器仍不替代审批或策略，原始 E2B 对象只留在 backend/transport 私域。
- 阶段 5B1 为每个 `SessionEventWorkflow` 固定只读的 `execution_backend_name`。内存会话只在实例内绑定；持久会话在事件提交前用规范 UTF-8 `execution.json` 原子绑定 `local` 或 `aliyun-sandbox`，重开必须精确匹配。旧 header/事件无 sidecar 的会话永久按 local 迁移，不能升级为远端；sidecar 的链接、硬链接、特殊文件、超限、非规范 JSON、额外字段和会话身份不匹配均 fail closed。
- 2026-10-15 完成绑定驱动的安全自动恢复：CLI 的 `--execution-backend` 变为可省略请求值；新存储省略时仍持久绑定 `local`，恢复已有存储省略时则采用锁内严格解析的 `execution.json`。显式参数只作匹配约束，local/remote 双向错配在加载 Sandbox 配置前失败。新存储先发布 binding 再发布 header；若恰在两者之间崩溃，安全的 binding-only 目录可被列出并恢复同一 session identity。远端配置加载失败发生在恢复事件之前，并保证释放 writer lock、不改写身份/事件、不创建 runtime/client、也不扫描宿主 workspace。
- 同日增加新会话默认后端的独立按需配置 `AGENT_DEFAULT_EXECUTION_BACKEND`。它只在“会话尚无持久身份且 CLI 未显式指定后端”时读取，系统环境优先 `.env`，缺省 `local`，只接受 `local` / `aliyun-sandbox`；非法值使用固定净化错误。显式 CLI、`--resume` 和已有 `current` 完全不解析该值。选择 remote 时先验证 Sandbox 总开关/配置再创建目录或身份；选择 local 或显式回滚 local 时不加载 Sandbox。解析后的新会话默认值作为精确 workflow 请求，检测到加锁之间若出现相反绑定会 fail closed，不会静默降级。
- 阶段 5B2a 只为 Registry 增加可选 `ExecutionToolRuntime` 接缝：remote runtime 的 `read_file`、`list_files`、`grep` 经同一 runtime 执行，并再次经过现有输出模型、关联 validator、`ToolResult` 和 observability；`write_file`、`editor_file`、`bash_shell`、`code_interpreter` 在同步与异步准备路径均明确拒绝，不调用本地 handler、不触碰本地路径准备、也不启动远端特权执行。runtime 缺失或为 local 时保持原 Registry 行为；非 workspace 工具不受影响。
- 阶段 5B2b 补齐只读策略等价和控制面隔离：remote `read_file` 在 Registry 进入 runtime 前只做共享的纯词法 POSIX 相对路径校验；`partial-control` 加载宿主策略并同时要求 allowed、非 protected，`full-access` 仍禁止 sandbox 越界表示，并可沿用本机 direct-read 语义读取普通 protected 路径，但 2026-10-16 起 sealed-secret 路径在任何模式下固定拒绝。远端 `list_files`/`grep` 把一次操作的宿主策略 predicate 下推到 E2B transport，在列举和读取正文前过滤 protected/不允许/sealed 路径；目录只按 protected/ignored/sealed 剪枝，不因 allowed glob 尚未命中而误剪可能含允许文件的祖先目录，工具组再对 transport 返回做二次校验。remote runtime 的 `list_pending_changes`/`apply_pending_change` 固定拒绝，local/`None` 不变。
- 阶段 5B2c/5B2c1 当时只同步放行 remote `code_interpreter`：Registry 在宿主用最新策略校验 enabled、timeout、AST 节点上限及 AST 能力白名单，不启动本机子进程；通过 `ExecutionToolRuntime.execute_authorized` 的真实 callback 签发并立即消费一次性 policy permit。远端工具组消费 permit 后再次加载最新策略，把该次调用的 timeout 与第二次策略的 `max_output_characters` / `max_memory_mb` / `max_processes` 一起下推为五字段 runner payload。runner 严格拒绝额外的 `max_timeout_seconds`：该上限已由宿主和远端工具组的两次最新策略校验保证，runner 只再校验实际 timeout 在 1..10 内。Linux/POSIX runner 在 compile/exec 前二次设置不放宽既有更严限制的 `RLIMIT_AS` / `RLIMIT_CPU` / 可用时的 `RLIMIT_NPROC` / `RLIMIT_FSIZE`；Windows runner 不导入 `resource`，继续依赖父进程 Job Object。策略/授权拒绝固定为 denied，其余 runtime/transport/runner/输出校验失败固定为 error，不泄露底层详情且不本地 fallback。5C 第一门已在不改变 local/`None` 的前提下补上异步 code 和三个远端验证命令；write/editor 仍拒绝。
- 阶段 5B3a/5B3b 把精确绑定的 `ExecutionToolRuntime` 接入 Agent 生命周期，并由 CLI 的 `--execution-backend {local,aliyun-sandbox}` 显式选择。local 默认路径不加载 Sandbox 配置、不构造 runtime，也不向 `CodingAgent` 传额外参数；remote 新建/恢复要求持久 `execution.json` 与本次选择精确一致，并在新目录创建前验证复用的显式云总开关和严格 Sandbox 配置。首次真实模型请求才构造 backend/runtime 内存对象并交给 Agent，构造过程不 `ensure_ready`、不导入 E2B SDK、不创建云资源；首次远端工具操作才可能创建 Sandbox。该阶段完成时 Agent 只远端路由 read/list/grep；5C 第一门现已补上 async code/bash，mutation 仍 fail closed。
- 阶段 5B4a/5B4b 在每次 CLI 进程首次 remote 模型请求组装 runtime 时，从当前宿主项目捕获一次有界不可变 seed；直接退出、空 Plan 取消、local、help 和 list 路径不扫描。seed 复用 `scan_project_files('**/*')` 与 `read_file` 的策略/句柄边界，拒绝截断扫描、链接、硬链接、竞态和不完整读取；上限为 1,000 文件、单文件 1 MiB、总计 32 MiB，并以规范 path/size/sha256 manifest 建立摘要。除最新 policy 外，代码内永久排除整个 `data/`、`skills/`、`.agent/`、`.git/`、`.venv/`、缓存目录、`code.md` 和所有 `.env*`（包括 `.env.example`）。首次远端只读工具触发 `ensure_ready()` 后才创建 Sandbox；typed transport 在固定 `<persistence_root>/.agnet-study/checkpoints/v1/<session_id>/<snapshot.digest>` 中安全续写或验证内容寻址 checkpoint，严格 manifest 最后 create-only 发布，再从已校验 checkpoint 恢复到固定 `/workspace/agnet-study/<session_id>`。OSS mount 永远不是 cwd/活跃源码目录；checkpoint 是当前宿主 snapshot 的 immutable baseline cache，不保存远端 mutation，不做指针、删除或 GC。

## 目录和模块职责

| 路径 | 职责 |
| --- | --- |
| `pyproject.toml` | 项目打包元数据；固定 `e2b==2.49.1`，并注册 `agent` 与 `sandbox-probe` 两个 console script。 |
| `agent.cmd` / `agent.ps1` / `agent.sh` | Windows 与 POSIX 根目录启动器；固定使用项目 `.venv`，不修改 PATH、Profile 或 shell 配置。 |
| `.agent/tool_policy.json` | 第 5 版外置策略：路径白名单、受保护路径、shell 命令白名单/审批要求、Code Interpreter 资源上限，以及分别显式启用的搜索/读取网络边界。旧版或缺少网络字段会整体 fail closed，绝不默认开网。 |
| `app/permissions.py` | 可信宿主权限模式、不可由模型覆盖的敏感路径分类和统一修改审批路由。 |
| `app/security_domain.py` | 不受 permission mode 或外置 policy 放宽的 sealed-secret 路径硬门，以及未来账号动作的独立可信批准决策；只做纯词法分类，不读取秘密正文。 |
| `app/daemon_executor.py` | 不参与解释器退出等待的 daemon worker，以及紧急退出期间的尽力后台清理。 |
| `app/main.py` | 交互式 CLI；显式后端选择、已有会话绑定自动恢复、新会话默认后端迁移开关、Sandbox 配置预检、惰性 backend/runtime 与模型运行时组装、单次 `/plan` 交互、状态命令、唯一输入 broker、实时流式输出，以及 runtime/客户端/会话/维护的安全清理。remote 首次请求在纯本地 seed 预检后证明同 session provider active=0，再把启用初始化期 orphan GC 的惰性 runtime 交给 Agent；direct-exit/local/help/list 仍零云。 |
| `app/console_ui.py` | TTY 感知的文本呈现层；非 TTY、`--plain` 或设置 `NO_COLOR` 时不输出 ANSI。 |
| `app/terminal_input.py` | 延迟加载 `prompt_toolkit` 的 act 主命令斜杠补全；连续空闲轮次与普通 act 的运行期预读保留补全，Plan 澄清和非 TTY 环境可靠复用同一 broker。 |
| `app/plan_mode.py` | 模型驱动 Plan 的严格 JSON/Pydantic 协议、跨请求三选一澄清、选择数据编码，以及无未决问题 Markdown 的确定性分块渲染；不启动浏览器或本地 HTTP 服务。 |
| `app/agent.py` | 流式模型循环、Plan 校验后分块输出、SessionEvent 检查点与消息投影、SDK 适配、工具调用分片重组，以及审批和会话状态机的生命周期管理。 |
| `app/skills.py` | 项目 Skill 的有界元数据发现、按需完整加载、UTF-8 资源分页、会话运行时令牌和文件树快照复核。 |
| `app/async_tasks.py` | 会话绑定的后台任务管理器；分离 accepted/start，限制 worker 数量，提供进度、查询、逻辑取消、迟到结果审计与只恢复状态不恢复 worker；共享严格事件 projector 区分 accepted-only、started-without-terminal 和已有 durable terminal。 |
| `app/event_queue.py` | 普通事件批处理、屏障可靠提交、紧急事件排序和 generation 取消回调，并把运行时任务事件映射为 SessionEvent。 |
| `app/observability.py` | 工具调用和结果的隐私安全日志；按本地日期和 2,000 条/8 MiB 双阈值分片，遇到损坏或不安全状态时停止当前落盘，并用有界内存队列顺序重试失败事件。 |
| `app/maintenance.py` | 无构造副作用的 daemon 维护服务；启动时立即、之后每小时独立 fail-soft 地清理脱敏日志并补写缓冲；不清理候选或会话。 |
| `app/workflow.py` | SessionEvent 的持久化、连续事件状态机、消息/摘要投影和崩溃恢复；在独立 `execution.json` 中固定持久会话的 local/aliyun-sandbox 执行后端身份；把异步 SessionEvent 转为共享恢复 projector，并保留一次性、两阶段和既有候选的文件审批状态机。 |
| `app/config.py` | 从系统环境变量或根目录 `.env` 加载并验证 DeepSeek 配置。 |
| `app/client.py` | 构造 OpenAI SDK 的 DeepSeek 兼容客户端。 |
| `app/sandbox_config.py` | 默认关闭的阿里云 Sandbox/OSS 严格配置模型与加载边界；不导入 E2B SDK，不产生云调用。 |
| `app/aliyun_sandbox_probe.py` | 延迟导入 E2B SDK 的阶段 1 连通性探针；构造 OSS metadata，执行 marker 写读删，并在 `finally` 路径销毁 Sandbox。 |
| `app/aliyun_sandbox_recovery_probe.py` | 阶段 2 跨 Sandbox 检查点恢复探针；manifest 与 A 内存快照严格相等、逐文件 size/hash 校验，异常时只通过 Filesystem API 删除精确路径，并对 B 创建失败最多尝试一次清理 Sandbox C。 |
| `app/execution_backend.py` | 执行后端生命周期边界；提供不可变 workspace/lease、本地 no-op 后端和按安全 session 惰性创建的 Aliyun 双 Sandbox 适配器。execution metadata 明确不含 OSS/auth，persistence metadata 才包含挂载与 RAM Role；两个资源必须对象与 ID 均分离，并以 role 标签区分。创建前清理同 session stale 资源，创建/初始化失败回收全部候选；关闭先停 execution、再停 persistence，任一 direct kill 未严格成功都会继续处理另一资源并由独立 cleaner 兜底。租约只公开 execution ID，不暴露 persistence ID、raw SDK 或凭据。 |
| `app/aliyun_sandbox_session_cleanup.py` | 跨进程 session-scoped provider 资源恢复清理：精确 owner/session 查询、有界分页/对象/时限、返回项二次验证、按 ID 重连销毁和最终 active=0 证明；延迟导入 E2B，错误边界不泄露 provider 正文、ID 或密钥。 |
| `app/workspace_seed.py` | 阶段 5B4a 宿主只读 seed 捕获：不可变正文/manifest、硬排除与三重大小上限；initializer factory 在宿主侧预检 checkpoint manifest/layout 后委托 5B4b，不接触 raw SDK、shell 或 archive。 |
| `app/workspace_checkpoint.py` | 阶段 5B4b OSS immutable baseline：严格 canonical JSON manifest、固定内容寻址路径、半成品安全续写、manifest 最后发布、完整 checkpoint 复核及 active workspace create-only 恢复；宿主 snapshot 始终是权威，不信任 OSS manifest 自报 digest。 |
| `app/workspace_versions.py` | OSS v2/v3 durable overlay：canonical full-state manifest、连续无分叉恢复、单/多文件 mutation 事务、确定失败的事务级 object 回收，以及默认关闭的宿主初始化期 orphan GC。GC 以 v1 baseline digest 为锚，保留全部历史 manifest 引用并集，要求双扫描稳定并在精确删除后第三次复核；普通恢复仍忽略 orphan。 |
| `app/sandbox_transport.py` | 类型化 transport 协议、有界结果、execution E2B 适配器、窄 `E2BPersistenceTransport` 与 `SplitSandboxTransport`。execution 端固定可信 venv、非特权 runtime user、唯一 invocation 目录和可取消 handle，不绑定 OSS；persistence 端只验证 OSS mount 是普通目录并只用显式 root Files API，不建 workspace/runtime、不调用 Commands API 或 chmod/chown。组合门面将 active/command/active rollback 路由到 execution，将 v1/v2/v3、object rollback/GC 路由到 persistence。原有 namespace、路径、大小、摘要、create-only、符号链接拒绝和删除后不存在复核保持不变，raw SDK、OSS mount 与删除能力不向 Agent/Registry 暴露。 |
| `app/aliyun_sandbox_tools.py` | 内部远端工具组适配层；执行严格 POSIX 相对路径、workspace/cwd、输出/超时和 close 闸门，并为远端列举/搜索加载宿主策略、下推 predicate、复核返回路径；消费一次性 permit 后复核 Code Interpreter 或结构化命令策略，并把 dispatch-attempt 与 run handle 交还 runtime；不注册给 Agent。 |
| `ALIYUN_CLOUD_VALIDATION_REPORT.md` | 2026-10-10 真实云验证的脱敏报告：资源生命周期、OSS 恢复、Registry 权限矩阵、凭据边界及已知限制。 |
| `app/schemas.py` | 内部消息、不可变 SessionEvent 及载荷、异步任务/宿主事件、恢复状态、文件审批状态、候选清单和 17 个工具的严格输入输出模型。 |
| `app/recovery.py` | 未知修改结果的只读外部状态检查，以及会话/目标/观察绑定的一次性恢复令牌状态机。 |
| `tool/policy.py` | 安全加载外置策略并执行统一路径判定；失败时 fail closed。 |
| `tool/file_access.py` | 固定项目根、路径词法检查、白名单、句柄、链接、硬链接和写入恢复基础设施。 |
| `tool/pending_changes.py` | 固定 `data/pending` 候选与严格清单存储、双重完整性校验、沿用候选锁的 7 天安全清理和有界 unified diff 预览。 |
| `tool/pending_tools.py` | 只读列出有效候选，并在一次新的 diff 审批后复用原候选写入正式文件。 |
| `tool/project_scan.py` | `list_files` 与 `grep` 的有界扫描器；敏感项不会被枚举或遍历。 |
| `tool/bash_shell.py` | 结构化受限命令调度、审批、临时工作副本、输出上限和超时处理。 |
| `tool/code_interpreter.py` | Code Interpreter 父进程边界：提供不启动进程的共享策略/AST 校验，并为本机执行建立净化环境、临时目录、超时和 OS 资源限制。 |
| `tool/web_fetch.py` | 受控公网 HTTP/HTTPS GET：全 DNS 结果公网校验、IP 固定连接、TLS 主机名验证、逐跳重定向复核和文本/字节上限。 |
| `tool/web_search.py` | 固定 Brave HTTPS API 的只读搜索：宿主密钥隔离、全 DNS 公网校验、IP 固定/TLS SNI、无重定向和有界 JSON 解析。 |
| `tool/process_cancel.py` | 后台进程树取消协议：线程安全的 sticky cancel-before-bind、Windows Job Object 与 POSIX 独立进程组绑定。 |
| `tool/_code_interpreter_runner.py` | 不对模型注册的子进程入口；有界读取严格五字段 JSON 请求，Linux/POSIX 二次收紧 OS 资源限制后才执行白名单内的计算语法和函数。 |
| `tool/_sandbox_command_runner.py` | 不对模型注册的结构化命令子进程入口；只接受固定命令/路径/超时/输出 payload，POSIX `preexec_fn` 固定收紧 AS=256 MiB、CPU=30/31 秒、NPROC=4、FSIZE=16 MiB，缺界、设置失败或非 POSIX 均 fail closed，并保持 `shell=False`、wall timeout、输出限制和进程组清理。 |
| `tool/registry.py` | 唯一工具注册入口、参数/返回值复核及审批回调分发；remote 将读搜、专用事务化写入/编辑、同步/异步 Code Interpreter 以及异步 `unittest` / `compileall` / `validate_json` 按宿主策略路由到同一 runtime。异步路径在 foreground 完成精确授权并创建一用 invocation；旧通用授权入口不能绕过 mutation transaction，删除/清空和两个宿主 pending 工具继续 fail closed。 |
| `code.md` | 默认系统提示词；由宿主用户编辑，但被策略和代码内底线保护，Agent 的文件与 shell 工具不能读取、扫描、改写或删除。 |
| `skills/` | 开发者维护的项目 Skill 根目录；`README.md` 说明格式，`_template` 提供脚手架；partial-control 下普通读取/搜索/验证型 shell 不可见，修改按敏感路径审批。 |
| `tests/` | 单元、协议、权限、Plan/CLI、Skill、网络边界、异步任务/队列、状态机、会话恢复、多会话 CLI、进程取消、维护服务、可观测性，以及默认跳过的 DeepSeek/阿里云真实 E2E。 |

## 启动和配置

首次创建虚拟环境后，或 `pyproject.toml` 中的命令入口发生变化后，安装一次项目入口：

```powershell
.\.venv\Scripts\python.exe -m pip install --editable .
```

之后在 PowerShell 中激活项目虚拟环境，即可在项目根目录使用裸命令启动：

```powershell
.\.venv\Scripts\Activate.ps1
agent
```

当前项目虚拟环境已经完成 editable 安装，`agent` 会解析到 `.venv\Scripts\agent.exe`。输入 `exit` 或 `quit` 退出。

会话选择参数互斥；不带参数保持恢复旧的 `current` 会话：

```powershell
agent                         # 恢复 data/sessions/current
agent --new-session feature-a # 以安全单目录名创建新会话
agent --new-session           # 自动生成唯一会话名
agent --list-sessions         # 仅列名称，不加载配置或 API 客户端
agent --resume feature-a      # 恢复安全 session.json，或 binding-first 崩溃窗中的 execution.json
agent --plain                 # 强制纯文本，不输出 ANSI 样式
agent --permission-mode partial-control # 默认：普通文件自动，敏感修改审批
agent --permission-mode full-access     # 高风险：本机文件和网络工具不审批
agent --new-session remote-a --execution-backend aliyun-sandbox # 显式新建远端绑定会话
agent --resume remote-a                                      # 自动采用已验证的远端持久绑定
agent --resume remote-a --execution-backend aliyun-sandbox   # 可显式约束；不匹配则拒绝
```

`agent.cmd` 和 `agent.ps1` 会原样透传上述参数。新会话省略 `--execution-backend` 时采用 `AGENT_DEFAULT_EXECUTION_BACKEND`（安装缺省 `local`）；恢复会话省略时采用 `execution.json` 中已验证的绑定，显式指定时必须精确一致。持久会话首次创建后不能升级或降级。把环境变量设回 `local` 或对新会话显式传 `--execution-backend local` 即可回滚后续新建行为，不会改写已有远端会话。显式名称只允许安全 ASCII 单目录名，拒绝绝对路径、路径穿越、Windows 保留设备名和重名；不同命名会话可同时启动，同一会话仍会拒绝第二个写入者。

权限模式默认 `partial-control`，不会从模型工具参数、提示词或会话消息读取。空闲主提示符输入 `/permissions` 可查看当前模式和风险；`/permissions partial-control` 立即降级，`/permissions full-access` 必须通过同一 input broker 再次明确输入 `yes` 才提权。切换只影响后续新调用，不追溯正在执行的调用；重启后仍以 `--permission-mode` 为准。

Windows PowerShell 出于安全原因不会自动搜索当前目录，因此在**未激活**虚拟环境的 PowerShell 中，根目录的启动方式是 `.\agent.cmd` 或 `.\agent.ps1`，不能写成裸 `agent`。传统 CMD 会搜索当前目录，可以直接执行 `agent`。Ubuntu/POSIX 可在建立 `.venv` 后使用 `./agent.sh`。启动器只使用项目 `.venv`，不会写入用户/系统 PATH，也不会修改 shell 配置。导入入口、查看帮助和列出会话都不会导入 OpenAI SDK；正常交互先显示 CLI，后台模型服务检查会加载 SDK 和独立客户端，通过后才接受需求，Agent 与工具运行时仍按需初始化。

此前 Windows 主机的 5 次离线进程基准：`import app.main` 平均约 116 ms（此前约 1.17 s），`--help` 平均约 126 ms，临时新会话启动并立即 `exit` 平均约 316 ms。这些数值早于启动服务检查，不能作为新版交互启动基准；新版仅保留导入/帮助/列举会话不加载 OpenAI SDK，以及检查中退出不等待网络的保证。

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是 | 无 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 否 | `deepseek-v4-flash` |
| `DEEPSEEK_TIMEOUT_SECONDS` | 否 | `180`；允许 `10..600` |
| `DEEPSEEK_MAX_RETRIES` | 否 | `2`；允许 `0..5` |
| `AGENT_DEFAULT_EXECUTION_BACKEND` | 否 | `local`；只影响未建立身份且未显式选择后端的新会话，可设为 `aliyun-sandbox` |
| `BRAVE_SEARCH_API_KEY` | 仅使用 `web_search` 时 | 无；Brave Search 正式 API 的订阅密钥 |
| `RUN_DEEPSEEK_E2E` | 否 | 未设置；真实 E2E 默认跳过 |
| `ALIYUN_SANDBOX_PROBE_ENABLED` | 选择远端执行或运行探针时 | `false`；复用为 Agent 远端执行总开关，必须显式为 `true` |
| `ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS` | 否 | `false`；创建 Sandbox 时始终显式传入，仅业务必需时设为 `true` |
| `E2B_API_KEY` | 选择远端执行或运行探针时 | 无；阿里云 Agent Sandbox 极速版 API Key，不写入 metadata |
| `E2B_API_URL` / `E2B_DOMAIN` | 选择远端执行或运行探针时 | 无；必须组成同一个阿里云接入点 |
| `E2B_TEMPLATE` | 否 | 留空时使用 SDK 默认模板；非空时必须是阿里云侧已验证的模板名/ID |
| `ALIYUN_SANDBOX_TIMEOUT_SECONDS` | 否 | `300`；允许 `30..3600` |
| `ALIYUN_OSS_BUCKET` / `ALIYUN_OSS_ENDPOINT` | 选择远端执行或运行探针时 | 无；Endpoint 必须与 Sandbox 地域一致 |
| `ALIYUN_OSS_BUCKET_PATH` | 选择远端执行或运行探针时 | 无；必须是 RAM Policy 仅授权给本任务的非根绝对前缀 |
| `ALIYUN_OSS_MOUNT_DIR` | 否 | `/mnt/oss`；必须是 `/mnt/` 下的规范化绝对路径 |
| `ALIYUN_OSS_ROLE_ARN` | 选择远端执行或运行探针时 | 无；访问 OSS 的 RAM Role ARN，不使用长期 AK/SK metadata |
| `ALIYUN_OSS_READ_ONLY` | 否 | `false`；当前共享配置要求可写挂载，设置 `true` 会拒绝探针和远端执行启动 |
| `RUN_ALIYUN_SANDBOX_E2E` | 否 | `0`；真实测试还要求探针配置自身已显式启用，形成双重门控 |

系统环境变量优先于 `.env`。dotenv 插值关闭；URL 只接受 HTTP/HTTPS，拒绝凭据、查询参数、片段、空白和非法端口；模型名拒绝空白、控制字符和超长值。`Settings.api_key` 不进入 `repr`。超时和重试会同时在环境加载与直接构造 `Settings` 时校验。

### 阿里云 Sandbox + OSS 阶段 1 探针

`sandbox-probe` 不带参数时只打印“未执行”，不会读取 `.env`、导入 E2B SDK 或创建真实云资源。真实探针必须同时满足两个显式条件：配置 `ALIYUN_SANDBOX_PROBE_ENABLED=true`，并由操作者运行：

```powershell
sandbox-probe --execute
```

探针把 `fc.sandbox.storage.oss` 构造成 JSON **字符串**，并把 RAM Role ARN 放入 `fc.sandbox.auth.role`。阶段 1 只在挂载目录根部写入一个随机唯一 marker，严格回读比对后删除；阶段 2 在同一受限前缀下写入固定的有界文件集和严格 manifest，成功销毁 A 后创建 B，要求 B 读取的完整 manifest 与 A 生成的原始 manifest 相等，再逐文件验证 size/hash 并恢复到 B 的 `/workspace/restored-<token>`。任一路径结束时都会尝试 `kill()`；A 已销毁而 B 创建失败时最多创建一次清理 Sandbox C，并只用 Filesystem API 删除精确已知文件/目录。若清理未确认，错误只报告 `checkpoint_path` 可能残留，不回显第三方异常正文。当前不执行用户代码、不调用 DeepSeek、不接管 Agent 文件/命令工具。真实凭据只填写在未提交的 `.env` 或系统环境变量中，不要写入 `.env.example`、metadata、模板或工作区。

### CLI 与交互式 Plan

默认提示符采用克制的 Codex/Claude 风格，并保留实时流式输出。真实 TTY 的 act 主命令提示符由 Python 依赖 `prompt_toolkit==3.0.53` 提供命令菜单：每个空闲轮次输入 `/` 都会展示可用命令，继续输入可过滤，Tab/方向键可选择。普通 act 模型运行时保持的抢先读取也是一条完整主命令读取，带相同提示符与补全；若模型先结束，该读取由下一次空闲循环接管，不会遗留一个普通 `input` 吞掉下一条命令。Plan 的首次模型请求及每次选择后的后续请求都不打开 act 提示符或抢先读取；只有澄清到达后才完整显示问题与三个选项，并新建提示符为 `请选择 1、2 或 3：` 的专属无补全读取。该依赖仅在首次交互读取时延迟导入，`--help`、`--list-sessions` 和导入入口不加载它；依赖不可用、非 TTY 或 `agent --plain` 时可靠降级为普通 `input`，不输出补全 ANSI。所有读取仍由同一个 input broker 拥有，不存在第二个 stdin reader。

- `/plan [初始需求]`：启动一次交互式 Plan；普通输入始终按 act 工作，不存在持久 Plan 模式；
- `/status`：显示当前 mode、会话、模型、请求超时、重试和单用户轮工具 step 上限。
- `/permissions`：显示当前权限和风险；可在空闲主提示符降级到 `partial-control`，或经二次 `yes` 确认提权到 `full-access`。

`/plan` 先将用户目标交给模型生成结构化计划。为兼容 DeepSeek 的 OpenAI Chat Completions 接口，SDK 使用 `response_format={"type":"json_object"}`，再由本地严格 Pydantic 协议校验，而不假设供应商支持 `json_schema`。模型只能返回无未决问题的最终规划（`open_questions` 必须为空），或一个带恰好 3 个互斥选项的澄清问题；选项包含简短影响说明，最多一项可标记为推荐。终端回答被编码为数据追加给下一次模型请求，不解析为命令；这是跨模型请求的结构化暂停/继续，不是向同一次生成中途注入输入。最多 5 轮澄清，重复 ID 或重复问题会立即失败；不合规 JSON/最终计划最多进行 2 次有界协议纠正，仍失败即停止。真实 TTY 与 `--plain`/非 TTY 均通过专属选择读取接受数字 `1`/`2`/`3`；选择期间 Ctrl+C、EOF、`exit` 或 `/interrupt` 会安全取消 Plan。旧 `/mode plan`、`/mode act` 只提示已移除，不切换状态，也不会作为普通请求发送给模型。

整个规划过程在内存中进行，不发送 `tools`/`tool_choice`；兼容服务若仍返回工具调用会作为协议错误拒绝。只有完整最终规划通过验证后，才将 6 个完整 turn 事件经整批预检提交；取消、协议错误、重复问题或轮次超限均不写残缺 Plan 事件。最终 Markdown 由已验证的 JSON 确定性渲染并按章节分块流式显示：第一块在校验和提交后立即交给 UI，不加人为延迟；所有块拼接必须严格等于最终返回值和持久化 assistant 正文。这不是原始 JSON 或供应商 raw chunk 的直接输出，无效结构和半成品不会泄露到终端。原始模型 chunk 仍只作为 stream 快照保存，严格回放后的渲染文本必须与 assistant 正文一致。整个实现只有 Python，不使用浏览器、HTML、HTTP 服务或其它语言。

### Ubuntu / POSIX 状态

实现已包含 POSIX 文件锁、目录句柄、`rlimit`、独立进程组/`killpg`、`.venv/bin/python` 和 `agent.sh`，路径保留名检查也不依赖新版本 Python 才有的 `ntpath.isreserved`。因此代码按 Windows 与 Ubuntu/POSIX 双平台设计。**本轮 377 项基线只在 Windows 主机执行，没有在真实 Ubuntu 环境运行**；Ubuntu 可用性仍应由 Linux CI 或真实主机确认，不能把设计分支等同于已实测兼容。

## 统一工具权限策略

### 两种宿主权限模式

- `partial-control`（默认）：普通项目文件的 `write_file`、`editor_file`、删除和清空不弹出二次确认；`.env*`、密钥/证书、`AGENTS.md`、`code.md`、`skills/`、`.agent/`、`.git/`、`.venv/`、会话/候选/日志等内置或策略配置的敏感路径必须人工批准。写入和编辑显示一次绑定候选内容的 diff 审批，删除和清空显示一次操作预览审批。项目外绝对路径继续拒绝。
- `full-access`：对本机普通文件允许项目外绝对路径，文件修改不调用人工审批回调；当前已启用的 `web_search`/`web_fetch` 也不需要人工批准。该模式仍保留参数 schema、路径词法检查、链接/重解析点/多重硬链接防护、网络 SSRF/DNS/TLS、超时和响应大小边界，也没有开放任意 shell。它是高风险可信宿主模式，不等于关闭所有数据边界。

两种模式都由 `CodingAgent` 显式传入 Registry，并通过线程上下文传播。可信宿主 setter 使用非阻塞运行锁拒绝活动 turn 内的切换，因此已启动的工具保持原权限快照。模型不能在工具 arguments 中携带 `permission_mode`；多余字段会被严格 schema 拒绝。外置策略新增的受保护路径会自动进入 `partial-control` 审批，而不是被静默当成普通文件。

`.agent/tool_policy.json` 是所有工具共同使用的外置白名单：

- `allowed_path_patterns` 决定工具可接触的项目相对路径；当前为 `*` 与 `**/*`，即允许普通项目文件，再叠加下面不可绕过的保护规则；
- `.env`、任意大小写形式、`.env.*`、`.agent/`、`.git/`、`.venv/` 以及配置列出的缓存目录会被拒绝或从扫描结果隐藏；
- `data/pending`、`data/sessions`、`code.md` 和 `skills/` 以规范化项目相对路径精确保护；同名但位于其它目录的文件和相似前缀不会被误伤；
- `.agent/tool_policy.json` 所在目录受代码内置保护，不能通过修改策略 JSON 将其解除；
- 策略路径固定，不接受模型传入其它策略文件；
- 策略缺失、格式错误、超大、为链接/重解析点、具有多重硬链接或读取期间变化时，所有工具 fail closed；
- 策略 schema 当前固定为 v5，`network` 必须显式提供 `fetch_enabled` 与 `search_enabled` 及各自上限；旧版、缺字段或额外字段都整体拒绝，不能因为升级而静默获得公网权限；
- 每次工具操作重新加载并验证策略，避免长期缓存旧权限；
- 所有直接路径先做词法检查，再做白名单检查，最后才解析或打开文件；
- 所有文件读取和写入都拒绝软链接、junction、reparse point 及多重硬链接；`read_file` 已补齐多重硬链接拒绝；
- `list_files` 与 `grep` 对每个目录项应用相同策略，明确把 `.venv` 或 `.git` 写进 glob 也不会进入；
- `bash_shell.paths` 的每个操作数都单独校验，不能用第二个参数夹带越界或敏感路径。

Git ignore 仅用于防误提交，真正的 Agent 工具权限由上述策略执行。

## `bash_shell` 的真实语义

`bash_shell` 是对模型暴露的工具名，不代表系统中存在 Bash。实现使用 `shell=False` 和结构化参数：

```json
{
  "command": "compileall",
  "paths": ["app", "tool", "tests"],
  "timeout_seconds": 30
}
```

它不接受自由命令行、任意 argv、`python -c`、任意脚本、管道、重定向、自定义工作目录或环境变量。

| command | 行为 | `partial-control` 人工确认 |
| --- | --- | --- |
| `unittest` | 固定执行 `python -m unittest discover -s <单一目录> -p test*.py -v` | 是；测试代码会执行 |
| `compileall` | 固定执行 `python -m compileall -q <路径...>` | 否 |
| `validate_json` | 对每个 `.json` 固定执行 `python -m json.tool <文件>` | 否 |
| `remove_file` | 删除一个已验证的普通单链接文件 | 仅敏感路径 |
| `clear_file` | 把一个已验证的普通单链接文件截断为 0 字节 | 仅敏感路径 |
| `clear_directory` | 递归删除一个目录内的普通单链接文件和目录，保留目标目录 | 是；目录子项在审批前不能完全确定 |
| `python` | 把 `code` 委托给 Code Interpreter；不接受路径、argv、脚本或 `-c` | 否；由能力沙箱约束 |

该表完整描述 local 路径。remote 当前只开放前三个验证命令，并在 foreground 完成白名单、路径、权限模式和必要确认后签发精确一次性 capability；runner 固定使用 workspace/OSS 外的可信 venv 和非特权用户。remote 不开放 `remove_file`、`clear_file`、`clear_directory` 或 bash `python`，Code Interpreter 走独立的远端 capability 路径。

`full-access` 下上述命令都不调用人工确认，但仍受结构化命令、参数和执行边界限制。命令开关、最大路径数和基础 `requires_confirmation` 由 `.agent/tool_policy.json` 控制，最终是否询问由可信宿主权限路由统一决定。需要确认时：

1. 工具先校验命令、全部路径、链接和目标状态；
2. Registry 或文件审批状态机将请求交给 `CodingAgent` 注入的回调；
3. CLI 显示命令、相对路径和影响说明；
4. 只有用户明确输入 `yes` 才批准；没有回调、其它输入、回调返回非 `True` 或回调异常都拒绝；
5. 执行前再次验证文件或目录快照，目标变化则取消。

local 的 `unittest`、`compileall`、`validate_json` 使用项目 `.venv` 的 Python，在系统临时目录中的一次性项目副本执行。副本不会包含 `.env`、`.env.*`、`.git`、`.venv`、缓存、链接或多重硬链接文件，且子进程环境不会传入 DeepSeek 密钥或 E2E 开关。remote 则直接在 Sandbox active workspace 执行同样的结构化参数，不建立宿主副本，也不会 fallback 到宿主。两条路径的 stdout/stderr 均有界；超时会终止并返回净化错误。

重要信任边界：临时副本不是操作系统沙箱。`unittest` 会执行仓库中的测试代码，恶意或不可信测试仍可能主动访问宿主系统、网络或已知绝对路径，因此该命令默认要求用户确认。不要批准运行来源不可信的测试代码。

## Code Interpreter 的真实安全边界

`code_interpreter` 只接受以下结构，不接受路径、模块、环境变量或命令行参数：

```json
{
  "code": "values = [2, 3, 5]\nstatistics.mean(values)",
  "timeout_seconds": 5
}
```

- 父进程先执行严格 AST、名称和调用白名单校验；`import`、`open`、`eval`、`exec`、`compile`、`__import__`、dunder 反射、任意属性、文件、网络、系统命令和子进程能力会在创建执行进程前被拒绝；
- 子进程再次执行相同能力校验，只暴露少量纯计算 builtins 以及经过枚举的 `math`、`statistics` 函数；
- local 固定使用项目 `.venv` Python，`shell=False`，在一次性空临时目录运行；不会转发 `PATH`、DeepSeek Key 或 E2E 开关；
- 默认超时 5 秒，策略上限 10 秒；AST 最多 512 节点；stdout 和最终表达式结果均有上限；
- Windows 使用父进程 Job Object 限制整个作业内存、单进程内存和进程数，runner 不导入 POSIX `resource`；POSIX 父进程保留 preexec rlimit，runner 还会在 compile/exec 前用当前值与目标值中更严的一个二次设置 `RLIMIT_AS` / `RLIMIT_CPU` / 可用时的 `RLIMIT_NPROC` / `RLIMIT_FSIZE`；任何解析、导入、读取或设置限制失败都 fail closed 且不执行用户代码；
- local 的 `bash_shell(command="python")` 只构造 `CodeInterpreterArguments` 并调用同一实现，因此不能绕过 Code Interpreter 白名单；remote 不开放该 bash 子命令。

remote Code Interpreter 沿用宿主 foreground AST/资源策略校验，并在远端工具组消费一次性 policy capability 后再次加载最新策略。实际 runner 只使用已验证的绝对 venv 解释器，以固定非特权用户在唯一 invocation 目录启动；每次派发前重新复核 runtime realpath、版本、root owner/mode、解释器 SHA-256 和 metadata digest。provider dispatch 一旦已尝试而结果或 handle 不确定，runtime 会立即 fence 并销毁该 Sandbox，不会改走 local。

重要信任边界：这是“能力受限的本机子进程沙箱”，不是 Docker、VM、Windows Sandbox 或 OpenAI 托管容器。当前机器没有可用 Docker/Podman，项目也使用 DeepSeek 兼容的 Chat Completions 接口；因此不能声称具备独立内核或虚拟机级隔离。对于真正不可信的任意 Python 代码，仍应改用容器/微虚拟机或远程托管沙箱。本实现的设计是拒绝任意代码能力，只执行受限纯计算。

## Skill 渐进式加载

项目 Skill 固定放在根目录 `skills/<kebab-case-name>/SKILL.md`。Agent 运行时采用三段式披露：

1. 构造 `CodingAgent` 时只读取每份 `SKILL.md` 的 front matter 到闭合分隔线，获得严格的 `name` 与单行 `description`；正文和 supporting resource 内容此时不读取；
2. 目录以临时 user 级消息插入每次 SDK 请求的 system 消息之后，不写入 `_messages`、SessionEvent 或恢复投影，Plan 请求也能看见同一目录但不能调用工具；
3. 模型确认用途匹配后调用 `load_skill` 读取完整清单；只有清单明确需要某个支持文件时，才能在**后续 model step** 携带返回的随机 `skill_token` 调用 `read_skill_resource`，按 UTF-8 字节分页读取。

`skill_token` 只存在于当前 `SkillRuntime`，并绑定 Skill 名称、加载步骤和已验证文件树快照；跨 Skill、跨 Agent/会话运行时、同一步猜测使用、Skill 或资源变化、同一 Skill 重新加载后的旧 token 都会被拒绝。分页只在 UTF-8 字符边界结束，落在多字节字符内部的 offset 会拒绝，`scripts/` 也只返回文本而不执行。

发现和加载均 fail closed：目录名与 front matter 名称必须完全一致且只有一个大小写不敏感的 `SKILL.md`；拒绝未知根条目、控制字符、非法 UTF-8、软链接、junction/reparse point、多重硬链接和检查期间变化。根条目最多 66 个，每个 Skill 最多 256 个目录、256 个支持文件、8 层深度，清单最多 64 KiB，单资源最多 1 MiB，总字节最多 4 MiB。`_template` 是唯一忽略目录。`skills/` 同时进入代码内固有保护和 v5 policy；在默认 `partial-control` 下，普通读取、搜索和验证型 shell 看不到它，修改型工具则必须经过敏感路径人工审批。`full-access` 保留项目既有的可信宿主语义，不在本功能中另行改变。

Skill 内容按不可信 user 级输入处理，不能覆盖 system prompt、工具 schema、权限模式、恢复闸门或用户审批。新增 Skill 后需要重启 Agent 才会更新启动目录；具体格式见 `skills/README.md`。

## 受控公网访问

网络没有被整体关闭，但也没有交给任意模型 Python。`code_interpreter` 继续禁止 import、socket、文件与子进程；搜索和读取分别通过独立的 `web_search`、`web_fetch` 工具完成。

`web_search` 只连接固定的 Brave Web Search 正式 API：

- endpoint 固定为 `https://api.search.brave.com/res/v1/web/search`，模型不能选择 host、path、header 或密钥；只使用 `X-Subscription-Token` 认证头，不把密钥放进查询串、模型结果、日志或错误；
- query 最多 600 字符和 75 个词，count 在 1..10；freshness、country、search_lang 和 safesearch 都由严格 schema 约束；
- Brave host 的全部 DNS 结果必须是公网地址，实际 socket 固定到验证后的 IP，TLS 仍校验原 hostname；不跟随重定向，只接收未压缩、受策略字节/超时上限约束的 JSON；
- 只解析 `web.results` 的 title、url、description，输出严格映射为 title、url、snippet；不访问结果页面。结果 URL 只作为数据，后续读取必须重新通过 `web_fetch` 的 SSRF 边界；
- 标题、摘要和 URL 全部带有固定“不可信外部数据”标记，不能作为系统/用户命令、工具授权或执行请求。Observability 只记录 query 长度、筛选项有效性和结果数量，不记录 query、结果或密钥；
- `BRAVE_SEARCH_API_KEY` 从系统环境优先、项目 `.env` 次之安全读取；未配置、认证失败、限流、网络或响应格式异常都返回净化错误。本轮没有调用真实 Brave API；使用该正式 API 需要用户自行申请密钥，并可能受 Brave 套餐、额度和计费约束。

`web_fetch` 的约束如下：

- 只允许无凭据、无 fragment、纯 ASCII 且不含空白/控制字符的 HTTP/HTTPS URL；只执行 GET，不接受自定义 header、Cookie、认证或请求正文；
- 拒绝 IP literal；DNS 返回的**每一个**地址都必须是全局公网地址，任一内网、环回、链路本地、保留或混合结果都会整体拒绝；
- 连接固定到已验证 IP，HTTPS 仍以原 hostname 做 SNI 和证书验证，避免 DNS 重绑定绕过；
- 每个重定向都重新验证 URL、重新解析全部 DNS 结果并新建连接；不读取旧重定向响应正文；
- 只接收未压缩的文本、JSON 或 XML，默认最多 256 KiB，策略最多 1 MiB、20 秒、3 跳；返回内容显式标记为“不可信外部数据”，不得作为系统/用户指令、工具授权或执行请求；
- 策略、解析、网络、TLS、字符集或返回校验失败都输出净化错误，不回显底层 URL/异常秘密。

两个网络工具都经统一 Registry、Pydantic schema、v5 policy、输出相关性复核、retry-safety 分类与脱敏 Observability；所有外部数据只作为 tool output 进入对话，不会拼接进 system/user role。这解决了 Agent 的公网搜索/读取需求，同时避免任意脚本探测本机服务、云元数据、内网管理面或外传宿主凭据。

## 文件变更审批状态机

- `FileApprovalState` 的默认状态是 `unapproved`，适用于首次出现的每个规范化项目相对路径；
- 宿主未显式传入权限模式的旧嵌入调用仍保留原两阶段兼容语义；CLI 和 `CodingAgent` 始终显式传入当前可信宿主模式；
- `partial-control` 对敏感删除/清空执行一次操作预览审批；普通文件不询问。`clear_directory` 因子项未知继续询问一次；`full-access` 全部不询问；
- `partial-control` 的敏感 `editor_file`/`write_file` 跳过冗余的“是否准备修改”提示，生成候选后只显示一次 unified diff 审批；普通路径和 `full-access` 不显示人工审批；
- 候选完整字节以随机 UUID 文件名保存到固定 `data/pending/*.bin`；同名 `.json` 是严格、最小化清单，只保存候选 UUID、规范化目标路径、来源操作、编码、大小/哈希及 UTC 有效期，不保存正文、diff 或模型消息。该子树同时由代码内底线和 `.agent/tool_policy.json` 的精确 `protected_relative_paths` 保护，普通 Agent 文件工具不能读取、扫描、修改、删除或复制到测试副本；
- 候选建立后，状态绑定同一 `request_id`、工具、操作、规范化路径、候选 UUID 和 diff SHA-256，并转换为 `diff_pending`。CLI 只显示目标与候选位置，并明确提示 diff 正文未在终端展示；只有第二次明确输入 `yes`（回调返回布尔值 `True`）才进入 `diff_approved` 并提交正式文件；
- 第二次输入 `no`、其它值、回调异常、候选变化或正式目标变化都会拒绝提交，正式目标保持原状态，完整候选保留在 `data/pending`。成功提交并验证后才尽力删除对应候选；
- 用户之后要求确认以前拒绝的修改时，Agent 先调用 `list_pending_changes` 检索通过清单与候选双重校验的条目，再用精确 UUID 调用 `apply_pending_change`。CLI 显示目标和候选位置，不打印 diff 正文；本次只需一次明确 `yes`，并在写入前复核正式目标仍是生成候选时的状态。该流程直接复用原 `.bin`，不会创建或改写另一个候选；输入 `no` 或执行失败仍保留原候选；
- 候选和清单有效期为创建后 7×24 小时；清理只在新建、列出或应用候选时触发，不是后台定时任务。没有候选相关操作时，过期文件会等到下一次触发才删除。清理沿用候选锁，只处理严格 UUID 命名且仍为普通单链接文件的条目；
- 安全迁移规则：升级前没有严格清单的旧 UUID `.bin` 缺少目标与原始状态元数据，不能猜测目标、伪造清单或安全恢复，只会作为 orphan 按文件修改时间在满 7 天后清理；
- 内部 diff 预览最多 64 KiB、1,000 行，超出部分只截断预览，候选文件不截断；预览仍参与审批哈希绑定并会转义 C0、DEL/C1 及 Unicode Cc/Cf 控制字符，但 CLI 不向终端打印 diff 正文；
- 字符串 `"yes"` 直接作为回调返回值、缺少回调或任一阶段拒绝都不会批准；
- 批准精确绑定工具名、操作和全部规范化路径，提交前再次检查文件身份、原内容哈希或新文件路径；目标变化则取消；
- 无论成功、拒绝还是异常，调用结束都恢复为 `unapproved`，批准不可复用，也不能授权另一个路径或操作；
- 状态机只授权工具继续执行；底层文件白名单、敏感路径、链接、硬链接和原子写入检查仍然生效。

`editor_file` 保留兼容的 `replace_text`，同时提供 `replace_lines`、`insert_lines` 和 `delete_lines`。行操作不再接收行号，而是用 `old_text` 的完整一行或连续行块定位；插入使用 `position=before|after`。`expected_replacements` 对全部操作精确校验，多匹配可显式批准，重叠匹配会拒绝。请求中的 LF/CRLF 会自动转换为目标文件最常见（并列时最先出现）的换行样式，`new_text` 不必手工补末尾换行。所有模式都保留原 UTF-8 BOM 和未修改行的原始换行，正确处理文件末尾没有换行的情况，并在无变化、匹配数不符、重叠、参数混用或审批失效时拒绝写入。

`code.md` 只允许由 Agent 外部的宿主编辑器修改。默认加载器使用代码位置计算固定绝对路径，在每次新建 `CodingAgent` 时读取最新内容，并严格拒绝空白/NUL、非法 UTF-8、超过 256 KiB、软链接、Windows 重解析点、多重硬链接和读取期间身份或内容元数据变化。嵌入方仍可显式传入 `system_prompt` 覆盖文件。`code.md` 同时列入 `.agent/tool_policy.json` 的 `protected_relative_paths` 和代码内不可放宽底线，因此 Agent 的 `read_file`、`editor_file`、`write_file`、扫描工具及 `bash_shell` 均不能访问它。

`write_file(mode="create")` 可以创建缺失的多级父目录，但第一次审批前只做词法、白名单和已有路径组件检查；第一次批准后仅创建内部受保护的候选目录/文件，正式目标的缺失父目录仍不会创建。第二次批准后才逐层创建普通目录并独占创建正式文件。失败时只反向移除本次创建且仍为空、身份未变的正式目标目录。`overwrite` 仍不会创建缺失文件或父目录。

## 工具可观测性日志

每次工具执行生成两个标识：`tool_uuid` 是按已注册工具名计算的稳定 UUIDv5，`invocation_uuid` 是每次调用重新生成的 UUIDv4。一次调用的 `tool_call` 与 `tool_result` 事件共享相同标识，便于关联；不同调用不能复用 `invocation_uuid`。

日志不再区分显式/隐式模式，也不读取 `OBSERVABILITY_MODE`。所有事件只写入 `data/log/tool-calls-YYYY-MM-DD.json`；同一本地自然日从无编号文件开始，每个分片最多 2,000 条且最多 8 MiB，合法分片达到任一阈值后才续写 `tool-calls-YYYY-MM-DD-2.json`、`-3.json` 等文件。每个成功写入的分片始终是合法 JSON array，次日重新从无编号文件开始，终端不会打印可观测性日志。目录或文件状态不安全、已有分片 JSON 损坏、创建/原子替换失败或磁盘不可写时，原状态不会被修改，本次事件停止落盘并进入有界内存队列，不会通过旁路到新编号掩盖损坏，也不会改变工具执行结果。

Observability 构造不访问文件系统。临时写入失败时，当前事件和尚未写入事件会按原顺序保留在最多 512 条的线程安全内存队列；后续事件、每小时维护和进程正常退出会重试，部分成功只移除成功前缀。队列满时保留更早事件并丢弃最新事件，通过不含正文和底层错误的 `pending_count`、`dropped_count`、`healthy`/`health` 暴露状态。保留窗口固定为今天和之前 6 天，写日志及后台维护都会清理更早日期的所有安全分片。升级前的固定 `tool_calls.jsonl` 只按本地修改日期进入同一保留窗口，到期后安全删除，不再写入。

日志只记录固定安全元数据，例如 UTC 时间、事件类型、工具名、状态、数量、长度、超时、退出码和是否截断。以下数据永不记录：

- API Key、环境变量、模型消息和模型提供的 `tool_call_id`；
- 原始或规范化文件路径、文件内容、写入内容、哈希；
- Code Interpreter 代码和 Bash Python 代码；
- grep 搜索词、glob、匹配文本；
- stdout、stderr、最终表达式结果和错误正文。
- diff 预览、候选 UUID、候选文件相对路径和候选内容。

未注册工具统一记为 `unregistered`，未校验枚举和标量统一记为 `invalid`，避免模型把秘密夹带到伪造字段中。日志导入和 logger 构造无文件副作用；每次更新先验证旧文件、写入同目录独占临时文件，再原子替换。记录和维护失败均采用 fail-soft，不改变原工具结果，也不会把底层错误打印到终端。轮转只处理固定命名的日志文件；非匹配文件、链接、Windows 重解析点、特殊文件和多重硬链接不会被删除或写入。目录已加入 `.gitignore`。

## SessionEvent 会话事实源

本项目使用 DeepSeek Harness 的追加事件与消息投影设计，在 Python 中实现，未引入其 TypeScript 运行时，也不声称与上游磁盘格式互通。完整会话存放于 `data/sessions/<name>`，无参数启动仍使用 `current`。现有 `data/log` 仍是独立脱敏可观测日志；只有后者使用 fail-soft、每日分片和 7 天保留策略。

每个已提交事件保存为一个连续序号 JSON 文件，持久信封包含 `version`、`session_id`、`seq`、`time`、`type` 和严格 `data`。待追加载荷先生成无损 JSON 快照并校验；可靠提交后才向内存发布冻结对象。字典/列表形式的任意 JSON 子树保存为规范 JSON 字符串，事件容器使用冻结模型和 tuple，防止调用方修改历史。无效结构、不支持的数据或不可靠存储均拒绝，不能静默丢事件后继续执行工具。

所有 22 种 v1 事件还提供统一、冻结的派生视图 `SessionEvent.structured_view`，固定包含 `source`（事实来源）、`channel`（lifecycle/conversation/model_stream/tool/recovery/background_task/host/urgent/control/summary）、`content`（主要内容）和规范 `context_json`（turn、step、call、task、状态等背景关联）。`SessionEventWorkflow.structured_events` 可批量读取这些视图。派生字段不写入原始事件，也不参与 canonical JSON，因此旧 v1 会话文件可以按原字节继续验证。

`host/normal` 与 `host/urgent` 的 v1 载荷字段形状相同，回放时现在由外层 `type` 先确定具体 data model；不向旧事件增加判别字段，重新序列化后的 v1 canonical JSON 字节保持不变。这已修复包含紧急宿主事件的中断会话无法重新打开的问题。

事件包含轮/步骤起止、用户消息、带完整有序 stream 快照的模型回答、失败/中断回答尝试、顶层工具调用、工具结果、后台任务生命周期、普通/紧急/控制宿主事件、恢复结论与摘要。恢复时验证连续序号和跨事件状态关联，并从事件投影模型上下文。系统提示词按当前 `code.md` 加载；历史事件不会替用户保留一个永远不变的旧角色设置。

模型上下文投影遵守 Chat Completions 的工具消息邻接约束：带 `tool_calls` 的 assistant 消息后，必须先连续投影该组全部对应 `tool` 消息。持久事件中夹在结果之间的 normal/urgent/control 宿主消息、后台任务终态和恢复结论不会被删除或改写，而是在投影层按原相对顺序暂存，等工具组闭合后再释放。真实用户消息、下一条 assistant、步骤/轮边界以及不匹配或重复的结果不会被静默重排，而是 fail closed。实时提交允许最后一个尚未闭合且步骤仍开放的工具组，投影暂时退回最后一个完整 SDK 前缀；结果齐全后再原子呈现整组。

`assistant/message` 在一次回答组装完成时提交，包含回答及所有已接收的 JSON 数据块。网络/协议错误或 Ctrl+C 被捕获时，已接收数据块通过 `assistant/attempt` 追加保存，但不投影成完整模型回答；进程被强制终止或断电时，尚在内存且来不及提交的数据仍不能恢复。恢复会重新投影已经可靠提交的完整历史和中断前原用户消息，并追加一条不含原始 chunk 的 `host/normal(source=recovery)` 事实，明确 `partial_stream_replayed=false`。恢复后不会执行或重放半截工具参数。

恢复不是“只保留最新一次中断”。磁盘中的全部可靠 SessionEvent 都继续存在，恢复会验证并重新投影完整的已提交历史；启动时只对**当前未闭合的尾部**补恢复事件，因为更早轮已经闭合。中断生成的 partial stream 保留在 `assistant/attempt` 供审计，但不会冒充完整 assistant 回答进入模型上下文。尚未可靠提交到磁盘的内存 chunk 无法在断电/强杀后恢复。

会话本身没有用户轮次总上限。默认 `max_tool_rounds=8` 只限制一个用户 turn 内“含工具调用的 assistant step”数量，防止模型无限工具循环；它不限制会话总轮数，也不等于第 8 轮后强制关闭。突然中断更常见的来源是模型请求超时/连接或服务端错误、上下文超过供应商限制、Ctrl+C/`/interrupt`、宿主终止进程、断电，或会话/磁盘可靠提交失败。`/status` 可查看当前请求超时和该单轮工具上限；分类后的错误不会回显 SDK 原始异常正文。

摘要通过宿主的 `compact_history(summary=..., replace_start_seq=..., replace_end_seq=...)` 显式提交，只能替换完整结束轮的上下文投影。原始文件和事件编号保持不变。没有设置自动压缩阈值，也没有自动调用模型生成摘要；未来接入自动压缩时需要先明确上下文预算。

### 异步任务与事件队列

- 后台生命周期是只追加事件：`async/task/accepted`、`started`、`progress`、`completed|failed`；取消追加 `cancel_requested`、`cancelled`，逻辑取消后到达的执行事实只记为审计用 `late_result`，不会反转 `cancelled` 终态。
- `accepted` 事件先可靠提交，原始 `tool_call_id` 随后只写一个 `status="accepted"` 的占位工具结果；只有占位结果也可靠提交后才调用 `start`。这避免 worker 在模型协议闭合前越过执行边界。
- 完成或失败结果不再伪造同一 `tool_call_id` 的第二条 tool message，而是作为带 `[事件 #seq | type | task=ID]` 标记的宿主上下文投影给模型。普通用户、assistant 和 tool 原文不加标记。
- 普通事件在 32 条、256 KiB 或 100 ms 任一阈值触发时用 `append_batch` 连续提交；轮/步骤边界、工具调用、占位结果和任务 accepted/started/cancel 事件属于屏障，先排空更早的普通事件再立即可靠提交。
- `host/urgent` 在同一线性化锁内先作废当前模型 generation，再把已等待的普通事件和紧急事件一次性追加，且紧急事件位于批次末尾。旧 generation 的迟到 chunk 会被 generation fence 丢弃，不能组装成 `assistant/message`。
- Agent API 的 `interrupt()` 可由宿主线程在流式响应期间调用；CLI 以唯一输入 broker 避免命令和审批争用 stdin，流式期间可输入 `/interrupt 文本`，Ctrl+C 也会保存紧急事件并让模型基于新轨迹重新评估。`/tasks`、`/task ID`、`/cancel ID` 用于列出、查询和逻辑取消任务，模型侧有 `get_async_task`、`cancel_async_task` 两个会话绑定工具。
- `code_interpreter` 和 `bash_shell` 的 `unittest`、`compileall`、`validate_json` 默认后台执行；审批仍在前台完成。文件编辑、写入、删除、清空和候选应用保持同步，不能通过后台调度绕过确认或提交临界区。
- 同一步中多个符合后台条件的调用会依次可靠提交 accepted/占位结果后立即调度；默认 `AsyncTaskManager(max_workers=2)` 可让两个长任务真实并发，更多任务排队。普通同步读取和所有修改型工具仍按调用顺序串行处理。
- 可取消后台工具在接受阶段获得 sticky `ProcessCancellationHandle`：先取消后创建进程时不会丢失取消信号；Windows 将整个子进程树绑定到带 `KILL_ON_JOB_CLOSE` 的 Job Object，POSIX 使用 `start_new_session` 与 `killpg`。任务管理器始终先提交逻辑取消终态，再尽力物理终止；物理结果和迟到结果不能反转已保存的 `cancelled`。
- 后台工具的临时无工具回答用 `step/end(kind=awaiting_async)` 闭合；终态事件到达后在同一 user turn 开启下一 model step，不新增伪用户消息。快速任务若在模型流式回答期间完成也不会漏掉重评估；等待期间出现 `host/urgent` 时以 100 ms 有界等待唤醒并立即开始下一 step。
- 重启只恢复查询状态，不恢复 worker：只有 accepted 的任务补为 `not_started`；已有 started 而无终态补为 `outcome_unknown`；已有取消请求但缺少终态补为 `cancelled`。摘要不得只覆盖异步生命周期的一部分。

### 崩溃恢复与执行边界

- 模型消息中有调用但尚无顶层 `tool/call`：追加 `not_started` 工具结果，允许模型重新发起，仍需正常权限检查和审批。
- 顶层 `tool/call` 已提交但没有结果：追加 `outcome_unknown`。这表示已经跨入可能执行的调度边界，不等于证明外部修改成功，甚至可能在审批前就中断。
- 白名单内的读取/纯计算和隔离验证操作可以由模型重新发起；恢复过程本身不会自动执行工具。`unittest` 执行仓库代码，按非只读处理。
- 仍有未知修改结果时，所有其他非只读工具调度受阻。Agent 必须使用精确 `turn_id/step/call_id` 先调用 `inspect_recovery`，获得与会话、目标及观察绑定的运行期随机一次性 token；后续步骤再携带该 token、明确 `applied|not_applied` 和非空证据调用 `resolve_recovery`。确认前会重新做一次只读检查，状态改变、令牌/目标不匹配、无待确认项或重复使用均拒绝；`matches_requested` 只能提交 `applied`，`different` / `missing` 只能提交 `not_applied`，`unknown` 才允许结合可靠轨迹判断任一结论。矛盾提交同样消耗 token 并要求重新检查；此约束不把当前状态声称为过去执行证明。
- `resolve_recovery` 只追加原有 `recovery/resolution` 事件，不修改项目文件，旧调用绝不重放。轮内追加时，序列验证器要求当前打开步骤存在唯一、已落盘且尚无结果的匹配 resolver 调用；投影在 resolver 的 tool result 闭合后才释放恢复事件，保持 Chat Completions 消息顺序合法。若结论已落盘而 resolver 结果未落盘就崩溃，重启后旧调用仍保持已解决，恢复工具自身可安全重试。
- CLI `/recovery` 只保留只读诊断展示；`/resolve` 手工证据和 `yes` 流程已停用。修改后的参数或工具名称不能绕过恢复闸门。
- 所有修复均追加新事件，随后闭合中断的步骤和轮；不改写原工具结果，不重复使用历史审批。

会话目录加入代码内保护底线、外置策略和 `.gitignore`。完整会话包含用户正文、模型输出、代码、工具参数和结果，不能按脱敏日志看待；不写客户端 API Key、环境配置或请求认证头，但用户或工具正文中原本包含的敏感信息也会随完整会话保存。会话文件没有自动保留清理；不要手工编辑其中的事件文件。

## Agent 数据流和审批

1. CLI 先加载轻量配置、取得会话单写者锁、回放事件并显示交互界面；首个模型请求才延迟创建 SDK 客户端、Observability、审批回调和 Agent。运行时初始化成功后才启动脱敏日志维护服务；直接退出不会创建客户端。
2. `turn/start`、`user/message`、`step/start` 可靠提交后，将当前投影转换为 SDK 消息并请求模型。
3. SDK 请求固定使用 `stream=True` 和从 Registry 参数模型生成的 `TOOLSCHEMAS`。按顺序收集 chunk 快照，正文实时显示；`reasoning_content` 原样保存/回传，不向终端展示。
4. 严格重组、校验工具名称和 JSON object 参数；拒绝 NaN、Infinity、畸形索引及错误数据。完整回答与 stream 一起提交。
5. 恢复保护允许调度后，先可靠提交 `tool/call`。同步工具进入 Registry 的参数、权限、审批及执行流程；异步候选先完成参数/权限/前台审批，再提交 accepted 与唯一占位结果，最后启动 worker。
6. 权限路由先按本轮启动时的宿主模式和目标路径决定是否询问：`partial-control` 仅敏感写入/编辑做一次 diff 审批，敏感删除/清空做一次操作审批；`full-access` 不询问。候选恢复仍要求一次新的 diff 审批。CLI 不显示 diff 正文。
7. 同步工具结果通过对应 `tool_call_id` 投影；后台真实结果通过新的任务事件投影。事件队列的屏障确保副作用边界不被批处理延迟，Registry 继续独立记录脱敏统计。
8. 后台终态到达会在同一用户 turn 内触发下一 model step，以更新后的轨迹重新评估；同一步的多个调用全部闭合后才前进。紧急事件先取消旧 generation，或唤醒后台等待，旧流数据不能提交完整回答。达到 8 个“含工具调用的 assistant step”上限时，未执行调用得到 `not_started` 结果并结束为错误轮；同一步的多个 call 只计一轮。
9. 正常退出会等待后台任务、排空事件队列、刷新维护/日志并关闭客户端。活动请求期间输入 `exit`/`quit` 或连续两次 Ctrl+C 则进入有界紧急退出：立即设置 generation 与 Agent 失败栅栏，拒绝迟到模型、工具、异步任务和 SessionEvent 提交；会话锁优先释放，可能阻塞的 stream/client close、维护和日志刷新只在 daemon 清理线程中尽力完成，主线程不等待。候选清理不由维护线程触发。

## 测试状态

2026-10-03 阶段 4 真实模板初始化修复已完成离线验证：阿里云极速版模板初始没有 `/workspace` 时，transport 现在先对固定可信根执行 `make_dir("/workspace")`，再读取元数据确认它确实是普通目录；既有符号链接或特殊文件仍会 fail closed。远端 Linux command runner 使用独立 session，并在超时或输出上限触发时终止整个进程组；`killpg` 不可用或失败时仍直接终止子进程。新增用例覆盖缺失根创建后验证、根符号链接拒绝、超时强杀进程组、输出上限终止进程组、`killpg` 缺失和调用失败两种兜底。`tests.test_aliyun_sandbox_tools` **27 项全部通过**，新增 6 项定向用例全部通过；`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过。

主代理随后完成阶段 4 真实云最小工具组验证，**8 项检查全部通过**：同一远端 workspace 中成功写入并读取 3 个文件，grep 返回 1 个预期匹配，JSON 校验、`compileall` 和受限计算均成功；结束时 Sandbox 无条件 `kill` 成功。验证过程未写入 OSS。此前真实诊断的 `/workspace` 缺失创建失败已消除，阶段 4 至此完成。

阶段 5A 正在进行：已增加 session-scoped `ExecutionToolRuntime` 内部骨架，但**尚未接入 Registry、Agent、main/CLI 或环境变量选择**。默认 local runtime 只声明继续委托现有 Registry；Aliyun runtime 将 `read_file`、`write_file`、`editor_file`、`list_files`、`grep`、`bash_shell`、`code_interpreter` 固定为一个不可拆分的远端工具组，共享同一 backend 和 tool group，远端拒绝或失败不会回退本地。远端写入、编辑、命令和受限计算不再接受 `approval_granted: bool`，改为 runtime 内部即时签发、按对象身份验证的一次性 permit；permit 绑定 runtime identity/generation、工具名、规范 Pydantic 参数摘要、授权类型和随机 nonce，不可 JSON 化，错工具、改参数、跨 runtime、伪造、重放以及 close 后使用均拒绝。`bash_shell` permit 标记为 approval，`code_interpreter` permit 标记为 policy；当前仅提供 callback 返回严格 `True` 后立即签发并消费的内部衔接面，尚未实现远端 diff 审批。

Aliyun backend 的阻塞 transport 调用现已移到 Condition 锁外：锁内只验证 ready、取得 transport 并登记 active operation；`close()` 可原子切到 closing、拒绝新调用，并在旧调用仍阻塞时立即在锁外 `kill` Sandbox，最后进入不可重开的 closed。创建与关闭竞态、多个并发 close、kill 后旧调用正常返回或抛错，以及 `KeyboardInterrupt` / `SystemExit` 均保持明确语义且只清理一次。阶段 5A 定向回归 **56 项全部通过**；当时全量发现 **460 tests**，452 项通过、5 项按环境/显式开关跳过、3 项仍为工作区用户 Skill `skills/grilling` 导致的既有 Agent 消息数量断言失败。本阶段未调用真实云；5B1/5B2a 分别完成下面的持久身份绑定和 Registry 只读接缝，后续仍需单独评审 Agent 生命周期及特权操作接入。

2026-10-04 阶段 5B1 已完成：`SessionEventWorkflow` 新增 local/aliyun-sandbox 会话级绑定，默认及 `None` 均明确请求 local。新持久会话先以 no-replace 原子发布并同步 `execution.json`，再发布 `session.json`；若停在两次发布之间，匹配的显式后端可从 sidecar 恢复原 `session_id`，不匹配请求直接拒绝。已有 header 或事件但缺 sidecar 的旧会话永久迁移为 local，远端请求不能把它升级；绑定创建/读取失败会释放 writer lock，且不改写已有事件。新增 **14 项**定向测试覆盖新 local/remote、匹配重开、双向 mismatch、旧 header/事件迁移、sidecar-only 崩溃窗、无 header 事件拒绝、未知对象无副作用、规范 JSON/额外字段/session 篡改、大小门控、目录/链接/硬链接、并发 no-replace、预占竞态、故障锁释放和旧事件字节不变。会话相关定向回归 **87 项通过、2 项平台能力跳过**；全量发现 **474 tests**，466 项通过、5 项按环境/显式开关跳过、3 项仍是同一 `skills/grilling` 既有断言失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；未调用真实云。

2026-10-04 阶段 5B2a 已完成：`call_tool` 和 `prepare_async_tool_call` 新增默认 `None` 的 `execution_runtime` 参数，未传入或 local route 继续使用原 Registry。remote route 仅允许 `read_file`、`list_files`、`grep` 经 `ExecutionToolRuntime.execute`，远端返回会重新通过现有 Pydantic output model 和请求关联 validator；runtime 关闭、transport 异常及错型/恶意输出统一映射为固定安全错误，不回传底层异常且绝不本地 fallback。四个特权 workspace 工具在本阶段同步返回固定 denied，异步准备同样在任何本地路径/命令准备前拒绝。新增 **8 项**测试覆盖三只读成功路由、同一 runtime、本地 handler 不调用、关闭/失败/坏输出无 fallback、四特权拒绝、local/None 同步与异步兼容、非 workspace 不受影响，以及 observability 成功/失败均完成 finish。全量发现 **482 tests**，474 项通过、5 项跳过、3 项仍是同一 `skills/grilling` 既有断言失败；`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过。未接 Agent/main/CLI/env，未调用真实云。

2026-10-04 阶段 5B2b 已完成：Registry 与远端工具组共用不触盘的 POSIX 相对路径词法校验；remote `read_file` 的 `partial-control` 显式加载宿主 policy 并要求 allowed 且非 protected，`full-access` 仍限于 sandbox workspace 安全相对路径但可读 protected。远端 `list_files`/`grep` 每次操作加载宿主 policy，把区分目录/文件的 predicate 下推到 E2B transport；`.env`、`.agent/`、`skills/`、`data/pending/` 等在列举和正文读取前被排除，目录不会因窄 allowed glob 未直接命中而误剪枝，工具组对恶意 transport 返回的越界或 protected 路径再次拒绝。remote runtime 的 `list_pending_changes`/`apply_pending_change` 在任何宿主候选访问前固定 denied；local/`None` 行为保持。新增 **8 项**专门测试后，两个定向文件共 **43 项通过**，执行后端/Registry/读搜/权限/pending 相关回归 **100 项通过**；全量发现 **490 tests**，482 项通过、5 项按环境或显式开关跳过、3 项仍为同一 `skills/grilling` 既有 Agent 消息数量断言失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；未接 Agent/main/CLI/env，未调用真实云。

2026-10-04 阶段 5B2c 已完成：Registry 仅为同步 remote `code_interpreter` 增加授权执行路径。宿主 callback 用最新外置策略检查 enabled、timeout、max AST nodes 和 AST 能力白名单，全程不启动本机进程；`ExecutionToolRuntime.execute_authorized` 在 callback 严格返回 `True` 后签发与 runtime、generation、工具、规范参数摘要和 policy 类型绑定的一次性 permit，远端组先消费 permit，再用最新策略完成第二次相同校验。第二次策略的 `max_output_characters` 会独立下推到远端 runner payload，并再次限制模型可见 stdout/result；不再由硬编码 16384 绕过更低策略。策略或授权拒绝统一返回固定 denied，closed/transport/runner/恶意输出/validator 统一返回固定 error，均不泄露详情、不调用本地 handler、不中途 fallback，observability 对 success/denied/error 都完成 finish。remote async code、bash、write、editor 保持拒绝，local/`None` 保持原行为。两个定向文件 **49 项全部通过**；执行后端、runtime、Code Interpreter、Registry、权限和 bash 相关回归 **118 项全部通过**；全量发现 **496 tests**，488 项通过、5 项按环境或显式开关跳过、3 项仍为同一 `skills/grilling` 既有 Agent 消息数量断言失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；未接 Agent/main/CLI/env，未调用真实云。

2026-10-08 阶段 5B2c1 已完成：远端 Code Interpreter 工具组从第二次最新宿主策略取得 `max_output_characters` / `max_memory_mb` / `max_processes`，连同本次已受两次策略限制的 timeout 下推 transport；E2B transport 用 `code` 加这四个动态值构造严格 runner payload。协议选择不下推冗余 `max_timeout_seconds`，runner 仅接受实际 `timeout_seconds` 且严格限制为 1..10；多余字段会直接拒绝。runner 的 stdin 先以 `MAX_REQUEST_BYTES + 1` 有界读取，再拒绝重复 JSON 键、NaN、缺失/额外字段、bool、越界数值和超限 UTF-8。Linux/POSIX 在任何 compile/exec 前设置且绝不放宽既有 `RLIMIT_AS` / `RLIMIT_CPU` / 可用时的 `RLIMIT_NPROC` / `RLIMIT_FSIZE`；parse/import/getrlimit/setrlimit 任一失败都净化为固定错误且不运行代码。Windows runner 不导入 `resource`，父进程 Job Object 路径不变。资源限制测试只注入伪 `resource` 模块，没有降低测试主进程的真实 rlimit。runner 定向 **11 项全部通过**，执行后端/runtime/Code Interpreter/Registry/权限/bash 相关回归 **120 项全部通过**；全量发现 **507 tests**，499 项通过、5 项按环境或显式开关跳过、3 项仍为同一 `skills/grilling` 既有 Agent 消息数量断言失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；未接 Agent/main/CLI/env，未调用真实云。

2026-10-09 阶段 5B3a 已完成：`CodingAgent` 新增默认 `None` 的 session-scoped `execution_runtime` 注入，但未增加任何模型工具 schema、backend 选择参数或环境变量。local 会话只接受未关闭的 local runtime（或默认 `None`）；aliyun-sandbox 会话必须接受未关闭、backend 匹配且只读 `session_id` 与 `SessionEventWorkflow.session_id` 精确相同的 remote runtime。绑定检查只读内存身份，不调用 `ensure_ready`，构造期不创建 Sandbox；成功绑定后 runtime 由 Agent 接管，并传入同步 `call_tool` 与异步 `prepare_async_tool_call`。因此 remote Agent 当前只实际开放既有 `read_file` / `list_files` / `grep`，`code_interpreter` 保持 async 分类，和 async bash 一样在 Agent 准备阶段固定拒绝，不触发本机 runner 或远端 transport；Registry 的同步 remote code 接缝没有扩大到 Agent。

runtime 新增同步幂等 `begin_close()`：只在短锁内把 open 切到 closing、递增 generation 并清除 permit，不创建资源、不等待远端 kill，closing 也按 closed 拒绝后续操作和 Agent 绑定。普通 `agent.close()` 即使 runtime kill/close 报错也会继续尝试 tasks、dispatcher 与 session 清理，再重抛首个错误；紧急 `close(wait=False)` 在调用线程先同步安装 runtime fence，再保持 dispatcher→session 的既有 writer-boundary 优先顺序，之后才由 daemon 执行可能阻塞的 runtime kill 和 task shutdown。构造后续步骤失败时，已接管 runtime、tasks、dispatcher 和 Agent 自有 session 均清理；调用者提供的 session workflow 仍由调用者保留。新增 Agent/runtime 专项 **7 项**并扩展 runtime **1 项**，阶段定向/相关组 **59 项全部通过**，Agent/权限/事件相关回归除 3 个既有 grilling 断言外无新增失败；全量发现 **515 tests**，507 项通过、5 项跳过、3 项仍是同一 `skills/grilling` 既有失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；未修改 `app/main.py`、CLI 或 env，未调用真实云。

2026-10-09 阶段 5B3b 已完成：CLI 新增 `--execution-backend {local,aliyun-sandbox}`，默认 local。新会话把选择显式传入 `SessionEventWorkflow` 并持久绑定；resume 必须再次选择完全相同的后端，双向错配均在既有 sidecar 上 fail closed且字节不变。local 路径完全不加载 Sandbox 配置、不构造 execution runtime，也不向 `CodingAgent` 传额外 kw；remote 路径在创建新会话目录前通过 main 的 lazy wrapper 加载 `SandboxSettings` 并执行 `require_enabled()`，继续复用 `ALIYUN_SANDBOX_PROBE_ENABLED` 作为显式远端总开关，配置失败不留下会话目录。`--list-sessions`/`--help` 不加载模型或 Sandbox 配置，也不导入 Agent、OpenAI/E2B SDK、backend/runtime。

首次真实模型请求的 `ensure_runtime()` 才用已验证 settings、选择值和会话精确 `session_id` 构造 `AliyunSandboxExecutionBackend`/`ExecutionToolRuntime`，且只创建内存对象，不 `ensure_ready`、不导入或调用 E2B SDK、不创建云资源；构造成功后 runtime 交给 Agent 接管。runtime factory 的部分构造失败会自行关闭 backend，client/observer/Agent 任一步失败都会清理尚未接管或幂等 runtime、client、observer，并由 main 释放 session lock。remote 直接 `exit` 或空 `/plan` 取消只持久化会话后端绑定，不构造 runtime/client/cloud。新增 main/backend 专项 **9 项**；CLI/runtime/session 定向组 **68 项全部通过、1 项平台能力跳过**。全量发现 **524 tests**，516 项通过、5 项跳过、3 项仍是同一 `skills/grilling` 既有失败；`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过。本阶段没有调用真实云，remote Agent 仍只实际开放 read/list/grep，async code/bash 与 mutation 未开放。

2026-10-09 阶段 5B4a 已完成：新增 `app/workspace_seed.py`，在每个 CLI 进程首次 remote 模型请求组装 runtime 时捕获当前宿主允许文件的冻结 seed，但仍不创建云资源；同一 sandbox 生命周期固定使用这份 seed，恢复会话的新 CLI 进程会按当前宿主工作区重新捕获。snapshot 只接受唯一排序的规范 POSIX 路径和不可变 bytes，`repr` 不含正文；拒绝 truncated scan、链接/硬链接、scan/read 大小竞态、不完整读取、超过 1,000 文件、单文件超过 1 MiB 或总计超过 32 MiB。`data/`、`skills/`、`.agent/`、`.git/`、`.venv/`、缓存目录、`code.md` 及所有 `.env*` 由代码永久排除，policy 不能放宽；digest 只覆盖规范 path/size/sha256 manifest。

`AliyunSandboxExecutionBackend` 新增可选严格 callable initializer；transport 构造后、ready 发布前只把 typed `SandboxTransport` 与冻结 `ExecutionLease` 交给它，并发 `ensure_ready()` 只执行一次。initializer 要求固定 `/workspace/agnet-study/<session_id>` 初始无普通文件，逐文件 `overwrite=False` 写入并复核 `bytes_written`，再完整回读 size/hash，最后要求远端 path/size 清单与 seed 精确相同；任何普通异常都净化并 kill candidate，`KeyboardInterrupt`/`SystemExit` 也先清 candidate 再原样抛，后续调用只重抛既有失败而不重建。新增 **13 项**测试覆盖 snapshot 排序/digest/二进制/排除/上限/truncated/竞态/repr、uploader 空工作区与最终清单/恶意 metadata/部分失败、backend 顺序/并发一次/失败清理/中断，以及 main 惰性捕获和 fake Agent 实际读取 seed。远端相关定向 **108 项全部通过**；全量发现 **537 tests**，529 项通过、5 项跳过、3 项仍为同一 `skills/grilling` 既有断言失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；未调用真实云，未开放 mutation/bash/async code，且尚未实现 OSS checkpoint。

2026-10-10 阶段 5B4b 已完成离线实现：默认 E2B transport 现在同时绑定 lease 的 active workspace 与 persistence root，但 persistence 能力只接受 `<persistence_root>/.agnet-study/checkpoints/v1/<32hex session>/<64hex snapshot digest>` 及其后代；未绑定时 fail closed。挂载根必须已存在且是普通目录，namespace/文件父链只逐段创建缺失的普通目录；反斜杠、NUL、控制字符、`.`/`..`、越界、符号链接、特殊文件、超限文件、超深或超量列举都会拒绝。persistence 单文件上限为 8 MiB，以容纳最多 1,000 个长路径的 canonical manifest；seed 文件本身仍由 snapshot 严格限制为 1 MiB，列举深度固定为 64、条目固定为 20,000。

新增 `app/workspace_checkpoint.py`：manifest schema v1 严格绑定 `session_id`、宿主 `snapshot_digest`、`total_bytes` 与排序文件 path/size/sha256，JSON 拒绝重复键、NaN、float、bool 冒充 int、额外/缺失字段及非规范 bytes；OSS manifest 必须逐字节等于当前宿主 snapshot 重新生成的 canonical bytes，不能作为可信 digest 来源。初始化先要求 active workspace 无普通文件；无 manifest 的空或半成品 checkpoint 只允许存在当前 snapshot 的预期目录/文件，既有文件必须逐个正文、大小和哈希相等，缺失文件 create-only 写入并回读，manifest 最后发布；已有 manifest 则要求完整精确清单。随后再次从 checkpoint 读取已验证 bytes，create-only 恢复到 active workspace并回读，最终清单必须精确匹配。失败继续沿用 backend initializer 的 candidate kill 语义，错误不回显正文或第三方异常。

initializer factory 会在 main 首次 remote 模型请求构造 runtime 时用纯内存预检 manifest 大小、layout 条目数和 64 层深度，因此确定性失败发生在 backend/client/cloud 创建前；首次远端工具才实际创建 Sandbox、验证挂载并建立或恢复 baseline。每个新 CLI 进程仍重新捕获当前宿主 snapshot，digest 变化自然使用新内容寻址目录；旧目录保留，本阶段没有远端 mutation 同步、checkpoint 更新、sidecar/pointer、GC 或删除。新增 **14 项**测试（10 个 checkpoint、2 个 E2B persistence、1 个 backend root 绑定、1 个 main preflight）后，远端相关定向 **95 项全部通过**；全量发现 **551 tests**，543 项通过、5 项按环境/显式开关跳过、3 项仍为同一 `skills/grilling` 既有消息数量断言失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过；本阶段未调用真实云。

最后一次全量回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

2026-10-12 第一安全交付门定向回归覆盖 Sandbox 配置、backend/runtime、可信 venv/transport、Registry、local 进程取消和真实 Agent 接线，共 **123 项全部通过**。重点包括：缺失 venv 仅由 root 创建且不用 `--clear`、预置环境验证、root owner/no group-world write、非特权 runner、active workspace/OSS 排除、同路径解释器 realpath/SHA-256/metadata 篡改拒绝；foreground 精确授权、一用 invocation、同步/异步统一生命周期；cancel-before-bind、运行中取消、迟到结果、start-before-handle 普通异常及系统级中断、cancel/cleanup 不确定时 runtime fence + Sandbox kill；local 行为不变且 remote 无宿主 fallback。没有开放 write/editor，也没有运行真实云测试。

5C3/5C5 收口后的最终离线全量回归发现 **588 tests**：579 项通过、6 项按环境/显式开关跳过、3 项失败。3 个失败仍全部来自工作区当前额外存在的用户 Skill `skills/grilling`，而旧 `tests/test_agent.py` 用例硬编码假设默认没有项目 Skill，导致 SDK 消息中新增一条 Skill catalog；具体为 `test_direct_final_text_is_returned_and_history_is_kept`、`test_multiple_tools_execute_and_full_order_is_sent_back`、`test_multiple_user_turns_send_complete_history`。本轮没有删除用户 Skill，也没有为通过测试而篡改这些无关用例；未出现 5C3/5C5 产品代码新增失败。`.venv` 的 `compileall -q app tool tests` 与 `pip check` 均通过。

5C6 harness 替换旧 live 用例后，该文件现含 1 个默认 skip 的真实云用例和 1 个纯离线脚本/门控用例；定向结果为 **1 通过、1 跳过**，并确认未加载云配置或构造 backend。相关 Agent/runtime、remote mutation、workspace v1/v2 定向组共 **39 项，38 通过、1 个 live 跳过**，`compileall` 与 `pip check` 继续通过且未调用云。当前可信全量为 **589 tests：580 通过、6 跳过、3 个既有 `skills/grilling` 失败**，耗时 17.769 秒。宿主 `C:` 仍为 **0 bytes free**，这是后续依赖系统临时目录的测试和运行风险；本次通过仅为测试进程把 `TEMP` / `TMP` 定向到 F: 的现有 `data` 目录，没有清理或改写用户 Temp，也未再出现 `Errno 28`。

2026-10-13 首次为当前 5C6 harness 同时开启 live/probe/recovery 三门：运行已到达 Sandbox A，并发起过错误代码写入与首个异步 unittest 调用；旧 harness 在 stage 1 不检查 `a_write` 结果便进入 unittest，失败后也未到达结果断言，因此现有证据不能证明写入成功。首个异步 unittest 随后走到非-completed 状态，旧脚本只按 completed 数量推进，因此返回“等待第一次远端测试完成。”并使 Agent 误终止，B 未创建。`finally` 管理清理路径已执行；由于没有 provider 资源列表或等价外部证据，本记录不宣称 A 已成功 kill。随机 session 对应的 immutable baseline、objects 或 generation manifests 可能按 no-GC 策略保留。修复后 live 测试文件含 **4 个纯离线通过、1 个真实云默认跳过**；终态诊断只输出 call id、逻辑 status、result status 和固定错误类别，不复制 provider/tool 原始错误正文。

第二次真实运行在约 4.3 秒后再次只确认到达 Sandbox A，并进入写入与首个异步 unittest 调用路径；仍没有进入 `a_write`/异步结果断言，因此不能据此证明写入成功。该轮已识别异常终态，但 `_async_call_completed` 在 scripted client callback 内直接抛出的 AssertionError 被 `CodingAgent._request_model` 统一包装为脱敏 `AgentRequestError`，call id、status、result status 和固定 error class 仍未到达测试层，B 未创建。`finally` 清理路径再次执行，但没有 provider 资源列表或等价外部证据，不能宣称 Sandbox kill 成功；该随机 session 的 immutable OSS 数据也可能按 no-GC 策略保留。现已增加真实 `CodingAgent` 离线回归，证明 preparation denial 时 `agent.run()` 正常返回记录文本，随后测试层才抛出不含原始 secret 的固定诊断。

第三次真实运行在约 3.9 秒后由测试层安全报告 `call_id=a_test_fail status=failed result_status=error error_class=async_task_failed recovery_status=recorded`；远端 unittest 输出只证明它运行了 1 项测试并在 2.974 秒后进入失败终态，不能由此推断具体 provider/runner 错误或写入已持久化；Sandbox B 未创建。`finally` 清理路径已执行，但无 provider 列表或等价外部证据，不宣称 Sandbox kill 成功；该随机 session 的 immutable OSS 数据可能保留。

第四次真实运行在约 6.4 秒后报告 `call_id=a_write status=precondition_failed result_status=error error_class=tool_result_not_success`；本轮在约 5.078 秒的唯一 live 用例内结束，首个 unittest 没有启动，因此问题已前移到远端 `write_file` 的建租约/初始化/mutation transaction 路径，但现有固定错误边界尚不能区分它们。本次 `finally` 清理路径同样已执行，仍无外部证据可宣称 kill，immutable OSS 数据也可能保留。后续离线改动不读 `.env`、不调用真实云：在 `agent.run()` 返回后立即用 runtime 公开 `ensure_ready()` 判定 `backend_state=ready|create_failed`，并用仅测试级的固定 stage recorder 区分 lease create、trusted runtime、workspace initializer、mutation prepare 与 commit/v2。新增离线回归证明 failed backend 只重抛缓存错误而不第二次创建，且诊断不泄露 provider 类名、正文或 secret。

第五次真实运行在约 5.1 秒后报告 `call_id=a_write status=precondition_failed result_status=error error_class=tool_result_not_success backend_state=create_failed backend_stage=trusted_runtime exception_chain=ExecutionBackendCreateError`；唯一 live 用例用时 3.808 秒。该证据已确认 mutation prepare/commit、OSS workspace initializer 和 runner 均未执行，失败发生在 `E2BSandboxTransport` 的 trusted runtime 初始化内。本轮 `finally` 清理路径已执行，但仍无 provider 列表证据可宣称 kill，对应 immutable OSS 数据可能保留。后续 test-only recorder 不记录方法参数、command、path 或异常正文，只用固定方法入参分类与 ` -I -m venv ` 模式细分 `system_python_probe`、`runtime_path_check`、`venv_create`、`runtime_freeze`、`venv_python_probe` 与 `runtime_inspect`。`python3` 和 `python` 两个系统候选失败都统一记为 `system_python_probe`；Python 版本低于 3.11 、输出异常或候选不可用也不回显 builtin/provider 类型。

第六次真实运行报告 `backend_stage=runtime_path_check`，唯一 live 用例用时 3.196 秒。根因是 E2B 2.49.1 的 `get_info()` 对缺失路径抛 `e2b.exceptions.FileNotFoundException`，旧产品代码只捕获内置 `FileNotFoundError`。修复改为先调用 SDK `files.exists()` 并严格要求返回 `bool`：`False` 才进入 create 路径，`True` 后 `get_info()` 的任何异常都按 TOCTOU/provider 不一致 fail closed；未按 provider 异常类名匹配，也未引入顶层 E2B 导入。

第七次真实运行越过路径检查并报告 `backend_stage=runtime_inspect`，唯一 live 用例用时 8.311 秒。根因是 Linux venv 的解释器链接常为 root-owned mode `0777`，而旧检查对所有 `lstat` 项统一拒绝 `0o022`，与 `_freeze_runtime` 跳过 symlink chmod 的行为自相矛盾。修复仅允许 root 持有且 realpath 位于固定 runtime 内或等于已验证解释器 realpath 的链接；目标仍必须 root-owned、不可 group/world write 且为普通文件/目录，外部目标只能是已验证的普通解释器文件，外部解释器 stat 元数据纳入身份摘要。

第八次真实运行已完整越过 trusted runtime，报告 `backend_stage=workspace_initializer`，唯一 live 用例用时 17.202 秒。静态核对 E2B 2.49.1 后未找到足以安全修复产品的单一 SDK 差异，因此只增加 initializer 活跃期的固定脱敏子阶段：active list、baseline checkpoint list/read/write、active write/read、v2 version list/read；ready 后的普通 Agent I/O 不会覆盖诊断。

第九次真实运行报告 `backend_stage=initializer_version_list`，唯一 live 用例用时 15.870 秒。根因是 `WorkspaceVersionCoordinator` 请求 `MAX_VERSION_ENTRIES=22_000`，但 `E2BSandboxTransport` 的有界 persistence 列举上限为 `20_000`，所以任何真实 transport 都会在 provider I/O 前直接拒绝。修复把协调器上限收紧到 `20_000`，没有放宽 transport，并增加穿过 coordinator→真实 E2B transport 适配层的离线回归。

第十次真实运行于 2026-10-14 **通过**，唯一 live 用例用时 50.818 秒。Sandbox A 的真实 `CodingAgent` 成功完成 `write_file` 创建错误实现、异步 unittest 失败、`editor_file` 审批修复、异步 unittest 成功，以及 protected/escape/pending/绝对 OSS checkpoint 拒绝矩阵；随后相同 session 的不同 Sandbox B 通过 Agent list/read 看见 `return 42` 且不含 `return 41`，并再次通过远端 unittest。宿主 local Registry、shell 和 code runner mock 均未被调用，宿主指纹未变化，A/B runtime 均进入 closed，且两个非空 sandbox id 不同。由于仍没有 provider 资源列表或等价外部证据，本记录只确认清理路径执行和 runtime 关闭，不宣称 A/B 的物理 kill 已由外部确认；对应 immutable baseline、objects 与 manifests 按 no-GC 设计可能保留。

第十一次真实运行于 2026-10-14 **通过**，扩展后的同一 live 用例用时 64.739 秒，仍只创建 Sandbox A/B。partial-control 主闭环完成后，宿主只在 Agent 空闲边界调用 `set_permission_mode("full-access")`；真实 Agent 随后对合成远端 `AGENTS.md` 完成 create→replace edit→异步 unittest，外部 confirmation callback 计数在整个 full-access 阶段没有增加。不同 Sandbox B 的 list/read 精确看到 `state: after`，证明 full-access mutation 也进入同一 OSS v2 generation 链并成功恢复；宿主 `AGENTS.md` 已纳入指纹且未变化。本轮同样只确认 runtime close/清理路径执行，不把它解释成 provider 侧物理 kill 已获外部证明。

独立 5C4 运行中取消 live 用例于 2026-10-14 **通过**，用时 24.954 秒。第一次启用该专用门控时在创建 Sandbox 前被 `WorkspaceSeedSnapshot` 的非规范条目顺序本地拒绝；修复为规范字典序并增加直接调用生产 snapshot 校验器的离线回归后重新执行。真实用例用受控 `unittest` sleep 启动长任务，先确认 async running，再由 test-only 无数据 `Event` 确认 `PreparedRemoteExecution._bind_handle` 原实现已成功绑定真实远端 handle，随后真实 `CodingAgent` 调用 `cancel_async_task`。任务主终态固定为 cancelled 且主结果字段为空，worker 的迟到 error 只写入 `async/task/late_result` 审计，不产生 completed；同一 runtime/Sandbox 随后再次运行短 unittest 并成功，证明取消没有破坏后续结构化执行。宿主 local Registry、shell 和 code runner mock 未调用，宿主指纹未变化，两个 unittest 恰好各触发一次 partial-control confirmation，受管 cleanup 后 runtime closed。该结果仍没有 provider 资源列表或等价外部证据，因此只证明 handle 取消协议和清理路径可用，不宣称 Sandbox 物理销毁已获外部确认。

独立 5C4 结构化超时 live 用例也于 2026-10-14 **通过**，最终复验用时 22.237 秒。该用例以真实 `CodingAgent` 对合成长测试目录发起 `timeout_seconds=2` 的 unittest，要求 async/tool 协议正常完成但 `BashShellOutput` 精确为 `exit_code=1`、空 stdout、`stderr="命令执行超时。"`、无截断和附加 result；随后相同 Sandbox ID 的短 unittest 成功，runtime 在受管上下文内保持 open 并于退出后 closed，宿主执行 mock/指纹和两次审批断言均通过。首次真实运行用时 22.375 秒并安全失败为 `async_task_failed`，证实内部 runner timeout 与 provider command timeout 共用同一 deadline 时，外层可能在 runner 完成进程组 kill、输出排空和 JSON 返回前抢先终止。产品修复保持 payload 内实际执行上限不变，仅把 provider timeout/request_timeout 映射为请求值加固定 5 秒，且结构化命令最大只从 120 映射到 125、Code Interpreter 从 10 映射到 15；非法类型或越界值在派发前拒绝。修复后的同一 live 用例通过，说明收尾竞态已消除而未延长模型代码运行时限。该轮仍只有 runtime close 证据，没有 provider 列表外部核验。

随后接通 provider active-list 外部核验：execution backend 只在真实 Sandbox create metadata 中追加固定 owner/component 与 32hex session 标签；test-only verifier 延迟使用 E2B 2.49.1 公共 `Sandbox.list`，显式过滤该双标签及 `RUNNING/PAUSED`，每页最多 8 条、最多 3 页/24 条、单请求 5 秒，丢弃 provider 对象并只保留固定状态与计数。它不记录或输出任何 Sandbox ID/provider 原文，也不新增清理或 kill API。接线后真实复验 timeout 用时 23.196 秒、cancel 用时 24.098 秒、完整 A→B 用时 60.871 秒；四个新创建 Sandbox 在受管关闭后均由 provider 查询确认对应 session active count 为 0。该证据可以支持“本轮无 running/paused Sandbox 残留”，但不代表 provider 历史记录或 no-GC OSS immutable baseline/object/manifest 已删除。

独立 wait-result uncertainty live 用例于 2026-10-14 **通过**，最终复验用时 18.594 秒。用例先让真实结构化 unittest 完成 provider dispatch，并通过包装 `PreparedRemoteExecution._bind_handle` 的原实现证明真实 run handle 已成功绑定；随后 test-only 将该 handle 的第一次 `wait()` 替换为固定异常且绝不调用原 wait，模拟“远端可能已执行，但宿主无法取得结果”的不确定边界。产品路径同步 fence/close runtime、销毁整个 Sandbox，异步任务只持久化 `failed` / `result_status=error` / `recovery_status=recorded` 与固定“远端工作区工具执行失败。”，注入字符串未进入模型消息、SessionEvent、task snapshot 或诊断，本地主机 Registry/shell/code runner 均未调用，审批恰好一次；受管上下文退出后 provider session 查询为 active=0。第一次真实运行已经正确完成隔离，但 harness 将产品“远端”错误文案误写为“远程”而安全失败；修正后新增真实 `prepare_async_tool_call` 离线契约测试和近似文案拒绝测试。该首次运行随后另以 owner 标签做有界只读查询，结果也为 active=0；此查询不等价于保存或披露当时的 session/资源 ID。

独立 pre-bind uncertainty live 用例于 2026-10-14 **一次通过**，用时 18.439 秒。用例在真实 provider background command 已 dispatch 后，让 `_E2BRunHandle.__init__` 原实现先成功校验并封装 raw handle，再在返回 `PreparedRemoteExecution._bind_handle` 前注入一次固定异常；断言 dispatch 已发生、raw handle 已生成、bind 调用次数为 0。transport 将包装异常净化为启动失败，runtime 因“已派发但无 handle”同步 fence/close 并销毁整个 Sandbox；Agent 只看到 `failed` / `result_status=error` / `recovery_status=recorded` 与固定公开错误，注入字符串未泄露，async task 恰好一个，宿主 local Registry/shell/code runner 未调用，审批恰好一次，宿主指纹不变。受管退出后的 provider session active count 为 0。该测试证明了 start-before-handle 断线的真实控制面路径，但不等价于 provider kill 本身失败已经验证。

独立 kill-success acknowledgement uncertainty live 用例于 2026-10-14 **一次通过**，用时 19.107 秒。用例先完成真实 dispatch、handle bind，并用一次 wait 不确定触发 runtime fence；随后只包装本次 backend 内的具体 Sandbox 实例 `kill()`，任何路径都先调用 provider 原方法，且只有其严格返回 `True` 后才记录真实 kill 成功并抛出固定“成功确认丢失”异常。产品将其转换为固定 `ExecutionBackendCloseError`，同时 backend/runtime 的 finally 均进入 closed；remote runner 只投影固定 ToolResult error，wait 与 kill 两个注入字符串均未泄露，受管 cleanup 没有发出第二次 kill。provider session 查询为 active=0。离线测试另证明 `False` 或 provider 原始异常不会被冒充为确认丢失。该用例验证的是“真实 kill 已成功、但宿主未收到成功确认”，不等价于真实 provider kill 失败。

2026-10-15 完成安全 GC 第一阶段并通过真实事务级 orphan cleanup。产品只跟踪 `_ensure_object` 先观察不存在、create-only 写入成功且精确回读后的本事务新 object；仅当 manifest 写入明确报错、随后 probe 明确返回不存在、且 active 已精确回滚并确认时，才逐个调用内部 v2 object rollback delete。删除前必须同时通过规范 `objects/<sha256>` 路径、普通文件、精确 size、完整正文和 SHA-256 复核，删除返回 `False`、异常或删除后仍存在都会转为固定净化错误；清理是 best-effort，单个失败保留 inert orphan 但不覆盖原确定发布失败。probe 异常、写成功但 probe 不存在、manifest 不匹配、active 回滚不确定或 durable commit 后异常均绝不删除。该原语没有注册给 Agent，也不接受 generation、目录或通用扫描目标；安全性依赖正常入口的 `SessionEventWorkflow.writer.lock` 跨进程单写者租约与进程内 workspace/coordinator 锁，绕过工作流直接并发共享同一 session 不在本阶段保证内。

独立 orphan-cleanup live 用例使用真实 `CodingAgent` → Registry → runtime → v2 commit，仅在当前随机 session 的规范 generation manifest 调用进入 provider **之前**注入一次确定失败；object 写入、active mutation、manifest probe、active rollback 和 object delete 均走真实 Sandbox/OSS 产品路径。用例确认全部 first-generation 新 object 均真实删除、v2 session 只剩允许的空目录、普通文件恢复 baseline、固定错误不泄露注入正文，且 runtime 不 fence；随后同一 Sandbox 通过 Agent read 和 `code_interpreter` 的 `6 * 7 == 42` 复验。前两次真实运行分别用时 26.698 秒和 25.007 秒，均已完成上述产品断言与关闭后 active=0，只在后置 harness 对“最终诊断阶段”和普通路径审批次数的错误假设上失败；修正为有界阶段历史与 partial-control 普通路径零人工审批契约后，第三次于 26.140 秒完整通过。三次随机 session 均在受管 close 后由 provider active-list 确认 active=0。

2026-10-18 完成安全 GC 第二阶段的离线产品接线。`WorkspaceVersionCoordinator` 新增默认关闭的初始化期开关和冻结统计报告；普通 restore 只校验 manifest 实际引用的对象，继续忽略未发布甚至正文损坏的 orphan，避免改变既有恢复可用性。获得宿主维护授权后，GC 会严格读取有界 listing、解析 canonical v2/v3 manifest、验证 session/baseline/parent/operation 与每代状态转移，并把所有历史 full-state `files` 的 digest 并集作为保留集，而不是只看 latest。第一次与第二次扫描必须在目录/文件 inventory、generation 链、保留集、orphan 集及恢复状态上逐项相同；每个 orphan 还必须证明文件名、size、完整正文与 SHA-256 一致，才调用独立 `gc_delete_workspace_version_object`。删除失败会固定净化，删除完成后第三次扫描必须等于原 inventory 精确减去 orphan，且 manifest/reference/state 全部不变。中途部分删除后发生 provider 错误不会伪报成功，下次可重新扫描剩余 inert orphan。

CLI 接线保留“确定性宿主失败先于云调用”：首次 remote 请求先捕获并纯本地预检 seed/checkpoint、构造尚未创建 Sandbox 的惰性 runtime；随后在仍持有 `SessionEventWorkflow.writer.lock` 时调用 session cleaner 证明旧 `RUNNING/PAUSED` 为 0，成功后才把带 GC initializer 的 runtime 交给 Agent。若 started-without-terminal 的恢复流程已完成同一清理，则首次请求不重复。local、help、list、direct-exit 和取消 Plan 仍不会为 GC 创建云资源。GC 不扫描或递归删除 v1 checkpoint roots；当前 v1 digest 只作为 generation 链锚，旧 v1 cache 继续保留。新增离线用例覆盖 v2/v3 历史引用并集、无 manifest crash orphan、默认关闭、损坏 orphan 兼容、两次扫描漂移、invalid chain 零删除、精确 transport 删除、删除/后置复核失败、CLI 清理顺序/去重/失败释放锁和 local 零云；未调用真实云。

同日完成迁移开关的两步收敛。第一步让 CLI 在 resume 时不再要求操作者重复记住后端：省略 `--execution-backend` 会在取得单写者锁后严格验证并采用持久 binding；显式 local/remote 仅作为一致性断言，双向错配在 Sandbox 配置加载前 fail closed。binding-first/header-second 的发布顺序支持“只有 `execution.json`”的崩溃窗口恢复。第二步增加 `AGENT_DEFAULT_EXECUTION_BACKEND`，只控制尚无身份且未显式选择的新会话；配置 remote 时在任何目录/身份创建前完成 Sandbox 预检，配置 local 或显式 local 时完全不加载 Sandbox。默认配置被拆出模型 `Settings` 并按需读取，所以非法默认不会阻断已有会话或显式选择；相反绑定的 TOCTOU 注入会被精确请求拒绝。两步都保证损坏、不安全链接/硬链接、身份不一致或未知对象 fail closed，远端配置失败不会运行事件恢复、创建 client/runtime、捕获 workspace seed或回退 local。安装缺省仍为 local，**尚未移除 local 受限进程**。

离线验证后的首次真实探针曾在 Sandbox 创建阶段因目标 RAM Role 信任策略拒绝 `AssumeRole` 而收到 403，未进入 OSS marker 写入。信任策略修正后，2026-10-10 已重新调用真实云并完成 Stage 1、Stage 2 和 live Agent E2E：总共创建并销毁 5 个 Sandbox，marker/临时 checkpoint 按各自协议清理，live immutable baseline 按设计保留。配置只经严格加载边界读取，credentials 未打印或写入报告；真实 DeepSeek/Brave API 未调用，独立 Ubuntu 主机环境未另行验证，`data/sessions/current` 的持久事件未被改写。

| 测试文件 | 数量 | 重点覆盖 |
| --- | ---: | --- |
| `tests/test_agent.py` | 39 | 系统提示词逐实例安全加载、流式正文/工具调用重组、失败流追加 attempt、模型驱动 Plan 多轮澄清/取消/上限/协议纠正/校验后分块输出/快照一致性、同 turn 异步占位/真实结果回注、Agent 自主 inspect→resolve 恢复及合法工具消息顺序、紧急等待唤醒、generation fence、唯一 stdin broker、运行期 EOF、事件提交先于执行、审批/日志注入、维护生命周期和 CLI；审批与 Plan 请求的 close/enqueue 原子性及多并发请求精确回传有确定性回归。 |
| `tests/test_async_tasks.py` | 6 | accepted/start 两阶段、两个长任务真实并发、会话绑定查询、逻辑取消、迟到结果审计、异步工具分类、模型控制工具、started-without-terminal 与 durable terminal 精确区分，以及事件 sink 卡住时的有界紧急关闭。 |
| `tests/test_async_workflow.py` | 7 | 异步事件状态机、`awaiting_async` 同轮续步、占位结果前禁止 started、恢复状态、取消恢复、非只读未知结果阻断、SessionEvent 共享恢复投影、显示标记和摘要生命周期边界。 |
| `tests/test_main_remote_task_cleanup.py` | 4 | remote started-without-terminal 恢复时持 writer lock 先清资源再写 `outcome_unknown`；失败不落终态并可下次重试；已完成启动清理的 session 首次请求不会重复清理；accepted-only、已有终态、local、新会话和 direct-exit 零清理调用。 |
| `tests/test_aliyun_sandbox_session_cleanup.py` | 11 | 精确 metadata/state/ID 复核、running/paused 去重清理、分页/对象/时间上限、list/connect/kill/最终复查失败净化、SDK request timeout 语义以及系统级异常传播。 |
| `tests/test_bash_shell.py` | 13 | 命令/参数白名单、路径绕过、审批、真实执行、隔离副本、Python 委托及越权代码不启动执行器。 |
| `tests/test_cli_launcher.py` | 4 | console script 注册目标，以及 CMD/PowerShell/POSIX 启动器固定使用项目虚拟环境、透传参数且不下载依赖。 |
| `tests/test_session_cli.py` | 18 | normal/urgent canonical JSON 回放、含 urgent 的中断恢复、同/异会话锁、Windows 锁 errno 分类、安全命名与父链、延迟创建、new/resume/list、精确 busy/损坏错误、banner 和初始化失败资源释放。 |
| `tests/test_client.py` | 5 | SDK 构造参数、默认配置加载、类型、篡改后边界复核及异常行为。 |
| `tests/test_code_interpreter.py` | 10 | 安全计算、文件/网络/系统能力拒绝、拒绝发生在进程启动前、净化环境、输出/超时/资源和 fail-closed。 |
| `tests/test_code_interpreter_runner.py` | 11 | 五字段 payload 精确绑定、有界 stdin、重复键/NaN/缺失/额外/bool/越界拒绝、limits 先于代码、四类 POSIX rlimit 及 min 不放宽、可选 `RLIMIT_NPROC`、import/get/set 失败不执行，以及 Windows 不导入 `resource`。 |
| `tests/test_config.py` | 8 | 模型配置与新会话默认后端配置隔离、系统环境优先、dotenv 参数、严格 local/aliyun-sandbox 值、非法值净化、URL/模型、已移除日志模式不生效和密钥 repr。 |
| `tests/test_deepseek_e2e.py` | 1 | 显式启用的真实 DeepSeek API 到最终 Agent 文本链路；默认跳过。 |
| `tests/test_aliyun_sandbox_probe.py` | 13 | 严格配置、Bucket/地域/路径约束、密钥隐藏、可信 venv 默认值及 workspace/OSS/相对路径与特权用户拒绝、metadata 字符串、SDK 参数转发、成功/异常/不确定写入清理、始终销毁、默认无调用和禁用边界。 |
| `tests/test_aliyun_sandbox_e2e.py` | 1 | 真实 Sandbox + OSS marker 写读删；必须由两个环境开关显式启用，默认跳过。 |
| `tests/test_aliyun_sandbox_recovery_probe.py` | 12 | 阶段 2 manifest 完整绑定、路径与文件完整性、A/B 生命周期顺序、精确清理、一次性 C 清理 Sandbox、残留 checkpoint 安全错误和 no-E2B 默认导入。 |
| `tests/test_aliyun_sandbox_recovery_e2e.py` | 1 | 真实 A→kill→B OSS 检查点恢复；必须由恢复环境开关和配置恢复开关共同显式启用，默认跳过。 |
| `tests/test_aliyun_sandbox_live_agent_e2e.py` | 64 | 八个真实 `CodingAgent` harness 均使用 scripted client 且不调用 DeepSeek：5C6 覆盖 A 中错误代码→失败 unittest→editor 修复→成功 unittest、partial/full-access 和 B 的 OSS v2 恢复；5C4 取消、timeout、wait/pre-bind/kill-ack uncertainty 分别覆盖运行中取消、结构化超时、两种 handle/result 不确定及真实 kill 成功后确认丢失；事务 orphan cleanup 覆盖确定 manifest 失败后的真实 active rollback 与精确 object 删除；maintenance A→B 覆盖双文件 v3、随机 staging 清理、sealed provider 前拒绝、direct-kill=False 独立 cleaner、扫描式 GC 和 v3 恢复。八者都断言宿主指纹不变、local handler/项目子进程未调用、最终 close 及 provider active=0；分别使用独立 process gate，且都叠加 probe/recovery 配置门控，默认跳过。56 个纯离线用例还覆盖 provider 双标签过滤、分页/条目上限、active 存在、错误脱敏、精确注入顺序/单次约束、真实删除成功后才记录、有界阶段历史、双资源 maintenance 绑定、普通/敏感审批契约和默认零云。2026-10-23 修正 test-only trusted-runtime 旧签名与 B 侧旧单 transport 假设后，maintenance 双资源真实门禁再次通过（62.932 秒）。 |
| `tests/test_aliyun_sandbox_cross_process_live_e2e.py` / `tests/_aliyun_sandbox_cross_process_worker.py` | 11 | 一个默认关闭的真实跨 OS 进程 gate 与 10 个离线协议用例：A 创建带 exact owner/session 标签的 Sandbox 后不 kill 退出；父进程在 A 退出后有界等待 active=1；独立 B 进程调用产品 cleaner；父进程复验 active=0 并始终 emergency cleanup。离线覆盖零云 gate、固定 token、非固定输出不回显、进程/观察/清理顺序、A 歧义失败、`0 → 1` 可见性收敛、持续 0 超时和多资源 fail closed。2026-10-18 修正 provider metadata 索引即时可见假设后真实通过，耗时 6.102 秒。 |
| `tests/test_execution_backend.py` | 35 | 阶段 3 本地 no-op 与双 Sandbox 远端生命周期：execution/persistence metadata 精确分离、不同对象/ID、组合路由、默认 factory 绑定、并发单次双创建、initializer ready 前顺序；任一创建/transport/initializer 失败回收全部候选，同对象去重，关闭按 execution→persistence 顺序且单端失败不跳过另一端，独立 cleaner 兜底、错误净化、幂等关闭、安全租约与密钥隔离。 |
| `tests/test_execution_runtime.py` | 18 | local 委托、remote 单 runtime/单 group、精确授权摘要与一用 capability、同步/异步统一 dispatch 生命周期、workspace transaction gate、旧授权入口禁止绕过专用 mutation transaction，以及取消或不确定结果时 fence + Sandbox kill；并发审批签发不同的一用 permit，取消和重放不能串用。 |
| `tests/test_agent_execution_runtime.py` | 9 | Agent/runtime 严格 backend+session+closed 绑定、local 默认等价、remote 读搜与异步执行路由、绝不 local fallback、普通/紧急关闭顺序、构造失败资源回收、seed bootstrap，以及远端 mutation 不确定结果记录为 `outcome_unknown`。 |
| `tests/test_main_execution_backend.py` | 23 | 阶段 5B3b CLI 后端解析、配置先于目录、local 完全惰性、remote direct-exit/Plan 取消、首次请求精确 runtime 组装、失败清理、resume 自动采用持久 binding、显式双向错配、binding-only crash window、按需新会话默认 local/remote、显式覆盖、非法默认隔离、双向竞态注入 fail closed、配置失败零产物和 list/help 导入边界；阶段 5B4a/5B4b 确定性预检先于云调用；GC 开关默认关闭，remote 首次请求按 runtime 构造→active=0 清理顺序启用，清理失败会关闭惰性 runtime 并释放 session lock。 |
| `tests/test_workspace_seed.py` | 9 | 阶段 5B4a snapshot 排序/digest/二进制/硬排除/三重上限/truncated/竞态/repr，以及 typed uploader 的空 workspace、create-only write/read、最终精确清单、恶意 metadata 和部分失败净化。 |
| `tests/test_workspace_checkpoint.py` | 10 | 阶段 5B4b canonical/strict manifest、固定 root、>1 MiB manifest 容量、空/新/半成品 checkpoint、跨 transport 恢复、manifest/正文/额外路径/类型/截断篡改拒绝、第三方错误脱敏、active 非空拒绝、64 层深度与建云前 layout preflight。 |
| `tests/test_aliyun_sandbox_tools.py` | 69 | 共享 workspace/cwd、路径/链接/扫描/输出边界、远端 files/commands 与 persistence；可信 venv 缺失创建、预置验证及篡改拒绝；persistence root 的 root:root/0700 固化、root/runtime_user list/read/write 双向实探、cleanup、ready 前顺序、固定错误净化、未绑定零探测，以及 v1/v2/v3 Files API 显式 root 控制面；唯一 invocation runner、结构化命令/受限 code、策略审批、超时和错误脱敏；结构化命令固定 AS/CPU/NPROC/FSIZE rlimit 的完整调用、继承更严限制、缺界/非法值/设置失败/非 POSIX fail closed，以及 payload 不可扩大边界；runner 与宿主裁剪标志严格 OR 合并、exact-boundary 传播和恶意标志拒绝；active rollback delete、v2 object rollback delete 和独立 GC delete 均只接受精确绑定的普通文件，并复核正文、摘要、删除返回值和删除后不存在，错误不泄露 provider 正文。 |
| `tests/test_aliyun_sandbox_resource_limits_live_e2e.py` | 4 | 独立默认零云门禁；有限内存/CPU/墙钟/输出压力、每次失败后的同 Sandbox 复用、最终 session cleaner 与 provider active=0；进程创建能力前置拒绝，不伪造 `RLIMIT_NPROC` live 结论。 |
| `tests/test_aliyun_sandbox_approval_concurrency_live_e2e.py` | 7 | 一个默认关闭的真实审批并发 gate 与 6 个离线协议用例：同 workflow 重入拒绝、独立 workflow 精确 request/pending 绑定、workspace gate 对 mutation/command 的串行化、拒绝/异常/cancel/close 后释放、模型布尔值不能伪造批准、失败后始终 cleaner 与 active=0；默认零云且不调用 DeepSeek。2026-10-20 显式 live 一次通过，耗时 21.159 秒。 |
| `tests/test_aliyun_sandbox_default_remote_canary_live_e2e.py` | 15 | 单会话与固定三会话 batch 两个默认关闭的真实 gate，加 13 个离线协议用例：真实 CLI 默认解析、remote 持久绑定、scripted client 经 CodingAgent/Registry/runtime 完成 partial-control overwrite/read、宿主零 fallback、v2 object/generation 有界计数、remote 默认下显式 local 回滚优先、原 binding 不改写、失败即停、严格整数耗时/计数聚合，以及每次失败后 cleaner/active=0；默认零云且不调用 DeepSeek。2026-10-20 单次 live 耗时 15.144 秒；三次 batch 3/3 通过，总耗时 42.609 秒。 |
| `tests/test_aliyun_sandbox_concurrent_remote_canary_live_e2e.py` | 16 | 固定并发 3 的默认关闭真实 gate 与 15 个离线协议用例：spawn 三进程跨进程 Barrier、每会话完整默认 remote/v2/local 回滚/双 Sandbox 路径、环境不可放大、父进程有界墙钟、超时终止后的 exact-session emergency cleaner、missing result/nonzero exit/start failure/清理未确认 fail closed、无 replacement、整数脱敏报告和失败增量不完整标志。2026-10-23 真实并发 3/3 通过，总耗时 18.609 秒，active_zero=3、object/generation 各 +3；只证明当前并发下界，不推断最大配额。 |
| `tests/test_production_canary.py` | 8 | 运维 canary 的纯离线合同：固定三道精确 unittest target、单 gate 环境、严格 allowlist、`sys.executable`、总时限、fail-fast、成功/失败/超时/子进程异常/配置错误退出码，以及子进程输出和资源坐标不进入单行 JSON。2026-10-23 入口真实演练三道 gate 全部通过，`alert_required=false`。 |
| `tests/test_aliyun_sandbox_natural_kill_exception_live_e2e.py` | 13 | 一个默认关闭的真实 gate 与 12 个离线协议用例：原始 SDK 对临时非响应 loopback endpoint 产生真实 transport timeout，exact provider 可见性屏障、当前 concrete Sandbox 绑定、独立 cleaner 的单次精确 kill、backend closed、finally emergency cleanup、active=0、门控先于 settings/provider，以及固定阶段和严格 0/1 净化诊断；默认零云且不调用 DeepSeek。前两轮安全暴露 harness 假设且最终 active=0，补上真实 SDK trigger 与可见性屏障后第三轮通过，耗时 3.856 秒。 |
| `tests/test_aliyun_sandbox_security_boundaries_live_e2e.py` | 10 | 一个默认关闭的独立真实 gate 与 9 个离线协议用例：基础/recovery/config 前置且默认零云，产品 SDK create 显式禁网；execution Sandbox 的 OSS 路径 `ismount=false` 且 list/read/create 3/3 不可用；两个公网数字地址、阿里云元数据 `100.100.100.200:80` 与额外 link-local 的 4 次无 DNS、无响应读取连接拒绝；AS/NPROC/FSIZE 越界失败后同 execution runtime quick reuse；独立 persistence 控制面 v2 小写对象读回；严格冻结计数、fixed stage、双资源 close、exact-session cleaner 与 active=0。2026-10-22 真实通过，耗时 70.190 秒，未调用 DeepSeek。 |
| `tests/test_aliyun_sandbox_oss_ram_policy_live_e2e.py` | 13 | 一个默认关闭的真实 RAM Role prefix gate 与 12 个离线协议用例：配置 prefix 随机 marker 正向 write/read/remove；同 Bucket/Role 的规范化 sibling prefix 负向 mount/list/write；negative create 异常后必须由 fresh configured-prefix control Sandbox 重新证明可用，避免把 provider 波动冒充权限拒绝；list-only、write-only、list-and-write 固定安全分类；精确 marker 清理、所有 Sandbox direct kill、exact-session cleaner 与 active=0。旧 `AliyunOSSFullAccess` 下于 2026-10-22 固定失败为 `code=list_and_write`（4.779 秒）；替换为自定义最小权限策略后于 2026-10-23 通过（9.667 秒），报告 `positive_roundtrips=1 sibling_denials=1 active_zero=1`。 |
| `tests/test_aliyun_oss_ram_policy_document.py` | 1 | 动态读取本地配置并验证 paste-ready 自定义 RAM Policy：仅含官方读写挂载所需 6 个 OSS action，Bucket/list 与 object 资源分离，`oss:Prefix` 和 object ARN 精确绑定配置 prefix，拒绝 `oss:*`、全局 Resource、额外字段及 sibling 匹配。2026-10-23 定向通过；策略已应用云端，并由正向往返、sibling 拒绝和完整安全门禁真实复验。 |
| `tests/test_editor_file.py` | 27 | 文本与完整行锚点编辑、换行自动适配、多匹配/重叠/无尾换行、diff 二次审批、候选保留、恢复、句柄/链接/硬链接和敏感路径拒绝。 |
| `tests/test_event_queue.py` | 4 | 普通事件批量、整批状态预检、Plan 完整 turn 的屏障批提交、屏障排空、紧急取消回调以及“紧急事件最后提交”的顺序。 |
| `tests/test_maintenance.py` | 3 | 无副作用构造、幂等 start/close、可注入周期以及日志维护任务独立 fail-soft；不会后台清理候选。 |
| `tests/test_observability.py` | 17 | 按日合法 JSON、双阈值分片、损坏/不安全状态停止当前落盘、有界顺序缓冲与部分补写、溢出统计、7 天保留、脱敏、静默 fail-soft 和链接/硬链接攻击拒绝。 |
| `tests/test_pending_changes.py` | 3 | UUID 候选的完整写入/读取/安全清理、篡改和硬链接拒绝、diff 上限及终端控制字符净化。 |
| `tests/test_pending_tools.py` | 8 | 候选检索、拒绝后复用、恢复 diff 审批、无重复候选、目标/清单篡改拒绝、非 UTF-8 原文件预览、7 天完整候选/orphan 边界清理和硬链接保护。 |
| `tests/test_permissions.py` | 13 | 两种可信宿主权限模式、普通/敏感修改路由、策略新增受保护路径、模型参数不可提权、Unicode 绝对路径、跨线程模式传播、宿主 setter 活动轮竞态拒绝、CLI 二次确认/降级、已初始化 Agent 同步切换、sealed-secret 与 account-action 永久硬门，以及紧急退出迟到提交栅栏。 |
| `tests/test_process_cancel.py` | 4 | 运行中进程树取消、cancel-before-bind、取消后禁止启动以及逻辑 cancelled 终态优先。 |
| `tests/test_read_file.py` | 28 | 分页、Base64、句柄边界、敏感路径和多重硬链接拒绝。 |
| `tests/test_recovery.py` | 14 | 修改型未知结果的只读外部检查、精确目标、运行期一次性 token、双重检查 TOCTOU 拒绝、可验证状态与结论一致性、unknown 轨迹判断、Registry 绑定、`/recovery` 只读诊断和 `/resolve` 停用。 |
| `tests/test_registry.py` | 7 | 注册同步、旧版/缺失网络策略 fail-closed、策略自保护、精确保护、返回关联及审批分发。 |
| `tests/test_registry_execution_runtime.py` | 17 | 远端路由、输出复核、无 local fallback、local/None 兼容、宿主 pending 隔离与 observability；remote read 策略等价；同步/异步执行的 foreground 精确授权、双重策略、固定错误语义和一用 invocation；write/editor 必须具备专用 workflow，bash 同步入口和其他破坏命令仍拒绝。 |
| `tests/test_runtime_features.py` | 20 | OpenAI/客户端延迟加载、直接退出、取消 Plan 不创建客户端/事件、移除模式命令只告警不调用模型、Plan 无工具 JSON Object 协议、无 act 预读的专属三选一读取、选择后安静等待下一请求、Ctrl+C/EOF/exit/interrupt 取消、普通 act 模型先结束后的连续主命令补全、卡死模型/敏感审批/input broker 与真实主线程 Ctrl+C 的有界紧急退出、错误分类、超时/重试边界和纯文本/NO_COLOR UI。 |
| `tests/test_plan_mode.py` | 4 | 严格最终计划/澄清 JSON 协议、最终计划禁止未决问题、恰好三个互斥选项、推荐上限、安全选择数据编码、Markdown 确定性分块/重组和控制字符/额外字段拒绝。 |
| `tests/test_terminal_input.py` | 5 | 命令候选/说明/前缀过滤、非 TTY/NO_COLOR/缺失依赖降级、交互回答读取不创建 PromptSession。 |
| `tests/test_web_fetch.py` | 7 | 全 DNS 地址公网检查、内网/混合拒绝、IP 固定与 TLS SNI、重定向逐跳复核、文本上限、净化错误和返回 URL 校验。 |
| `tests/test_web_search.py` | 8 | 固定 Brave endpoint/header、查询编码、schema、密钥隔离、混合 DNS 拒绝、响应边界、恶意字段、策略 fail-closed、注册相关性和日志脱敏。 |
| `tests/test_schemas.py` | 10 | 消息、JSON 数值、ToolResult、审批状态和工具严格模型。 |
| `tests/test_search_tools.py` | 15 | glob/grep、扫描上限、敏感目录隐藏、链接和硬链接。 |
| `tests/test_session_events.py` | 13 | 连续持久事件、严格快照与深度冻结、损坏/缺口拒绝、崩溃补全、工具消息邻接投影、开放尾部、异常边界、普通顺序、显式摘要和保守重试分类。 |
| `tests/test_session_event_views.py` | 3 | 22 种 v1 事件统一派生视图、旧 JSON 字节兼容，以及中断恢复只回注安全事实而不重放 partial stream。 |
| `tests/test_session_backend_binding.py` | 18 | 阶段 5B1 新 local/remote 绑定、显式重开匹配与双向拒绝、已有身份忽略新会话 fallback、require-existing 防身份消失重建、未指定请求时采用现有绑定、新存储可选 local/remote 默认、旧会话 local 迁移、binding-first 双文件发布崩溃窗、严格 sidecar 安全读取、no-replace 竞态、故障锁释放和事件字节不变。 |
| `tests/test_session_hardening.py` | 16 | 单写者、无覆盖发布、目录替换、提交不确定性、跨轮调用关联、全局修改阻断、轮内 resolver 强关联、Chat Completions 延迟投影、摘要后待恢复精确身份保留、resolution 已落盘但 resolver 结果未落盘的崩溃窗口、摘要覆盖和临时文件处理。 |
| `tests/test_skills.py` | 13 | metadata-only 启动发现、SDK-only/Plan 目录、完整清单按需加载、后续步骤 token、跨 Skill/跨运行时拒绝、UTF-8 字节分页、快照变化、目录洪泛、非法树、链接/硬链接/竞态、普通工具保护与注册/日志/恢复分类同步。 |
| `tests/test_workspace_versions.py` | 36 | OSS v2/v3 canonical manifest、object→active→manifest 发布顺序、A→B 恢复、multi-file baseline object、1 MiB 上限、候选重放、active/manifest 未知结果 fence、host seed drift、内容篡改、缺代、分叉与转移语义；事务 cleanup 覆盖既存 object 保留、generation 1 baseline、重复 digest、旧 generation 引用、write-success/probe-none、rollback/durable 不确定及部分清理失败；扫描式 GC 覆盖默认关闭、损坏 orphan 兼容、v2/v3 全历史引用并集、无 manifest orphan、listing 类型、扫描漂移、invalid chain 零删除、删除失败与第三次复核失败。 |
| `tests/test_remote_mutation_runtime.py` | 15 | Registry→runtime→v2 的远端 create/overwrite/edit 集成、稳定 operation id、严格 diff 审批、host handler 不调用、escape/hard exclusion、审批拒绝与竞态、reservation 并发阻断、close 非阻塞及未知结果传播；并发独立 workflow 保持精确绑定，共享 workflow 拒绝重入，审批异常与审批中 close 均释放 gate 并安全 fence。 |
| `tests/test_workflow.py` | 8 | 初始未批准、首次与 diff 状态转换、候选/diff 精确绑定、严格 32 hex override 前置校验、不可复用、拒绝和异常复位。 |
| `tests/test_write_file.py` | 27 | 创建/覆盖、diff 二次审批、候选保留/成功清理、第二次批准后创建父目录、失败恢复、敏感路径、链接和硬链接。 |
| **合计** | **874** | 2026-10-23 在真实云门禁默认关闭时完整发现 874 项：849 通过、22 跳过、3 失败。3 个失败仍是既有 `skills/grilling` Skill catalog 消息序列旧断言，本轮没有新增失败；`compileall` 与 `pip check` 通过。最小权限替换后的串行 batch、固定三会话并发、maintenance 双资源和运维 canary 三道门禁均真实通过；所有可判定的测试 session 最终均确认 active=0。 |

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tool tests
```

### 显式运行真实 DeepSeek E2E

只有在操作者接受真实 API 调用、账户用量和网络依赖后运行：

```powershell
$env:RUN_DEEPSEEK_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_deepseek_e2e -v
Remove-Item Env:RUN_DEEPSEEK_E2E
```

测试通过 `load_settings -> create_client -> CodingAgent -> DeepSeek API` 发起一次请求，不记录或打印 API Key。2026-09-08 曾显式设置开关并通过当时的非流式全链路；2026-09-09 切换到流式协议后的本轮回归未读取 `.env`、未再次产生真实 API 调用，因此当前只确认离线流式协议测试通过。常规全量回归仍默认跳过真实 E2E，避免无意产生网络调用和账户用量。

### 显式运行真实阿里云 Sandbox E2E

先在本地 `.env` 或系统环境变量中完整填写 `.env.example` 的阿里云字段，并确认 Bucket、Sandbox 与 OSS Endpoint 同地域、RAM Role 只授权目标前缀。真实测试要求双重显式门控：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_e2e -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_E2E
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

也可以在探针配置已启用后运行 `sandbox-probe --execute`。两种方式都会尝试创建真实云资源并读写目标 OSS 前缀，可能产生账户用量；阶段 1 已通过真实验证。

阶段 2 需要在阶段 1 通过后再单独显式开启恢复配置和测试门控：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_RECOVERY_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_recovery_e2e -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_RECOVERY_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

或使用 `sandbox-recovery-probe --execute`。恢复探针也会创建、销毁真实云资源并写读删检查点；若 B 创建失败且一次性清理 Sandbox C 无法创建，错误会明确报告 `checkpoint_path` 可能残留，操作者应依据该前缀做受控人工核查。

Agent Registry/runtime 真实 E2E 使用第三个独立门控，只为当前进程设置，不需要改写 `.env`：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_AGENT_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_AGENT_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该测试不调用 DeepSeek，仅使用小型合成 snapshot；成功路径创建并关闭两个真实 Sandbox，并按设计保留 content-addressed immutable baseline、object 与 generation manifest。2026-10-10 通过的是旧版 Stage 1、Stage 2 和只读/baseline live E2E，详细脱敏结果见 [`ALIYUN_CLOUD_VALIDATION_REPORT.md`](ALIYUN_CLOUD_VALIDATION_REPORT.md)。当前 5C6 在前九次逐步定位并修复 harness/SDK/runtime/v2 上限问题后，已于 2026-10-14 第十次真实运行通过；加入 execution owner/session metadata 与 provider active-list 后再次通过，并确认 A/B 关闭后该 session 的 running/paused 数量为 0。随机 session 的 immutable OSS baseline/object/manifest 仍可能按 no-GC 设计保留。

运行中取消 E2E 使用独立 process gate，必须同时保留两个配置门控，只运行专用测试类：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_CANCEL_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLiveCancellationE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_CANCEL_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例只上传受控合成 unittest，不调用 DeepSeek，不开放自由 shell；2026-10-14 已真实通过 handle 绑定后的运行中取消、迟到结果隔离和同一 runtime 复用，并在加入 provider active-list 后复验该 session 关闭后 active=0。

结构化超时 E2E 也使用独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_TIMEOUT_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLiveTimeoutE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_TIMEOUT_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例的长 unittest 内部执行上限为 2 秒；provider 只获得额外 5 秒收尾宽限。2026-10-14 修复 deadline 竞争后已真实通过固定超时结果与同一 Sandbox ID 的短测试复用，并在受管关闭后由 provider active-list 复验该 session active=0。

已绑定 handle 后的 wait 结果不确定 E2E 使用第四个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_WAIT_UNCERTAINTY_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLiveWaitUncertaintyE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_WAIT_UNCERTAINTY_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例仅在真实 provider dispatch 与 handle 绑定成功后注入一次固定 wait 异常，不调用 DeepSeek，不把 provider 结果或注入正文写入诊断。2026-10-14 已真实通过净化 failed 终态、runtime 同步 fence/close、宿主零 fallback 与 provider session active=0；它证明的是“结果等待不确定时整 Sandbox 隔离”路径，不等价于 handle 绑定前断线或真实 provider kill 失败已经验证。

provider 已 dispatch、handle 绑定前断线 E2E 使用第五个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_PREBIND_UNCERTAINTY_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLivePrebindUncertaintyE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_PREBIND_UNCERTAINTY_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例让真实 provider 返回 raw background handle，并在 `_E2BRunHandle` 原构造器成功后、`PreparedRemoteExecution._bind_handle` 前注入一次固定异常。2026-10-14 已真实通过“dispatch=true、raw handle=true、bind=false”、净化 failed 终态、runtime fence/close、宿主零 fallback 与 provider session active=0；它不会伪造或破坏 provider kill，因而仍不代表真正的 kill 失败已通过。

真实 kill 已成功、但成功确认丢失 E2E 使用第六个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_KILL_ACK_UNCERTAINTY_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLiveKillAckUncertaintyE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_KILL_ACK_UNCERTAINTY_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例先让 wait 不确定触发 fence，再只包装当前具体 Sandbox 的 `kill()`：必须先真实调用且严格返回 `True`，之后才注入确认丢失异常。2026-10-14 已真实通过固定 close error、backend/runtime closed、受管 cleanup 不二次 kill、净化 failed 终态、宿主零 fallback 与 provider session active=0。它不会把 `False` 或 provider 原始异常当作成功，因此不宣称真正的 kill 失败已经验证。

事务级 orphan cleanup E2E 使用第七个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_ORPHAN_CLEANUP_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLiveOrphanCleanupE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_ORPHAN_CLEANUP_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例只在当前随机 session 的规范 generation manifest 调用进入 provider 前注入一次确定失败，object/active/probe/rollback/delete 仍走真实产品路径；2026-10-15 已真实通过本事务全部新 object 精确删除、v2 无普通文件、baseline 恢复、同 Sandbox Agent read/Code Interpreter 复用、宿主零 fallback 与关闭后 provider active=0。它不扫描或删除历史 object，因此不能替代 2026-10-18 新增引用扫描式 GC 的单独 live 验收。

v3/cleaner/扫描式 GC 合并增量验收使用第八个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_MAINTENANCE_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_live_agent_e2e.AliyunSandboxLiveMaintenanceE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_MAINTENANCE_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例默认 skip 且零云调用；显式开启后只使用 scripted client 和小型合成 snapshot，不调用 DeepSeek。2026-10-18 一次通过，耗时 57.856 秒：Sandbox A 真实发布双文件 v3、清理随机 staging，并在 provider 前拒绝 `.env`；随后只对该具体实例注入 direct `kill()` 严格返回 `False`，由真实独立 provider cleaner 接管并确认 active=0。Sandbox B 只在该零活跃前提成立后启用 `collect_orphans=True`，恢复 v3，对一个真实 OSS crash orphan 完成双扫描、exact delete 与第三次复核，逐字节保留 manifest 和 3 个被引用 object；B 关闭后再次确认 active=0。该结果验证受控直接销毁失败分支，不等价于自然 provider 故障或控制面重启后的跨进程 stale 实例已验收。

控制面退出后的跨进程 stale 接管使用第九个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_CROSS_PROCESS_STALE_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_cross_process_live_e2e.AliyunSandboxCrossProcessStaleLiveE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_CROSS_PROCESS_STALE_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例默认 skip，且整个 suite 在三个门均满足前不会加载 Sandbox settings、启动 worker 或调用 cleaner。它不创建模型客户端、不调用 DeepSeek：进程 A 用固定 owner + 随机 32hex session metadata 创建一个真实 Sandbox，验证安全 ID 形状后不 `kill()` 退出；父进程在 A 退出后有界等待 provider exact-session 索引收敛到 active=1，再启动不同 Python 进程 B 调产品 cleaner，最后由父进程外部确认 active=0 并执行幂等 emergency cleanup。worker 只输出固定 token，任何非固定输出、第三方异常或超时都折叠为固定 harness 错误；不得把此命令改成更宽 metadata 或移除 finally 清理。

内存、CPU、墙钟与输出压力验收使用第十个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_RESOURCE_LIMITS_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_resource_limits_live_e2e.AliyunSandboxResourceLimitsLiveE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_RESOURCE_LIMITS_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例默认 skip 且零云调用，不创建模型客户端或调用 DeepSeek。2026-10-19 首次运行安全暴露 runner `truncated` 标志在 transport 丢失的真实产品缺口；修复 code/command 两条协议并完成严格离线回归后，第二次通过，耗时 27.194 秒。它在同一 Sandbox 中验证有限内存分配、CPU 循环、结构化命令墙钟超时和固定大输出，并在每次失败后证明 runtime/Sandbox 可复用，最终由 cleaner 和 provider 查询确认 active=0。公开 AST 不允许创建进程，因此该门禁不声称已 live 压测 `RLIMIT_NPROC`。

审批并发压力验收使用第十一个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_APPROVAL_CONCURRENCY_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_approval_concurrency_live_e2e.AliyunSandboxApprovalConcurrencyLiveE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_APPROVAL_CONCURRENCY_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例默认 skip 且零云调用，不创建模型客户端或调用 DeepSeek。2026-10-20 一次通过，耗时 21.159 秒：首个 remote write 的 diff callback 阻塞时，同一 workflow 的碰撞请求固定拒绝；独立 workflow 的第二个 write 与已可信授权的 `compileall` 都不能越过 workspace transaction gate。首写提交后第二写只进入自身精确绑定的 callback 并被拒绝，命令随后执行；审批异常、执行前取消和审批中 close 都释放 reservation，旧 permit 不可执行或重放。finally 关闭 runtime、调用 session cleaner，并由 provider exact-session 查询确认 active=0。

默认 remote 新会话小流量 canary 使用第十二个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_default_remote_canary_live_e2e.AliyunSandboxDefaultRemoteCanaryLiveE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该用例默认 skip 且零云调用，不创建真实模型客户端或调用 DeepSeek。2026-10-20 一次通过，耗时 15.144 秒：测试进程临时把新会话默认后端设为 `aliyun-sandbox`，由真实 CLI/default loader 创建 remote 持久绑定，经真实 Agent/Registry/runtime 完成一项普通 overwrite/read，并确认本次 v2 object 与 generation 各 +1。随后在 remote 默认仍生效时以显式 `--execution-backend local` 创建回滚会话，证明不加载默认值、Sandbox 配置、runtime 或 client，且原 remote binding 字节不变。finally 精确清理并确认 active=0；不改写 `.env`。这是一条金丝雀证据，不是长期可用性或配额容量结论。

默认 remote 有界三会话批次使用第十三个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_BATCH_E2E = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_default_remote_canary_live_e2e.AliyunSandboxDefaultRemoteCanaryBatchLiveE2ETests -v
Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_BATCH_E2E
Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED
Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED
```

该 gate 默认 skip 且零云调用，批次数固定为 3，任何环境变量、模型字段或工具参数都不能扩大。2026-10-20 首批 3/3 通过，净化汇总为 active_zero=3、objects=3、generations=3、total=42.609 秒、min=13.889 秒、max=14.412 秒；每次都包含真实默认 remote、显式 local 回滚和最终 active=0。若任一次失败会立即停止后续 attempt，避免故障时继续放量。

固定三会话并发下界使用独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_CONCURRENT_REMOTE_CANARY_E2E = "1"
$testExit = 1
try {
    .\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_concurrent_remote_canary_live_e2e.AliyunSandboxConcurrentRemoteCanaryLiveE2ETests -v
    $testExit = $LASTEXITCODE
}
finally {
    Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_CONCURRENT_REMOTE_CANARY_E2E -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED -ErrorAction SilentlyContinue
}
if ($testExit -ne 0) { throw "concurrent remote canary failed: $testExit" }
```

该 gate 的并发数在代码中固定为 3，不能由环境或模型扩大；三进程经 Barrier 同时放行，任一异常不补发 replacement。2026-10-23 一次真实通过：3/3 success、3/3 active=0、object/generation 各 +3、总耗时 18.609 秒。它证明的是当前并发下界，不是云账号最大配额。

生产运维入口及告警/回滚演练：

```powershell
.\.venv\Scripts\python.exe .\scripts\production_canary.py --run-live-canary
```

该入口固定顺序运行 RAM prefix、安全边界和默认 remote/local rollback 三道门禁，只输出一行脱敏 JSON；失败分别以 20—23 的非零退出码区分 gate 失败、超时、子进程异常与参数错误。完整告警、active=0 检查、保持/切回 local 和升级步骤见 `docs/PRODUCTION_CANARY_RUNBOOK.md`。2026-10-23 一次真实演练三道 gate 全部通过、`alert_required=false`。

受控真实 SDK/transport kill 异常验收使用第十五个独立 process gate：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:RUN_ALIYUN_SANDBOX_LIVE_NATURAL_KILL_EXCEPTION_E2E = "1"
$testExit = 1
try {
    .\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_natural_kill_exception_live_e2e.AliyunSandboxNaturalKillExceptionLiveE2ETests -v
    $testExit = $LASTEXITCODE
}
finally {
    Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_NATURAL_KILL_EXCEPTION_E2E -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED -ErrorAction SilentlyContinue
}
if ($testExit -ne 0) { throw "natural kill exception live failed: $testExit" }
```

该用例默认 skip 且零云调用。双 Sandbox 架构下，显式启用后有界等待 provider 对 exact owner/session 可见的资源集合收敛为恰好两个，并确认 execution ID 位于其中；随后只对 execution 的原始 SDK `kill()` 使用临时非响应 loopback API endpoint 触发真实 transport timeout。backend 仍继续直接关闭 persistence，独立 cleaner 再使用未改写 settings 接管 execution 残留并由 active-list 证明 active=0。旧单资源架构于 2026-10-20 第三轮通过（3.856 秒）；2026-10-22 双资源适配后复跑通过（6.190 秒）。该证据是可重复的受控 SDK/传输异常恢复，不等价于 provider 服务端自然宕机。

三项发布安全边界的组合验收使用第十六个独立 process gate。2026-10-21 的单 Sandbox 版本在 `stage=create` 安全失败；完成双 Sandbox 隔离后，2026-10-22 已按以下命令复验通过。该命令继续保留为未来变更后的显式回归入口：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED = "true"
$env:ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS = "false"
$env:RUN_ALIYUN_SANDBOX_LIVE_SECURITY_BOUNDARIES_E2E = "1"
$testExit = 1
try {
    .\.venv\Scripts\python.exe -m unittest tests.test_aliyun_sandbox_security_boundaries_live_e2e.AliyunSandboxSecurityBoundariesLiveE2ETests -v
    $testExit = $LASTEXITCODE
}
finally {
    Remove-Item Env:RUN_ALIYUN_SANDBOX_LIVE_SECURITY_BOUNDARIES_E2E -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED -ErrorAction SilentlyContinue
    Remove-Item Env:ALIYUN_SANDBOX_PROBE_ENABLED -ErrorAction SilentlyContinue
}
if ($testExit -ne 0) { throw "security boundaries live failed: $testExit" }
```

该 gate 默认 skip 且零云调用，不创建模型客户端。成功报告的固定期望是 persistence_unavailable=3、network_denials=4、resource_denials=3、reuse_checks=3、v2_roundtrips=1、backend_closed=true、active_zero=true。persistence 证据还要求 execution 内 `os.path.ismount('/mnt/oss')` 为 false；网络证据必须同时包含 SDK create 显式禁网和四个固定数字地址的无响应读取连接拒绝。2026-10-22 真实报告完全匹配，耗时 70.190 秒。

## 当前限制与后续风险

- Bash 的临时测试副本不是强沙箱，运行测试代码仍以当前用户权限执行；
- Code Interpreter 是能力受限子进程，不是独立内核/VM；它不能安全承诺执行任意第三方 Python，只能执行白名单内纯计算；
- 阿里云 Sandbox 已完成阶段 1—5B 基线，并在 2026-10-14 至 2026-10-20 通过 partial-control、full-access、运行中取消、结构化超时、handle 已绑定后的 wait 结果不确定、provider dispatch 后的 pre-bind 断线、真实 kill 成功确认丢失、事务级 orphan cleanup，以及合并 maintenance 主路径：固定可信 venv、Agent 异步远端 unittest、精确一用 capability、write/editor 事务、统一 workspace gate、迟到结果隔离、未知结果 fence、OSS v2/v3 A→B 恢复、随机 staging 清理、受控 direct-kill=False 和真实 SDK/transport kill 异常后的独立 cleaner、sealed provider 前拒绝、确定发布失败 object 回收和扫描式 orphan GC 均已在真实云贯通。控制面退出后的跨进程 stale Sandbox 已由独立进程 B 接管并外部证明 active `1 → 0`；有限内存、CPU、墙钟和输出压力也已在同一 runtime 逐项失败后复用并确认 active=0；审批并发又证明同 workflow 重入拒绝、独立 workflow 精确绑定、mutation/command gate 串行及异常/取消/close 后释放；默认 remote canary 在两个观测时点累计 4/4 成功、v2 object/generation 各 +4、显式 local 回滚与 active=0。当前仍不能直接标记为无条件生产验收：样本仍不足以说明长期配额、并发流量或跨日趋势，本轮受控 SDK/transport timeout 不等价于未受控 provider 服务端故障自然发生，进程数上限没有可由公开 AST 安全触发的 live 压力入口，命令级旧 handle/result 不会重附着，旧 v1 checkpoint roots 仍按 immutable cache 保留；Agent-facing 删除/清空与自由 shell 继续关闭；
- CLI 已能在恢复时从安全持久 binding 自动采用 local/remote；新会话也可通过 `AGENT_DEFAULT_EXECUTION_BACKEND=aliyun-sandbox` 分批切换，显式 `--execution-backend local` 和把变量设回 `local` 提供回滚面。2026-10-20 的单次与固定三次真实 canary 已证明默认 remote、显式 local 回滚、失败即停和原 binding 不改写。安装缺省仍是 local，本地受限执行尚未下线；后续需跨时点继续观察真实云可用性、配额、对象增长与资源清理，不能把 remote 故障转成本地 fallback；
- permission mode 与 execution backend 尚未绑定成“低权限自动进 Sandbox”的产品策略：`partial-control + local` 仍会在宿主受限实现中运行，只有绑定 `aliyun-sandbox` 的会话才会把 workspace 危险操作放到云端；
- “高权限免普通审批，但密钥与账号能力例外”的代码硬门已经完成：local `full-access` 仍能操作普通宿主绝对路径，但 `.env*`、密钥/证书、credentials、token 等 sealed 路径在 local/remote 的读取、mutation、删除/清空、扫描、seed/version 和 provider 返回链路均固定拒绝。2026-10-18 maintenance live 已增量证明 remote `.env` 读取在 transport/provider 前拒绝且不输出正文；当前仍无账号变更工具，未来接入时必须把独立可信批准绑定到具体动作，不能仅传一个可由模型影响的布尔值；
- 自动持久化现覆盖 `write_file` / `editor_file` 及严格白名单的 remote-only `format_json`；后者已证明多文件 snapshot/隔离候选/diff 审批/单 generation 提交协议，但不是通用 watcher，也不会捕获任意命令副作用。未来若开放构建、包管理或代码生成命令，必须显式接入同一候选协议并新增命令专属安全限制，不能把本轮结果解释为任意 shell 已安全；
- 阶段 5B2c1 的 Code Interpreter Linux/POSIX runner 已按最新宿主策略收紧内存/CPU/进程/文件上限；2026-10-21 起，结构化 `unittest` / `compileall` / `validate_json` runner 也在 `preexec_fn` 固定施加 AS=256 MiB、CPU=30/31 秒、NPROC=4、FSIZE=16 MiB，且缺界、设置失败或非 POSIX 都在项目代码执行前 fail closed。2026-10-22 security-boundaries 真实门禁已验证结构化 AS/NPROC/FSIZE 三项越界失败及每项后的同 execution runtime 复用；CPU 30/31 秒未纳入该组合门禁。两类 runner 的 `RLIMIT_NPROC` 仍受内核 real UID 计数语义影响，这些 rlimit 仍不是 cgroup/VM 级聚合配额；
- 2026-10-21 已证明当前 OSS 动态挂载无法在同一 Sandbox 内通过 chmod/chown 建立 root-only `0700`，该失败事实没有改变；产品已于 2026-10-22 改为 execution/persistence 两个物理 Sandbox，execution create metadata 不含 OSS/auth，实际项目 unittest 复核 `/mnt/oss` 非 mount 且 list/read/create 全部不可用；独立 persistence 端的 v1/v2/v3 Files API 继续显式使用 root，真实 v2 往返已通过。该架构解除的是“不可信代码直接触达挂载”的阻断。2026-10-22 sibling-prefix 门禁在旧 `AliyunOSSFullAccess` 下固定失败为 `code=list_and_write`；控制台审计确认信任主体并确认无 Bucket Policy 后，2026-10-23 以精确 Bucket/prefix 的 6-action 自定义策略替换 FullAccess，门禁真实通过 `positive_roundtrips=1 sibling_denials=1 active_zero=1`，完整 security-boundaries 也再次通过。RAM Role prefix 最小权限已完成行为验收；安装缺省仍因持续运营样本不足保持 local；
- remote runtime 仍明确拒绝 `list_pending_changes` / `apply_pending_change`；远端 mutation 已使用绑定 backend、session、runtime/workspace generation、原文件摘要与精确参数的内存候选协议，不复用或暴露宿主 `data/pending` 控制面存储；
- 阶段 1/2 已在真实云账户验证：3 个文件、121 bytes 成功跨 A→kill→B 恢复，临时 checkpoint 已删除且 A/B 已销毁。2026-10-10 的旧 live Agent E2E 验证了 3 个合成文件、109 bytes 的 immutable baseline 恢复和当时的 Registry 权限矩阵；2026-10-14 的新 5C6 补上当前 write/editor、失败测试→修复和 OSS v2 generation 的 A→B 恢复。2026-10-15 又补上确定发布失败时本事务新 object 的真实删除；2026-10-18 maintenance live 已补上双文件 v3 `format_json`、跨 Sandbox 恢复，以及基于 v2/v3 全历史引用并集的一个真实 orphan GC。已发布 generation 与全部 v1 immutable baseline 继续保留；未被任何 generation 引用的规范 v2 object 只会在 writer lock、active=0、双扫描稳定和删除后复核均成立时清理；
- 后台取消仍以可靠保存的逻辑终态为准。local 使用 Windows Job Object/POSIX 进程组，物理终止受操作系统能力约束，迟到结果只进入审计事件；remote 使用 provider handle，cancel-before-bind、运行中取消和迟到结果都有协议覆盖，其中 handle 绑定后的运行中取消已于 2026-10-14 真实通过且关闭后 provider active=0。dispatch 且 handle 已绑定后的 wait 结果不确定、provider 已返回 raw handle 但 runtime 尚未绑定时的断线，以及真实 kill 已成功但成功确认丢失，均已用真实 Sandbox 验证会 fence/close、返回净化 failed 终态并取得 active=0。精确标签重查、重连、销毁和 active=0 复核的独立 cleaner 又于 2026-10-18 在具体实例 direct `kill()` 受控返回 `False` 后真实接管成功，同日还完成控制面进程退出后的跨进程 stale 接管；2026-10-20 又在 provider 已精确看见当前 Sandbox 后，让原始 SDK `kill()` 产生真实 transport timeout，并验证 cleaner 单次接管与 active=0。该受控传输故障不能冒充 provider 服务端自然宕机。控制面重启只恢复资源清理能力，不自动续跑旧命令或宣称取得旧结果；
- 每日日志按 2,000 条或 8 MiB 自动分片；损坏/不安全的已有分片会被保留且停止当前落盘，失败事件会进入 512 条有界缓冲重试，但缓冲溢出、进程非正常退出或永久磁盘故障仍可能丢失安全元数据，且始终不会改变工具执行结果；
- `clear_directory` 做审批前后快照和逐项复核，但 Windows 普通文件系统 API 不能提供完整事务或抵御拥有同等主机权限的并发攻击者；
- 已有完整 SessionEvent 消息持久化和手动摘要投影，但尚无自动 token/成本预算、自动摘要、Git 状态感知或自动回滚；
- 内部 diff 仍有 64 KiB/1,000 行上限，但这只影响预览数据；本地完整候选存放在受保护目录，远端 `format_json` staging 经校验后立即精确清理，完整候选 bytes 只保存在绑定当前 runtime/workspace generation 的宿主内存对象中；CLI 不打印 diff 正文；
- 真实 E2E 依赖账户、密钥、网络、接口和所配置模型，离线单元测试通过不代表这些外部条件可用。

阿里云方向已从“打通核心闭环”进入“小流量迁移与持续运营观测”：截至 2026-10-23，最小 RAM 权限、完整安全边界、v3 恢复/扫描式 GC、固定三次串行批次、固定三会话并发下界，以及运维 canary 的告警 JSON 与显式 local 回滚演练均已真实通过。当天新增观测为串行 3/3、并发 3/3、所有对应 session active=0，object/generation 增量均与成功数精确一致；当前清单中的可立即编码和单次验证任务已完成。剩余跨日可用性、长期配额/成本、自然 provider 故障和真实值班响应时效属于持续运营证据，在样本不足前不改变安装缺省；未受控 provider 服务端故障只能在自然发生时留证，不应为了勾选任务主动制造。命令级 handle/result 重附着仍受 provider 缺少可验证接口限制。这些是持续风险或外部能力边界，不再是可直接编码完成的核心构建项；正式签收前仍不应扩展 Agent-facing 删除/清空、包管理、代码生成或自由 shell，旧本地受限执行仅作为 local backend 保留，不得作为 remote 失败 fallback。

2026-10-22 的双 Sandbox 改造保留上述已完成协议，并把 execution 与 OSS persistence 的物理信任边界拆开；安全边界与双资源清理真实门禁均已通过。因此“同一 Sandbox 内无法建立 root-only 挂载隔离”不再阻断 remote 迁移，但 RAM Role 最小权限审计、跨前缀拒绝验证及持续运营样本仍是生产正式签收前的外部/运营门槛。

同日跨前缀真实门禁先把 RAM Role 风险从“尚待证明”升级为“已确认失败”：旧 FullAccess 下相邻 sibling prefix 的 list 与 write 均成功。2026-10-23 完成自定义策略替换后，该门禁已取得 sibling_denials=1、active_zero=1，且配置 prefix 正向往返与完整双 Sandbox 安全门禁同时通过；RAM Role prefix 权限阻断已经解除。固定三会话并发与运维告警/回滚入口也已各真实演练一次；正式生产签收仍需积累跨日可用性、长期配额/成本、自然故障和实际值班响应时效，在此之前不改变安装缺省。
