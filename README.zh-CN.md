# Multica 本地版一键部署包

这是给第一次部署 Multica 的 NAS/自有服务器用户准备的安装器和向导。它负责检查 SSH 与 Docker、部署 Multica、配置访问地址和登录方式、验证健康状态，并支持安全更新和重复部署。

当前仓库名 `multica-deployment-tool` 作为旧名保留。推荐的新仓库名候选是 `multica-local-deploy`；本阶段不直接重命名 GitHub 仓库。

## Multica、Plane、Gitea/GitHub、NetBird 的关系

- Multica：本地部署的主服务和任务入口。
- Plane：可选任务控制面，只通过用户填写的 URL 配置，不绑定固定 IP。
- Gitea OAuth：私网环境的一等登录方式。
- GitHub App：可选的仓库/事件集成，不替代 Multica 用户登录。
- 邮箱验证：可选的 SMTP 或 Resend 登录方式。
- NetBird：可选网络通道。它可以提供浏览器入口或服务间入口，但不是部署依赖。

工具不会把某一台参考 NAS 的 LAN/NetBird 地址写成产品默认值。浏览器访问、服务间访问和 OAuth 回调可以分别填写。

## 最短成功路径

先准备：

1. Synology Container Manager 或 Linux Docker 主机。
2. 可用 SSH 账号，并能直接运行 Docker 或使用免密 `sudo`。
3. 管理机上的 Python 3.9+、`ssh` 和 `scp`。
4. 一个浏览器可达的 LAN 地址、NetBird 地址或域名。

Linux/macOS：

```bash
git clone https://github.com/2233admin/multica-deployment-tool.git
cd multica-deployment-tool
python3 install.py
```

Windows：

```powershell
git clone https://github.com/2233admin/multica-deployment-tool.git
cd multica-deployment-tool
python .\install.py
```

也可以直接运行 `python3 multica_deploy.py wizard`。安装器第一行会明确显示“Multica 本地版一键部署包”，随后询问目标主机、地址、端口、目录和登录方式。

## 支持范围

管理机支持 Windows、Linux、macOS。远端目标支持 Synology Container Manager 和可通过 SSH 管理的 Linux Docker 主机。Windows Docker Desktop 可以作为本机构建机，但不是当前远端部署目标。

`--nas-host` 和 `--nas-ip` 是历史参数名：前者是 SSH 管理地址，后者是目标绑定/服务间地址。它们不代表固定 NAS，也不要求你使用 NAS。

## 向导中的地址和端口

向导会询问：

- SSH 管理主机和 SSH 端口；
- 目标绑定/服务间地址；
- Multica 浏览器入口端口；
- 浏览器访问 origin；
- 服务间访问 origin；
- OAuth 回调 origin；
- 可选 Plane origin；
- 持久化目录、Docker 路径、用户和用户组。

地址角色如下：

| 角色 | 用途 |
| --- | --- |
| SSH 管理地址 | 管理机连接目标主机、上传文件和执行 Compose |
| 目标绑定/服务间地址 | Caddy 绑定和目标主机自检 |
| 浏览器访问 origin | 用户打开 Multica、前端同源和 CORS |
| OAuth origin | 构造浏览器回调地址 |
| Plane origin | 可选任务控制面访问和可达性检查 |

可以用命令行覆盖这些值：

```text
--nas-host       SSH 管理地址（兼容旧参数名）
--nas-ip         目标绑定/服务间地址（兼容旧参数名）
--browser-url    浏览器访问 origin
--service-url    服务间访问 origin
--oauth-origin   OAuth 回调 origin
--plane-url      可选 Plane origin
--app-port       Multica 入口端口
```

部署目标上的 JWT、数据库密码、SMTP 密码、OAuth secret 和 GitHub 私钥不会保存到仓库或桌面端配置。管理机配置只保存非敏感部署设置，包括地址角色、端口、目录和镜像信息。

## 登录和回调

首次向导和后续菜单支持 Gitea OAuth、SMTP/Resend、测试固定验证码或稍后配置。Gitea 应用的回调地址是：

```text
<oauth-origin>/auth/callback
```

请把部署完成报告中的完整 URL 原样复制到 Gitea 应用。协议、主机、端口和路径都必须一致。使用 NetBird 或域名时，确认浏览器和 Gitea 都能访问该 origin。

## 一体化 onboarding 主流程

一次安装按以下顺序完成：

1. **安装**：在管理机运行 `install.py`，确认 Python、SSH、SCP 和目标 Docker 条件。
2. **服务器部署**：向导写入你提供的地址和端口，生成目标主机密钥，启动 Multica，并保留已有数据库和 `.env`。
3. **入口验证**：打开报告中的浏览器入口；运行 `status` 检查 `/health`、`/readyz`、容器和可选 Plane。
4. **登录授权**：内网自托管优先 Gitea OAuth；邮箱验证可作为回退。GitHub Device Flow 适合本地桌面授权，不需要把 GitHub 长期 token 手工复制到桌面端。
5. **桌面端连接**：当前版本手动输入服务器入口并完成浏览器登录；自动发现和一次性配对属于后续阶段。
6. **代码源连接**：登录 Multica 后，在代码源/集成设置中选择 GitHub、Gitea 或其他自托管 Git，完成仓库授权、选择和连接验证。部署包负责服务器地址与 GitHub App 基础配置，不替用户选择仓库或复制长期代码源密钥。

