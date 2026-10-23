# Coding Agent 使用说明

> 最后核对日期：2026-10-01

我是小白用户，请你正确指导我使用 AI，并纠正我的使用错误。

## 会话会怎样保存

不带会话参数时，命令行为了兼容旧数据，仍使用 `current` 会话。你也可以给不同工作
创建独立会话：

```powershell
agent                         # 打开或恢复 current
agent --new-session feature-a # 创建名为 feature-a 的新会话
agent --new-session           # 让程序自动生成唯一名称
agent --list-sessions         # 列出可恢复的会话，不连接模型
agent --resume feature-a      # 恢复已存在的 feature-a
agent --permission-mode partial-control # 默认权限模式
agent --permission-mode full-access     # 高风险完全权限模式
```

在未激活虚拟环境的 PowerShell 中，把上面的 `agent` 换成 `.\agent.ps1` 或 `.\agent.cmd`，
参数保持不变。两个终端可以分别打开两个不同会话；同一会话一次只允许一个 Agent
写入。如果第二个终端提示 `current` 正在被占用，请用 `agent --new-session [NAME]`
打开独立会话，不要删除锁文件。

## 怎样选择权限模式

权限模式只能由终端宿主设置，模型、提示词、网页内容和工具参数都不能修改。输入
`/permissions` 可以查看当前模式、风险和用法。只能在终端空闲的主提示符切换：

```text
/permissions partial-control
/permissions full-access
```

降级到 `partial-control` 立即生效。提升到 `full-access` 时终端会再次显示风险，
必须在同一输入通道明确输入 `yes`；其他输入、Ctrl+C 或 EOF 均取消。切换只影响
之后新启动的调用，不追溯已经开始的工具；重启后仍以 `--permission-mode` 启动参数为准。

- `partial-control` 是默认且推荐的模式。普通项目文件的写入、编辑、删除和清空会直接执行；
  `.env*`、密钥/证书、`AGENTS.md`、`code.md`、`skills/`、Agent 配置、虚拟环境、会话、候选和日志等
  敏感目标需要人工批准。敏感写入/编辑只问一次 diff，敏感删除/清空只问一次操作预览。
  项目外绝对路径不允许访问。
- `full-access` 允许 Agent 对本机普通文件使用绝对路径，文件修改和当前启用的联网搜索/读取
  不再询问。它仍拒绝任意 shell，并保留链接/重解析点/硬链接保护、严格工具参数、SSRF、
  DNS/TLS、超时和大小限制。只有你信任当前项目、模型请求和数据来源时才使用。

权限模式不是“关闭所有安全检查”的开关。尤其是 `full-access` 下，网页和搜索结果仍只是
不可信数据，不能当成用户命令或授权执行。

## 怎样使用斜杠命令和 Plan

在真实交互终端中，每次回到主提示符后输入 `/` 都会显示命令菜单；继续输入可过滤，
Tab 或方向键可选择。普通 act 请求运行期间预先打开的主命令输入也保留同一补全能力：
如果模型先结束，这条读取会成为下一轮主提示符，不会由遗留的普通输入偷偷吞掉你的命令。
`--plain`、非 TTY 或缺少 `prompt_toolkit` 时会安全降级为普通文本输入。

普通输入始终按 act 工作。需要规划时只使用 `/plan [需求]`，它只影响这一次交互；旧的
`/mode plan` 和 `/mode act` 已移除，输入后只会收到提示，不会切换状态或发给模型。
Plan 的模型请求期间不会预先打开 `act` 主提示符。信息不足时，终端才会显示一个结构化
问题和恰好三个互斥选项，并新建提示符为 `请选择 1、2 或 3：` 的专属读取。输入数字后，
程序把选择作为数据发给下一次模型请求；等待期间也不会重新出现 `act` 提示符。这是多个
模型请求之间的暂停/继续，不是在同一次生成中途注入文字。所有输入仍由唯一 broker 读取，
选择期间的 Ctrl+C、EOF、`exit` 或 `/interrupt` 会安全取消 Plan。

最终计划不允许留下“尚待确认”问题。若仍缺少会改变方案的关键信息，模型必须继续提出
三选一澄清；不合规结构只会进行有限次数的自动纠正，然后安全停止。原始 JSON 不会直接
刷到终端，取消或失败也不会把半成品 Plan 写入会话；只有完整最终计划通过验证后才一次性
保存。随后已验证的 Markdown 会按章节立即分块显示，没有人为延迟；这些块拼接后严格等于
最终保存的计划。这是“校验后分块流式显示”，不是直接展示供应商的 JSON 或 raw chunk。

每个会话保存在 `data/sessions/<会话名>`。重新启动并使用 `--resume` 后，程序会从
SessionEvent 事件流恢复以前的模型上下文，无需复制粘贴历史消息。如果上次会话在回答或
工具调用中断，恢复流程会先验证已持久化事件，追加安全的中断边界，并且不会偷偷重放
旧工具调用。

会话目录含有完整用户消息、模型回答、流式数据块、工具参数和工具结果，因此属于
敏感内部状态；Agent 自己的文件、搜索和 shell 工具均不能访问它，也不要手工改动其中的文件。

事件采用只追加设计。每个连续编号对应一个独立 JSON 分段，先在同目录完整写入、
同步并验证，再原子发布；这样不会产生 JSONL 半行。任何序号缺口、JSON 损坏、链接、
多重硬链接、不安全目录或磁盘写入问题都会让会话 fail closed：当前工作流停止，且
不会继续请求模型或执行下一个工具。错误信息不会显示底层路径或异常内容。

