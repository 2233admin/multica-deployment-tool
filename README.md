# Multica 本地版一键部署包

这是一个给 NAS 和自有 Linux 服务器使用的 Multica 本地版一键部署包。它通过安装器和向导完成目标主机检查、Docker Compose 部署、地址配置、登录配置、健康检查和安全更新。

当前仓库名是 `multica-deployment-tool`，作为旧名兼容保留。更直观的仓库名候选是 `multica-local-deploy`；本阶段不直接重命名 GitHub 仓库。

## 这个项目解决什么问题

你不需要手工拼接 Compose 命令，也不需要把某台 NAS 的 IP 写进配置。向导会让你明确填写：

- SSH 管理地址和 SSH 端口；
- 目标主机用于绑定和服务间自检的地址；
- 浏览器访问地址：LAN、NetBird 或域名；
- Multica、backend、frontend 的端口；
- OAuth 回调 origin；
- 可选的 Plane 任务控制面 URL；
- 远端持久化目录、Docker 路径、用户和用户组。

Multica 是要部署的本地服务。Plane 是可选的任务控制面，可以和 Multica 共用一台主机，也可以是另一台已有服务；工具只接受 URL，不假设固定 IP。Gitea OAuth、GitHub App 和邮箱验证是可选的登录或集成方式。NetBird 是可选的网络通道，不是部署依赖。

## 最短成功路径

准备以下条件：

1. 一台运行 Synology Container Manager 或 Linux Docker 的目标主机。
2. 一个可以通过 SSH 登录的账号，并能直接运行 Docker 或使用免密 `sudo`。
3. 管理机上的 Python 3.9+、`ssh` 和 `scp`。
4. 一个从管理机和浏览器都能到达的 Multica 地址。可以是 LAN 地址、NetBird 地址或域名。

然后运行：

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

安装器会先显示“Multica 本地版一键部署包”，随后启动向导。首次部署请按提示填写地址、端口、目录和登录方式。地址没有产品默认值；示例中的 `YOUR_*` 都必须替换成你自己的值。

不使用安装器时，可以直接运行：

```bash
python3 multica_deploy.py wizard
```

## 支持的平台和目标

| 角色 | 支持范围 |
| --- | --- |
| 管理机 | Windows、Linux、macOS |
| 远端目标 | Synology Container Manager 或可通过 SSH 管理的 Linux Docker 主机 |
| 本地构建机 | Windows/macOS Docker Desktop 或 Linux Docker |

远端流程需要 POSIX shell、`curl`、`sed`、Compose 和 Docker。Windows Docker Desktop 适合作为本机构建机；Windows Docker 主机不是当前远端部署目标。

命令中的 `--nas-host`、`--nas-ip` 是历史兼容参数名。它们不表示必须使用 NAS，也不包含任何固定 NAS 地址：`--nas-host` 是 SSH 管理地址，`--nas-ip` 是目标绑定/服务间地址。

## 向导会询问什么

必须由你提供的值包括 SSH 管理地址、目标可达地址或 URL、入口端口、远端目录权限，以及你选择的登录提供方凭据。使用 NetBird 时，`--nas-ip` 应填写目标的 NetBird IPv4，并加上 `--netbird`；不使用 NetBird 时不要勾选它。

向导会把以下非敏感配置保存到当前用户配置目录，供下一次 `status`、`upgrade` 和 `wizard` 使用：

- SSH 管理地址、SSH 端口；
- 绑定/服务地址、浏览器 origin、服务间 origin；
- OAuth origin、可选 Plane URL；
- Multica 和 Compose 端口；
- 远端目录、Docker 路径、用户组和镜像设置。

部署目标上的 JWT、数据库密码、SMTP 密码、OAuth secret、GitHub 私钥等由目标主机生成或输入，保存在目标运行时配置中，不写进仓库，也不会写进桌面端配置。

## 登录和 OAuth

可以在首次向导或后续菜单中选择 Gitea OAuth、SMTP/Resend 邮箱验证、测试固定验证码，或稍后配置。Gitea OAuth 仍是私网部署的一等登录路径；GitHub App 主要用于 GitHub 仓库/事件集成，不会替代 Multica 用户登录。

OAuth 回调由你输入的 origin 和固定回调路径构造：

```text
<oauth-origin>/auth/callback
```

