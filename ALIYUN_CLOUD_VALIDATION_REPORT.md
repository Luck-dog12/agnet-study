# 阿里云 Agent Sandbox 真实云验证报告

> 验证日期：2026-10-10  
> 验证范围：阿里云 Agent Sandbox 极速版、OSS 动态挂载、内容寻址 immutable baseline，以及 Agent 实际使用的 Registry/runtime 工具权限边界。

## 结论

本轮真实云验证全部通过。首先在 live gate 未开启时验证了建云前拒绝；随后只为当前测试进程设置显式 gate，没有修改 `.env`。三组真实测试合计创建并销毁 **5 个 Sandbox**，未发现 Sandbox 泄漏。

| 验证项 | 耗时 | 真实 Sandbox | 结果 |
| --- | ---: | ---: | --- |
| gate-disabled 预检 | 未计时 | 0 | 通过：在任何建云动作前拒绝 |
| Stage 1 Sandbox + OSS marker | 6.323 s | 1 | 通过：marker 写入、回读、删除，Sandbox kill |
| Stage 2 A→kill→B 恢复 | 6.358 s | 2 | 通过：3 files / 121 bytes，临时 checkpoint 删除，A/B 均 kill |
| live Agent Registry/runtime E2E | 24.089 s | 2 | 通过：同一随机 session/snapshot 跨 Sandbox 恢复，A/B 均 close/kill |

## live Agent E2E 数据边界

- 测试仅使用 3 个人工合成的安全文件，合计 109 bytes，没有捕获或上传真实项目工作区。
- snapshot digest：`da4cbbbf9936bd4771cba469e579a21027d55497320797fe9c7decc66773a2af`。
- canonical checkpoint manifest：525 bytes。
- Sandbox A 首次通过 Registry 远端只读工具惰性创建，建立 OSS immutable baseline 后恢复到 active workspace。
- Sandbox A 关闭后，Sandbox B 使用完全相同的随机 session 和 snapshot，从已完成的 content-addressed checkpoint 恢复出相同的文件清单和正文摘要。
- 随机 session 和两个 Sandbox ID 只在测试进程内用于断言，未打印、未记录，本报告也不猜测其值。

## Registry 工具权限实测

下表的所有工具结果均经过 `tool.registry.call_tool` 或 Agent 实际采用的 `prepare_async_tool_call` 路径，没有绕过 Registry 直接调用远端 transport。

| 工具/场景 | 实测结果 | 安全含义 |
| --- | --- | --- |
| `read_file` | success | 只读合成 active workspace 文件 |
| `list_files` | success | 返回严格受限、稳定排序的 3 文件清单 |
| `grep` | success | 在合成文件中找到预置标记 |
| `partial-control` 读取 `.env` | denied | 受保护文件在进入远端执行前被拒绝 |
| `partial-control` 的 `../` 路径逃逸 | denied | 不解析、不触碰 workspace 外路径 |
| `write_file` | denied | 未开放远端 mutation |
| `editor_file` | denied | 未开放远端 mutation |
| `bash_shell` | denied | 未开放远端命令执行 |
| `list_pending_changes` | denied | 远端会话不能访问宿主 `data/pending` 控制面 |
| 同步 direct Registry `code_interpreter` | success | 受限纯计算返回 `42`，依次经过宿主策略、一次性 permit 和远端二次策略检查 |
| Agent async `code_interpreter` prepare | denied | 符合 Agent 当前异步路径的 fail-closed 设计 |

在所有被拒绝的工具调用前后，active workspace 的路径/大小元数据和逐文件正文 SHA-256 摘要完全一致，因此未观察到被拒绝操作产生的远端写入或篡改。

## 资源和持久化结果

- Stage 1 的唯一 OSS marker 已删除，其 Sandbox 已 kill。
- Stage 2 的临时恢复 checkpoint 已删除，Sandbox A/B 均已 kill。
- live Agent E2E 的 Sandbox A/B 均已通过 runtime close 触发 kill。
- live Agent E2E 的 immutable baseline 按设计保留在 `<persistence_root>/.agnet-study/checkpoints/v1/<random-session>/<digest>`。这是内容寻址的宿主 snapshot baseline，不是 active workspace；由于本轮未打印 random session，报告不提供也不猜测具体 OSS 对象路径。

## 凭据与控制面

- 本轮没有调用 DeepSeek，因此没有产生模型 API 用量。
- 报告不包含 API Key、URL、RAM Role、Bucket、随机 session 或 Sandbox ID。
- 测试日志和断言未打印 credentials；开关只为当前测试进程设置，未改写 `.env`。
- DeepSeek/OpenAI 模型控制面始终留在宿主，Sandbox 只接收受限合成 workspace 和类型化工具请求。

## 已知限制

- immutable baseline 只表示初始宿主 snapshot，**不是远端 mutation persistence**；远端修改尚不会回写或发布新 checkpoint。
- 当前没有 checkpoint GC/delete 机制；live baseline 需要未来增加受控的保留和清理策略。
- Agent 仍未开放 remote `write_file`、`editor_file`、`bash_shell` 和 async `code_interpreter`；本次 denied 结果是预期的安全边界，不是功能缺陷。
- 真实 live 测试仅使用 3 文件 / 109 bytes 的小快照，可验证协议、权限和生命周期，但不等同于生产文件数、总容量、并发度、长时运行或故障注入负载。
- 受限 Code Interpreter 本次只验证了最小纯计算链路，不代表已对内存、进程数、CPU 或输出上限做生产级压力验证。

## 最终离线回归

真实云验证完成后又在 live gate 默认关闭的状态下执行了最终离线回归，没有再次创建云资源：

- 全量：552 tests，543 passed，6 skipped，3 failures。
- 3 个 failure 仍是既有 `skills/grilling` 对旧 Agent 消息数量断言的影响，与本次 Sandbox/OSS/live E2E 改动无关。
- 相关定向回归：85 项，84 passed，1 个真实 live Agent E2E 按默认 gate 跳过。
- `.venv` 中的 `compileall -q app tool tests` 通过，`pip check` 通过。