模型的每条完整回答同时保存组装后的消息和按接收顺序排列的原始 stream 快照，
而不是给每个 chunk 单独创建事件。上下文过长时，可以由宿主显式追加
`compaction/summary`；摘要只替换模型上下文投影中的完整旧轮，原始事件不会被删除。
当前没有擅自设置自动摘要阈值。网络错误或 Ctrl+C 被程序捕获时，已经接收的快照会
追加到 `assistant/attempt`，但失败回答不会进入模型上下文；突然断电或强制结束进程时，
内存中尚未提交的数据仍可能无法恢复。

如果模型请求、流关闭或审批卡住，在活动请求期间输入 `exit`/`quit`，或连续按两次
Ctrl+C，会进入有界紧急退出。程序先禁止任何迟到的模型结果、工具结果和会话事件提交，
并优先释放会话锁；可能阻塞的 SDK 关闭和日志刷新只做后台尽力清理。因此可以立即用
`agent --resume <会话名>` 恢复已经可靠写入的上下文，而不必删除锁文件。强制断电或直接
杀死进程仍可能丢失尚未落盘的内存数据。

## 崩溃后的工具状态

程序恢复未完成轮时不会偷偷执行工具：

- 模型提出了工具调用，但还没有持久化顶层 `tool/call`：记录为 `not_started`，模型
  可以重新发起；
- 已持久化 `tool/call`，但没有 `tool/result`：追加 `outcome_unknown`；
- `read_file`、`list_files`、`grep`、`list_pending_changes`、`code_interpreter`、
  `inspect_recovery`、`resolve_recovery`、`load_skill`、`read_skill_resource`，以及
  `bash_shell` 的 `compileall`、`validate_json` 属于可安全重新发起的操作；
- 文件修改、候选应用、删除、清空及 `unittest` 等操作必须先由 Agent 检查外部状态并
  追加确认结论。在所有未知修改结果都解决以前，任何其他非只读工具调用都会被拒绝，
  不能通过修改工具名或参数绕过。

如果看到“上次工具执行结果未知”，不要手工再次批准写入或删除。Agent 会执行下列恢复链：

1. 用事件中精确的 `turn_id` / `step` / `call_id` 调用 `inspect_recovery`，只读检查目标；
2. 收到与会话、目标和本次观察绑定的随机一次性 `inspection_token` 后，再判断 `applied` 或 `not_applied`；`matches_requested` 只能选 `applied`，`different` / `missing` 只能选 `not_applied`，只有 `unknown` 才允许结合可靠轨迹选择任一结论；
3. 调用 `resolve_recovery` 时提交同一目标、令牌和非空证据。程序会立即重新只读检查；观察变化或结论与当前可验证后置条件矛盾都会拒绝、消耗 token，并要求重新检查。当前状态约束不是过去执行过程的证明。

确认成功只追加 `confirmed_applied` 或 `confirmed_not_applied` 事件，旧工具调用绝不重放。
`/recovery` 仍可用于只读诊断，但不会解除闸门；繁琐的 `/resolve` + 手工证据 + `yes` 流程已停用。

## 怎样添加和使用 Skill

Skill 是给 Agent 的项目级按需说明，存放在 `skills/<小写-kebab-case-名称>/SKILL.md`。
最简单的做法是复制 `skills/_template`，修改目录名、front matter 的 `name` 与
`description`，再写正文。`name` 必须与目录名完全一致；`description` 应用一行同时说明
“能做什么”和“何时使用”。详细格式与安全上限见 `skills/README.md`。新增或修改后重启
Agent，启动目录才会刷新。

你不需要在对话里手工指定加载步骤。每次模型请求只会临时看到 Skill 的 name 和
description；匹配任务时，Agent 自主调用 `load_skill` 读取完整 `SKILL.md`。如果清单明确
指向 `references/`、`scripts/` 等支持文件，Agent 会等到下一模型步骤，再使用返回的
会话运行时 token 调用 `read_skill_resource` 分页读取必要文本。`scripts/` 不会自动执行。

安全边界：Skill 目录和内容按不可信 user 级数据处理，不能覆盖系统提示词、权限模式、
工具 schema、恢复闸门或用户审批。令牌不能跨 Skill、跨 Agent/会话运行时或在加载的同一
模型步骤使用；同一 Skill 重新加载、清单或资源发生变化后旧令牌失效。目录树有数量、
深度和大小上限，并拒绝非法 UTF-8、链接、Windows 重解析点、硬链接与检查期间变化。
默认 `partial-control` 下，普通读取、搜索和验证型 shell 工具看不到 `skills/`；任何修改
还必须经过敏感路径人工审批。请优先在 Agent 外由开发者审查和维护 Skill。

## 两类日志不要混淆

- SessionEvent 是恢复会话的完整事实源，保存失败必须停止工作流。
- `data/log` 是脱敏工具可观测性日志，仍按每天 2,000 条或 8 MiB 自动分片，失败时
  fail-soft，不会改变工具执行结果。只有合法分片达到阈值才会进入下一编号；目录或
  文件状态不安全、磁盘不可写或 JSON 损坏时会保留原状态并停止当前事件的落盘，
  事件最多在 512 条有界内存队列中等待后续重试。旧日志不会迁移或删除。

候选文件仍保留 7 天，但清理只在候选的新建、列出或应用操作发生时触发，不是后台
定时任务。只有 `.bin`、没有严格清单的旧候选无法安全恢复，会作为 orphan 在满 7 天
后的下一次候选操作中清理。