例如使用域名时，把同一个 origin 配置到 Gitea OAuth 应用；使用 NetBird 时，确保浏览器和 Gitea 都能访问该 NetBird 地址。需要让浏览器入口、服务间入口和 OAuth origin 不同时，分别填写 `--browser-url`、`--service-url` 和 `--oauth-origin`。

## 一体化 onboarding 主流程

把一次安装理解为下面六个阶段。每个阶段都要完成后再进入下一阶段：

1. **安装**：在管理机运行安装器，确认 Python、SSH、SCP 和目标 Docker 条件。
2. **服务器部署**：向导写入你填写的地址和端口，生成目标主机密钥，启动 Multica，并保留已有数据库和 `.env`。
3. **入口验证**：打开报告中的浏览器入口；运行 `status` 确认 `/health`、`/readyz`、容器状态和可选 Plane 状态。
4. **登录授权**：私网自托管优先使用 Gitea OAuth；邮箱验证可作为本地回退。GitHub Device Flow 适合本地授权桌面端，避免把 GitHub 长期 token 手工复制到桌面端。
5. **桌面端连接**：部署完成后，工具读取 NAS 上实际运行的 backend/frontend runtime 版本，并让 Windows 桌面端 CLI/daemon 跟随同一个正式版本。
6. **代码源连接**：登录后在 Multica 的代码源/集成设置中选择 GitHub、Gitea 或其他自托管 Git，完成仓库授权、仓库选择和连接验证。部署包负责服务器地址与 GitHub App 基础配置，不替用户选择仓库或复制长期代码源密钥。

代码源与用户登录是两件事。Gitea OAuth 适合内网自托管登录；GitHub Device Flow 适合本地桌面授权；GitHub App 的安装和 webhook 适合仓库事件集成，但 webhook 必须有公网 HTTPS origin。当前 `github` 命令会保存 GitHub App 基础参数并打印 setup/webhook URL，不会自动创建 GitHub App，也不会在没有公网 HTTPS 时伪造 webhook 成功。

## 部署完成后的访问和验收

部署完成报告会打印：

- Multica 浏览器入口；
- 目标主机使用的服务间健康检查入口；
- OAuth 回调地址；
- 可选 Plane URL 和一次可达性检查；
- `/readyz` 结果。

也可以用显式参数重现一台已有安装。下面的地址和端口只是命令占位符，不是产品默认值：

```bash
python3 multica_deploy.py deploy \
  --nas-host YOUR_SSH_HOST \
  --nas-ip YOUR_TARGET_ADDRESS \
  --browser-url http://YOUR_BROWSER_HOST:YOUR_APP_PORT \
  --service-url http://YOUR_SERVICE_HOST:YOUR_APP_PORT \
  --oauth-origin http://YOUR_BROWSER_HOST:YOUR_APP_PORT \
  --app-port YOUR_APP_PORT
```

部署后可运行：

```bash
python3 multica_deploy.py status \
  --nas-host YOUR_SSH_HOST \
  --nas-ip YOUR_TARGET_ADDRESS \
  --app-port YOUR_APP_PORT
```

Plane 是可选项。通过向导输入 URL，或使用 `--plane-url https://YOUR_PLANE_HOST`；未配置 Plane 不会阻止 Multica 部署。工具会在 `status` 和最终报告中区分“未配置”“可达”和“不可达”。

## 桌面端首次连接

当前可用流程是：`deploy`/`upgrade` 默认使用官方滚动标签 `latest`；服务启动并通过 `/readyz` 后，工具从运行中容器的 OCI version label 检测真实版本，再从官方 release 下载同版本 Windows 安装包，保留本机 token/workspace，并把 CLI daemon 绑定到当前自托管地址。若 backend/frontend 版本标签不一致，或是没有正式版本标签的 `dev`/自定义镜像，桌面同步会安全跳过，不会混用版本。需要回滚或人工指定时可使用 `--image-tag vX.Y.Z` 或 `--desktop-version vX.Y.Z`。

当前仍未实现服务器生成一次性配对码、二维码/设备码授权和配对撤销；首次登录仍需完成官方登录流程。版本同步和 endpoint 绑定不依赖把长期服务端密钥写入部署配置。

后续配对阶段应复用现有健康检查、浏览器登录和官方 CLI/daemon seam，采用短时一次性凭证，明确过期、单次消费、撤销/重新配对、失败原因和最低兼容版本，并覆盖 LAN 与 NetBird 地址选择。配对完成后只下发非敏感运行配置和短期授权结果。

