# agnet-study

一个以通用 Coding Agent 为目标的 Python 单 Agent 项目：通过命令行接收自然语言需求，调用受控工具阅读代码、修改文件、运行检查，并保存可恢复的任务会话。

项目使用 **OpenAI Python SDK** 连接 **DeepSeek 兼容接口**，由宿主程序负责工具权限、人工审批、任务状态和执行环境。执行后端支持本地工作区，以及阿里云 Agent Sandbox + OSS。

目前处于持续开发阶段，适合学习和验证 Agent 的工具调用、执行控制与恢复机制。

## 功能概览

| 能力 | 说明 |
| --- | --- |
| 代码阅读与修改 | 文件读取、目录枚举、文本搜索、文件创建与编辑；受路径策略和审批规则约束。 |
| 受限代码执行 | Python 代码执行，以及白名单内的测试、编译和 JSON 检查；不提供任意 shell。 |
| 交互式规划 | `/plan` 发起规划，必要时通过选项澄清需求，再生成计划。 |
| 持久会话 | 创建、列出和恢复命名会话；事件记录支持任务追踪与结果核验。 |
| 异步任务 | 查询任务列表、查看单个任务、取消任务或中断当前请求。 |
| 权限控制 | `partial-control` 与 `full-access` 两种模式，权限由终端宿主控制。 |
| 云端工作区 | 阿里云双沙箱分别承担代码执行与 OSS 持久化访问，已提交文件可恢复。 |
| 联网与 Skills | 支持 Brave 搜索、受控网页读取，以及按需加载项目内的 Skill 说明。 |

## 快速开始

需要 Python **3.11 或更高版本**、Git，以及可用的模型服务 API Key。以下命令在 Windows PowerShell 中执行。

### 1. 下载并安装

```powershell
git clone https://github.com/Luck-dog12/agnet-study.git
cd agnet-study

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

如果没有 `py` 启动器，可用已安装的 Python 3.11+ 执行 `python -m venv .venv`。后续运行和测试均使用项目的 `.venv`。

### 2. 配置模型

编辑根目录 `.env`，填写自己的配置：

```dotenv
DEEPSEEK_API_KEY=填写你的模型服务密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

以上地址和模型名来自配置模板。使用兼容服务时，请改为该服务提供的地址和账号实际可用的模型名；模型需要支持本项目使用的工具调用。系统环境变量优先于 `.env`。

`.env` 已加入 Git 忽略规则，不要把真实密钥写进代码或提交到仓库。

### 3. 启动会话

```powershell
.\agent.cmd --new-session first-task
```

看到交互提示符后，直接输入自然语言，例如：

```text
阅读项目结构，说明程序入口、工具注册位置和测试目录。
```

默认使用 `partial-control` 权限模式。配置模板默认选择 `local` 后端；本地模式的工作区以本仓库目录为基础，启动脚本会先切换到该目录。

无需激活虚拟环境即可使用 `agent.cmd`。安装后也可以激活 `.venv`，直接运行 `agent`。

<details>
<summary>Linux / macOS 启动方式</summary>

```bash
git clone https://github.com/Luck-dog12/agnet-study.git
cd agnet-study
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
# 编辑 .env 后启动
sh agent.sh --new-session first-task
```

</details>

## 日常使用

### 会话管理

```powershell
.\agent.cmd                         # 打开或恢复 current 会话
.\agent.cmd --new-session feature-a # 新建指定名称的会话
.\agent.cmd --new-session           # 自动生成新会话名称
.\agent.cmd --list-sessions         # 列出已有会话，不调用模型
.\agent.cmd --resume feature-a      # 恢复已有会话
.\agent.cmd --plain                 # 使用纯文本终端显示
.\agent.cmd --help                  # 查看启动参数，不调用模型
```

同一会话同时只允许一个进程写入。会话被占用时，请关闭原进程或创建另一个会话，不要删除锁文件。

### 交互命令

| 输入 | 用途 |
| --- | --- |
| 普通自然语言 | 发起当前任务。 |
| `/plan 需求` | 进入本次需求的规划流程。 |
| `/status` | 查看当前会话和运行配置。 |
| `/permissions` | 查看权限模式及切换方式。 |
| `/permissions partial-control` | 在空闲提示符切换为受控权限。 |
| `/permissions full-access` | 请求提升权限，终端会要求明确确认。 |
| `/tasks` | 列出异步任务。 |
| `/task ID` | 查看指定任务。 |
| `/cancel ID` | 取消指定任务。 |
| `/interrupt 说明` | 中断当前请求并提供重新评估的说明。 |
| `/recovery` | 只读查看需要核验的修改结果。 |
| `exit` 或 `quit` | 退出。 |

遇到审批或规划选项时，按当前提示回复。执行结果无法确认的修改会进入恢复核验流程；已经发生的操作不会因为模型报错而自动撤销。

## 权限与执行后端

**权限模式决定哪些操作允许执行，执行后端决定代码在哪里运行。** 两者独立设置。

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| `--permission-mode` | `partial-control` | 普通项目操作按策略执行，敏感操作需要审批。`full-access` 放宽已支持操作的权限，并允许本地普通文件使用绝对路径。 |
| `--execution-backend` | 新会话读取配置，缺省为 `local` | `local` 在宿主执行受限工具；`aliyun-sandbox` 将工作区工具路由到云端。 |

`partial-control` 会实际修改允许访问的普通项目文件。`full-access` 也不会开放任意 shell，或绕过密钥、凭据、受保护路径及工具参数的硬性限制。远端多文件 `format_json` 保留 diff 审批。

执行后端与会话身份持久绑定。已有本地会话不能直接改为远端会话，需要新建；恢复远端会话时可以省略后端参数。远端不可用时，工作区工具不会自动改在宿主运行。

