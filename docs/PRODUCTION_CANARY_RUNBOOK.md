# 生产 Sandbox Canary 与故障处置手册

## 入口和安全边界

仅由已授权的值班人员或计划任务在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe .\scripts\production_canary.py --run-live-canary
```

入口固定顺序运行三道现有 live gate：OSS RAM prefix 最小权限、双 Sandbox 安全边界、默认 remote 单个新会话及显式 local 回滚。任一道失败即停止；第三道不会运行批次或并发 gate。总时限固定为 900 秒，Sandbox 命令时限固定为 300 秒，并强制 `ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS=false`。它不会调用模型、改变产品 Agent 行为、扩大默认 remote 配置或发送外部通知，也不会主动制造 provider 宕机。

脚本只向 stdout 输出一行固定结构 JSON。子进程 stdout/stderr 会被捕获后丢弃，报告不包含凭据、Role、Bucket、prefix、Sandbox ID、provider 原文或路径。脚本不自行写文件；由计划任务或监控系统保存这行 JSON 和进程退出码。

## 告警条件

只有 `status=success`、`exit_code=0`、三道 gate 均为 `passed` 才算成功。第三道必须证明一个默认 remote 新会话完成受控变更、读取、回滚和清理，并且显式 local 回滚路径仍可用。以下任一情况必须告警：

- 进程退出码非 0；
- `alert_required=true`；
- `status` 为 `gate_failed`、`timeout`、`subprocess_error` 或 `configuration_error`；
- 计划任务没有在预期窗口产生一行合法 JSON；
- 监控无法保存退出码或安全摘要。

退出码约定：`20` 表示 gate 失败，`21` 表示总时限或子进程超时，`22` 表示子进程启动/运行异常，`23` 表示没有提供唯一的显式运行参数。

## 告警后的立即动作

1. 保持安装默认 `local`。如果新会话已灰度为 remote，立即把 `AGENT_DEFAULT_EXECUTION_BACKEND` 切回 `local`；不要修改已有会话的持久 backend binding，也不要做 remote 到 local 的静默 fallback。
2. 禁止重新绑定 `AliyunOSSFullAccess`。RAM 修复只能维持或进一步收紧已验收的 Bucket/prefix 最小权限策略。
3. 暂停后续 remote canary 和新 remote 放量；不要为了复现而主动制造 provider 故障。
4. 保存计划任务开始/结束时间、脚本版本、固定安全 JSON 和退出码。provider 控制台证据单独存放在受限位置，不得粘贴凭据或 Token；不要把 provider 原文追加到公共告警正文。

## 确认 `active=0`

正常通过或普通 gate 失败时，三道 live gate 的 finally 都会执行精确 cleaner，并把对应 remote session 的 `RUNNING/PAUSED` 数量为 0 作为测试判定条件。因此只有 gate 通过才同时证明本次 `active=0`；第三道显式 local 会话本身不得创建 Sandbox。

若状态是 `timeout`、`subprocess_error`，或进程被系统强制终止，不能仅依据脚本摘要假定清理完成。应在阿里云 Sandbox 控制台按运行时间窗口和元数据 `agnet-study.owner=coding-agent-execution` 检查本次资源；同一 session 会分别出现 `agnet-study.role=execution` 与 `agnet-study.role=persistence`。确认两者均不处于 `RUNNING/PAUSED`。发现残留时，先保留资源标识和状态作为受限证据，再使用既有精确 session cleaner 或控制台精确终止对应资源；禁止按账号全量清理。

## 重跑与升级

- 配置错误：纠正计划任务参数或本地 Sandbox 配置后可重跑一次。
- gate 失败：先确认 `active=0`，排除权限被放宽、网络开关变化或资源限制失效，再重跑一次。
- 超时或 provider 异常：先确认 `active=0`，等待 provider 恢复后再重跑；不要连续快速重试。
- 同一 gate 连续两次失败、任何权限越界、无法确认 `active=0`、出现无法精确清理的残留、最小权限策略被更改，或发现费用/资源异常时，停止 remote 放量并升级给项目负责人和云平台管理员。

恢复 remote canary 的最低条件是：根因已经记录并修复、RAM FullAccess 未回绑、残留已清零、三道固定 gate 重新通过。恢复新 remote 会话放量还需要项目负责人确认；单次通过不能替代跨日可用性、并发、配额和成本观察。