## 常见失败

| 现象 | 排查动作 |
| --- | --- |
| SSH 连接失败 | 先运行 `ssh YOUR_SSH_HOST`；确认 SSH 端口、账号和密钥，再运行 `wizard` 重新保存配置。 |
| Docker 不可用 | 在目标主机运行 `docker version`；Synology 用户确认 Container Manager 路径和 `--docker-path`。 |
| 页面打不开 | 检查浏览器 origin 是否可达、Caddy 绑定地址、防火墙和端口策略；再运行 `status`。 |
| `/readyz` 失败 | 运行 `doctor` 和 `logs --service backend`；确认目标主机能访问服务间 origin。 |
| Gitea 回调失败 | 对照最终报告的完整回调 URL；检查 Gitea 应用的协议、域名、端口和路径是否完全一致。 |
| Plane 不可达 | 确认 Plane URL 的协议和端口；从管理机和目标主机分别测试该 URL。Plane 未配置时不会影响 Multica。 |
| 登录失败 | 确认选中的登录方式已配置；Gitea 看 issuer、Client ID、secret 和回调，邮箱登录看 SMTP/Resend。 |
| GitHub 本地授权失败 | 本地桌面授权应使用 GitHub Device Flow；确认桌面端/官方 CLI 支持该流程，不要把 GitHub 长期 token 粘贴到部署配置。 |
| GitHub webhook 不工作 | 确认 `--public-url` 是公网 HTTPS origin，并检查 GitHub App setup/webhook URL；LAN 或 NetBird 地址不能替代公网 webhook。 |
| 代码源连接失败 | 先确认 Multica 登录成功，再在代码源设置中检查 provider、仓库权限、仓库选择和连接测试；不要把代码源 secret 写进本地部署 JSON。 |
| 桌面端连接失败 | 区分地址不可达、服务未启动和登录失败；当前没有配对码，不能按“配对码过期”排查。 |

## 更新、重复部署和安全

重复运行 `deploy` 或 `upgrade` 会保留目标主机已有 `.env`、数据库和上传数据，只更新 Compose、入口配置和镜像。默认 `latest` 用于滚动跟随官方 runtime；工具会在服务真正启动后读取运行中容器的版本，再同步同版本桌面 CLI。更新前可运行 `doctor`；需要回退时使用 `rollback`，不要删除数据库卷。

管理机配置只保存非敏感部署设置。不要提交 `.env`、OAuth secret、SMTP 密码、GitHub 私钥或 SSH 私钥。对外暴露前请使用正确的 HTTPS 反向代理和证书；仅打开 HTTP 端口不等于完成 HTTPS 配置。

## 高级参数和贡献

自动化场景可以直接使用 `deploy`、`status`、`doctor`、`upgrade`、`rollback` 和 `build`。常用地址参数是：

```text
--nas-host          SSH 管理地址（兼容旧参数名）
--nas-ip            目标绑定/服务间地址（兼容旧参数名）
--browser-url       浏览器访问 origin
--service-url       服务间访问 origin
--oauth-origin      OAuth 回调 origin
--plane-url         可选 Plane origin
--app-port          Multica 浏览器入口端口
```

如果你维护 Multica 源码，可以用 `build --source-dir YOUR_MULTICA_CHECKOUT` 在管理机本地构建，再把镜像上传到 NAS；NAS 不会从源码重新编译。源码改动后的快速更新命令是：

```text
python multica_deploy.py build --source-dir YOUR_MULTICA_CHECKOUT --image-tag local-20260817 --hot-update
```

`--hot-update` 只逐个替换 backend 和 frontend，等待 backend `/readyz` 后再替换 frontend；PostgreSQL、数据卷和 Caddy 保持运行。它是低停机快速更新，不是开发环境里的浏览器 HMR。Docker Desktop 会复用本机构建缓存，后续改动不需要 NAS 再拉依赖。提交改动前运行完整 Python 测试、CLI help smoke test 和地址静态搜索。

仓库名迁移候选首选 `multica-local-deploy`，备选 `multica-local-deployment`。真正迁移时需要同步 clone URL、安装文档、发布 ZIP、脚本中的仓库链接和 issue/PR 链接，并保留旧仓库的 redirect；在 GitHub 重命名之前，旧 URL 才是唯一兼容入口。

## License

MIT。见许可证文件。
