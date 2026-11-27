# CI 与源码发布

本项目使用 GitHub Actions。CI 负责离线验证；CD 将通过同一套验证的版本发布到
GitHub Releases，提供源码 ZIP 和 `SHA256SUMS`。当前没有常驻服务器部署，
也不向 PyPI 上传 wheel。Agent 运行依赖仓库内的 `code.md`、`.agent/tool_policy.json`
和 Skills 等资源，源码包保留这些文件。

## 自动验证

`.github/workflows/ci.yml` 在以下情况运行：

- 推送到 `main` 或 `codex/**` 分支。
- 向 `main` 提交 Pull Request。
- 在仓库 Actions 页面手动运行 CI。
- 由 Release 工作流调用，对待发布的同一标签提交进行验证。

矩阵为 Windows、Ubuntu，各运行 Python 3.11 和 3.13。每个任务创建项目 `.venv`，
随后使用该虚拟环境安装依赖、运行完整 `unittest` 离线测试、编译检查与 `pip check`。
任何矩阵任务失败都会阻止源码产物和 Release 发布。

全部通过后，CI 保存该提交的源码 ZIP 和 SHA-256 校验文件到 Actions artifact，
保留 14 天。源码由 `git archive HEAD` 生成，只含该提交的跟踪文件，不包含本机
未提交文件、`.env`、虚拟环境和运行数据。下载解压后按 README 创建虚拟环境并安装。

CI 只使用读取仓库的权限，checkout 不保留写入凭据。真实测试的 `RUN_*` 门禁会
关闭，沙箱开关关闭；工作流不读取模型或云服务 Secrets，也不会运行收费的真实测试。
真实模型和云沙箱验收仍按各自说明显式运行，不能用离线 CI 成功代替真实云验收。

## 发布版本

1. 更新 `pyproject.toml` 中的 `project.version`，提交并通过 CI。
2. 为经过审查的提交创建对应标签，例如版本 `0.1.0` 对应 `v0.1.0`。
3. 推送该标签。Release 工作流重新对该标签提交执行完整 CI 矩阵。
4. 全部通过后，发布版本名对应的源码 ZIP、`SHA256SUMS` 和 GitHub 自动生成的说明。

示例命令（只在确实要发布该版本时运行）：

```powershell
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

标签必须与项目版本完全一致；上传文件取自该标签提交，发布前再次核对远端标签仍指向
通过测试的提交。Release 创建只在发布任务中
获得 `contents: write`，使用 GitHub 自带的短期 token，无需添加个人访问令牌。
工作流不会覆盖已有 Release；需要修正已发布版本时，创建新的版本号和标签。
仅推送普通代码不会创建 Release。

## 查看结果与处理失败

在仓库 Actions 页面查看 CI 或 Release。CI 任一检查失败时，查看对应系统/Python
任务日志，修复后推送新的提交。发布任务失败时，先检查标签与版本是否一致、同名
Release 是否已存在，以及仓库 Actions 是否允许该工作流使用 `contents: write`。

首次配置完成后，应确认真实 Actions 运行成功；工作流文件存在本身不等于云端验证通过。
