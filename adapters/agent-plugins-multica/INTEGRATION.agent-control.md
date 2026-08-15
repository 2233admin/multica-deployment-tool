# 可选适配器：Multica 与 agent-control / agent-plugins

这不是三个项目的合并方案。`agent-control` 和 `agent-plugins` 保持独立；本文件只描述一个可以随时不用、替换或删除的 Multica 适配器。Multica 不成为它们的运行时依赖，也不拥有它们的版本、权限或生命周期。

## 结论

可以联动，但两层职责不同：

| 资产 | 在 Multica 中的落点 | 是否重新构建 Multica |
| --- | --- | --- |
| `agent-plugins/plugins/*/skills/**` 的静态 Skill | 转成一个 Multica Private Plugin；一个源 Plugin 可包含多个 Skill contribution | 否；启用功能后安装/升级即可 |
| `agent-control` 的 Python/Go 工具（liveness、worktree、KB、报告等） | 安装在实际运行 Agent 的设备，通过本地 runtime 命令或 MCP 暴露 | 否；改工具本身时只更新设备上的工具 |
| MCP server / 本地 CLI（Claude、Codex、OMP、OpenClaw 等） | 每台设备的 daemon/runtime 配置；凭据留在设备 | 否；改配置后重启 daemon |
| Multica backend/web/daemon 的一等集成、UI 或新协议 | Multica 源码变更后发布成匹配的镜像/二进制 | 是 |

Multica Private Plugin V1 是声明式 UTF-8 Skill 包，不是任意 Python、Node、WASM 或 Shell 执行器。转换器会故意排除源 Plugin 根目录的 `scripts/`、`tests/`、provider manifest 和许可证目录，只复制 `skills/` 下面的文本及其 references/agents 文件。这样才能通过 Multica 的归档安全校验，也不会因为安装 Plugin 就在 NAS 上执行代码。

## 把 agent-plugins 导入 Multica

先获取两个公开仓库：

```powershell
git clone https://github.com/zaurakworks/agent-plugins.git
```

从分发包根目录执行可选转换器。输出目录必须为空，ZIP 可直接交给 `multica plugin validate` / `multica plugin install`：

```powershell
python .\adapters\agent-plugins-multica\agent_plugins_to_multica.py `
  .\agent-plugins\plugins\adaptive-problem-solving `
  --output-dir .\generated\adaptive-problem-solving `
  --archive .\generated\adaptive-problem-solving-0.2.11.zip
```

其它源 Plugin 同理，例如 `github-collaboration` 会把它下面的多个 Skill 一起转换成同一个 Multica Plugin 的多个 contribution。转换不会把本地绝对路径写进 manifest；默认 key 是 `dev.agent-plugins.<plugin-name>`，publisher 是 `zaurakworks`，版本直接沿用源 Plugin 的 SemVer。

启用 NAS 上的 Private Plugin API（只需做一次）：

```powershell
python .\multica_deploy.py plugins --nas-host YOUR_SSH_HOST --nas-ip YOUR_NAS_IP
```

然后在带有 Private Plugin V1 的 Multica CLI 上验证并安装：

```powershell
multica plugin validate .\generated\adaptive-problem-solving-0.2.11.zip
multica plugin install .\generated\adaptive-problem-solving-0.2.11.zip --workspace YOUR_WORKSPACE
```

安装后的启用、Agent 绑定、升级和回滚在 Multica 的 **Settings → Plugins** 完成。源仓库版本变化时重新转换并递增源版本；不要复用同一个版本号覆盖不同内容。

## 把 agent-control 工具接到 Agent 设备

`agent-control` 目前是治理、知识和本地工具仓，不是一个可被 Multica backend 直接调用的服务端 API。要让它参与任务，给每台运行 Agent 的设备做一次本地安装：

1. 克隆或同步 `agent-control` 到设备上的固定目录。
2. 安装该设备需要的 Python/Go 工具，并把受控入口放到 PATH；不要把 GitHub token、模型 key 或 SSH 私钥上传到 NAS。
3. 用 `client-bootstrap` 把设备 daemon 绑定到同一个 Multica 内网地址。
4. 在 Agent 的本地 runtime 配置里注册 MCP server，或者在 Skill 中写明要调用的本地命令；改 PATH 或 MCP 配置后重启 daemon。
5. 在 Multica 里选择该设备的 runtime。任务正文和 Skill 会下发到设备，工具进程仍在设备本地执行。

推荐先做静态 Skill + 本地命令的最小闭环，再把确实需要结构化输入/输出的工具封装成 MCP。不要把 `agent-control` 的治理状态复制进 Multica 数据库，也不要让 NAS backend 直接执行任意工作区脚本。

## 升级边界

- 只更新 `agent-plugins` Skill：重新转换、校验、安装新版本；不用重建 Multica 镜像。
- 只更新 `agent-control` 工具或 MCP：更新各 Agent 设备，重启 daemon；不用重建 NAS 服务。
- 新增 Multica 的 API、调度语义、UI、内置 runtime 或插件执行能力：改 `work/multica` fork，跑测试，发布同版本 backend/web 镜像，再由部署工具升级。

## 当前未做的事情

- 没有把 `agent-control` 的工具自动安装到所有设备；这会涉及权限、版本和凭据边界，必须按设备显式配置。
- 没有把 agent-plugins 根目录脚本当成 Multica Plugin 执行；Private Plugin V1 明确不允许安装 Hook 或任意代码。
- 没有把这两个 GitHub 仓库变成 Multica 的远程 Plugin Registry；当前导入是本地、可审计、可回滚的 ZIP 流程。