## 使用阿里云沙箱

本地使用无需配置云沙箱。远端使用前，需要准备阿里云 Agent Sandbox 接入信息、OSS Bucket 和限定目录权限的 RAM Role，并填写 `.env.example` 中的相关项：

- `E2B_API_KEY`、`E2B_API_URL`、`E2B_DOMAIN`，以及可选的 `E2B_TEMPLATE`。
- `ALIYUN_OSS_BUCKET`、`ALIYUN_OSS_ENDPOINT`、`ALIYUN_OSS_BUCKET_PATH`、`ALIYUN_OSS_ROLE_ARN`。
- `ALIYUN_OSS_MOUNT_DIR`、`ALIYUN_OSS_READ_ONLY` 等配置保留适合当前读写流程的值。

然后在 PowerShell 中启用并创建远端会话：

```powershell
$env:ALIYUN_SANDBOX_PROBE_ENABLED = "true"
.\agent.cmd --new-session cloud-task --execution-backend aliyun-sandbox --permission-mode partial-control
```

之后保持云配置有效，即可恢复同一会话：

```powershell
.\agent.cmd --resume cloud-task
```

沙箱按需创建，首次需要远端工作区的工具才分配实例。一个远端会话使用两个独立实例：执行实例运行项目代码，持久化实例通过宿主受控接口访问 OSS。模型调用、权限和审批仍在宿主处理；持久化恢复面向已提交文件，不保留任意进程或内存现场。

当前已提交版本的 `ALIYUN_SANDBOX_TIMEOUT_SECONDS` 默认值为 **300 秒**，是固定存活期限，工具运行不会自动延长它。退出时会尝试销毁实例，恢复时会清理该会话的残留。两个实例和 OSS 分别产生资源开销，超时设置不等于金额预算。

公网出口默认关闭，仅在任务确实需要时配置 `ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS=true`。权限示例见 [OSS 最小权限模板](config/aliyun-oss-persistence-minimal-policy.json)，云端验收步骤见 [生产金丝雀运行手册](docs/PRODUCTION_CANARY_RUNBOOK.md)。

## 常用配置

完整配置以 [.env.example](.env.example) 为准。

| 环境变量 | 默认值 / 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 必填；模型服务密钥。 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com`；兼容服务的接口地址。 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash`；需与账号可用模型一致。 |
| `DEEPSEEK_TIMEOUT_SECONDS` | `180`；模型请求超时，范围 10–600 秒。 |
| `DEEPSEEK_MAX_RETRIES` | `2`；SDK 重试次数，范围 0–5。 |
| `AGENT_DEFAULT_EXECUTION_BACKEND` | `local`；只影响尚未建立后端绑定的新会话，显式 CLI 参数优先。 |
| `BRAVE_SEARCH_API_KEY` | 可选；使用 Brave 联网搜索时填写。 |
| `ALIYUN_SANDBOX_PROBE_ENABLED` | `false`；同时控制远端后端的显式启用。 |
| `ALIYUN_SANDBOX_TIMEOUT_SECONDS` | `300`；云实例存活期限，范围 30–3600 秒。 |

## 项目结构

```text
agnet-study/
├── app/                  # Agent 主循环、会话、权限、异步任务和沙箱运行时
├── tool/                 # 工具注册、文件操作、受限执行与联网工具
├── .agent/               # 工具权限策略
├── config/               # 云资源权限配置示例
├── skills/               # 按需加载的 Skill 说明和模板
├── tests/                # 离线回归与独立门控的真实云测试
├── docs/                 # 运维与验收文档
├── data/                 # 使用说明及本地运行数据
├── .env.example          # 配置模板
├── pyproject.toml        # Python 项目与命令行入口
└── AGENT_STATUS.md       # 项目进度、验证记录和已知限制
```

主要入口是 `app/main.py`，Agent 逻辑在 `app/agent.py`，工具注册位于 `tool/registry.py`。新增 Skill 的格式和加载规则见 [Skills 说明](skills/README.md)。

## 开发与验证

在项目根目录使用虚拟环境运行离线回归。下面先关闭当前终端中可能遗留的 `RUN_*` 测试开关，避免误启用真实云测试：

```powershell
$env:PYTHONUTF8 = "1"
Get-ChildItem Env:RUN_* | ForEach-Object { Set-Item -LiteralPath ("Env:" + $_.Name) -Value "0" }
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q app tool tests
.\.venv\Scripts\python.exe -m pip check
```

真实云测试有独立开关，需要有效凭据并会创建收费资源；执行前按运行手册确认测试范围、清理和验证步骤。具体历史验证结果与尚未完成的工作见 [项目进度](AGENT_STATUS.md) 和 [云端验证报告](ALIYUN_CLOUD_VALIDATION_REPORT.md)。

## 常见问题

- **提示找不到虚拟环境**：先完成 `.venv` 创建和依赖安装；启动脚本不会自动安装依赖。
- **模型请求失败**：核对 API Key、接口地址、模型名和服务状态。工具调用达到轮次上限与模型服务故障是不同问题。
- **远端配置填写后仍在本地执行**：确认是新会话且指定了 `--execution-backend aliyun-sandbox`；已有会话保持原后端绑定。
- **工具被拒绝**：查看 `/permissions` 和当前操作的路径限制。密钥与凭据限制不能通过提升权限解除。
- **终端颜色或排版异常**：使用 `--plain`；若编码异常，可先设置 `$env:PYTHONUTF8="1"`。

欢迎通过 Issue 描述可复现的问题，或通过 Pull Request 提交改进。反馈时请提供启动参数、操作步骤与脱敏后的错误信息。