登录与代码源是两个独立步骤。Gitea OAuth 适合内网自托管登录；GitHub Device Flow 适合本地桌面授权；GitHub App 的安装和 webhook 适合仓库事件集成，但 webhook 必须使用公网 HTTPS origin。`github` 命令只保存 GitHub App 基础参数并打印 setup/webhook URL，不会自动创建 GitHub App；没有公网 HTTPS 时不能把 LAN/NetBird 地址当作 webhook 地址。

## 完成后的访问和健康检查

部署报告会输出浏览器入口、服务间入口、OAuth 回调、Plane 状态和 `/readyz` 结果。也可以运行：

```powershell
python .\multica_deploy.py status `
  --nas-host YOUR_SSH_HOST `
  --nas-ip YOUR_TARGET_ADDRESS `
  --app-port YOUR_APP_PORT
```

Plane 是可选的。使用向导输入 URL，或传 `--plane-url https://YOUR_PLANE_HOST`。未配置 Plane 不会阻止 Multica；报告会把“未配置”“可达”和“不可达”分开显示。

## 桌面端首次连接：当前可用与规划中

当前可用：部署结束后，复制报告中的浏览器入口，在桌面端或本地 CLI 的自托管设置中输入它，再通过浏览器完成登录。客户端引导脚本会检查 `/health`，配置官方 CLI，并验证 `auth status` 与 daemon 状态。

当前未实现：桌面端自动发现、二维码/设备码、服务器一次性配对码、配对撤销/重新配对和版本兼容握手。当前报告不会生成配对码，也不会向桌面端下发长期服务端密钥。

后续阶段应复用现有健康检查、浏览器登录和官方 CLI/daemon 接口，使用短时、单次消费的凭证；凭证应有过期、撤销、重新配对和最低版本策略，并能区分地址不可达、服务未启动、凭证过期、认证失败和版本不兼容。LAN 与 NetBird 应由桌面端选择或验证可用地址。

## 常见故障

| 故障 | 先做什么 |
| --- | --- |
| SSH 失败 | 运行 `ssh YOUR_SSH_HOST`，检查账号、密钥和端口，再运行 wizard。 |
| Docker 失败 | 在目标主机运行 `docker version`；Synology 检查 Container Manager 路径和 `--docker-path`。 |
| 页面打不开 | 检查浏览器 origin、Caddy 绑定地址、防火墙和端口，然后运行 `status`。 |
| `/readyz` 失败 | 运行 `doctor` 和 `logs --service backend`，确认服务间 origin 从目标主机可达。 |
| Gitea 回调失败 | 对照最终报告，逐字符检查协议、域名、端口和 `/auth/callback`。 |
| Plane 不可达 | 从管理机和目标主机分别测试 Plane URL；未配置 Plane 不影响 Multica。 |
| GitHub 本地授权失败 | 本地桌面授权使用 GitHub Device Flow；确认桌面端/官方 CLI 支持该流程，不要把 GitHub 长期 token 粘贴到部署配置。 |
| GitHub webhook 不工作 | 确认 `--public-url` 是公网 HTTPS origin，并检查 GitHub App setup/webhook URL；LAN/NetBird 地址不能替代公网 webhook。 |
| 代码源连接失败 | 先确认 Multica 登录，再检查代码源 provider、仓库权限、仓库选择和连接测试；不要把代码源 secret 写进本地部署 JSON。 |
| 桌面端失败 | 当前没有配对码；先区分 URL 不可达、服务未启动和浏览器登录失败。 |

## 更新和安全

重复运行 `deploy` 或 `upgrade` 会保留目标主机已有 `.env`、数据库和上传数据，只更新入口配置、Compose 和镜像。更新前运行 `doctor`；回退使用 `rollback`，不要删除数据库卷。

不要提交 `.env`、OAuth secret、SMTP 密码、GitHub 私钥或 SSH 私钥。对外提供访问时必须配置正确的 HTTPS 反向代理和证书，仅打开 HTTP 端口不等于完成 HTTPS。

## 高级部署与仓库迁移

自动化可以使用 `deploy`、`status`、`doctor`、`upgrade`、`rollback` 和 `build`。维护 Multica 源码时，可用 `build --source-dir YOUR_MULTICA_CHECKOUT` 在管理机本地构建，再把镜像上传到 NAS；NAS 不会从源码重新编译。源码改动后的常用快速更新命令是：

```powershell
python multica_deploy.py build --source-dir YOUR_MULTICA_CHECKOUT --image-tag local-20260817 --hot-update
```

`--hot-update` 只逐个替换 backend 和 frontend，等待 backend `/readyz` 后再替换 frontend；PostgreSQL、数据卷和 Caddy 保持运行。它是低停机快速更新，不是开发环境里的浏览器 HMR。Docker Desktop 会复用本机构建缓存，后续改动不需要 NAS 再拉依赖。

推荐仓库 slug：`multica-local-deploy`。备选：`multica-local-deployment`。真正重命名时需要同步 clone URL、安装命令、脚本和发布包中的链接，以及 issue/PR 链接；GitHub 旧仓库的 redirect 和旧名兼容标识也要保留。因此本阶段只统一产品标题，不直接重命名仓库。

提交改动前运行完整 Python 测试、CLI `--help`、安装器入口 smoke test 和无固定 IP/Plane 地址静态搜索。
