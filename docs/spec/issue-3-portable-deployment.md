# Multica 本地版一键部署包：可移植部署与桌面端连接

这是 **Multica 本地版一键部署包** 的可执行规格。它面向第一次在 NAS 或自有 Linux 服务器上部署 Multica 的用户，负责目标检查、地址配置、部署、登录配置和验收。当前 GitHub 仓库名 `multica-deployment-tool` 作为兼容旧名保留；推荐的新 slug 是 `multica-local-deploy`，本 issue 不直接重命名仓库。

## 目标

让用户通过安装器和 wizard 完成一条低认知负担的路径：准备 SSH/Docker 目标、填写自己的 LAN/NetBird/域名和端口、部署 Multica、配置 Gitea/GitHub/邮箱登录、可选连接 Plane，并得到实际可访问地址和健康结果。

当前参考 NAS 的地址和端口只能作为测试夹具，不能成为产品默认值。Plane 是可选 URL 集成，不是固定 IP 的第二台服务。

## 地址模型

目标配置必须区分：

1. SSH 管理地址：管理机连接目标主机、上传文件和执行 Docker/Compose。
2. 目标绑定/服务间地址：Caddy 绑定、目标主机自检和服务到服务请求。
3. 浏览器访问 origin：用户打开 Multica、前端同源和 CORS 使用的地址。
4. OAuth 回调 origin：构造 `<origin>/auth/callback` 的浏览器地址。
5. 可选 Plane origin：Plane 任务控制面 URL；可以与 Multica 共主机，也可以是已有外部服务。

当一个地址足够时可以复用，但 wizard 和非交互 CLI 必须允许覆盖。不得写死当前 LAN IP、NetBird IP、Plane IP 或 Plane 端口。

## 第一阶段验收

- 安装器和 wizard 的标题明确写出“Multica 本地版一键部署包”。
- 首次运行能收集 SSH 管理地址、目标地址、浏览器 origin、服务间 origin、OAuth origin、Multica 入口端口和可选 Plane URL。
- CLI 能通过参数覆盖这些值，并将非敏感配置序列化到受保护的本地配置文件。
- Caddy、健康检查、前端 origin、CORS、Multica app URL 和 Gitea 默认回调使用正确的地址角色。
- `status`、`doctor` 和部署完成报告分别显示浏览器入口、服务间入口、OAuth 回调和 Plane 状态。
- Gitea OAuth、Multica 登录、已有 `.env`、数据库和重复部署路径不被破坏。
- 测试覆盖 URL 构造、端口校验、回调构造、配置序列化、Plane 可选性和固定参考地址静态搜索。
- 文档面向陌生读者，明确当前可用流程和未实现的桌面端自动配对。

## 登录与集成边界

Gitea OAuth 是私网登录的一等路径；GitHub App 是可选仓库/事件集成；SMTP/Resend 是邮箱登录路径。部署工具不得把 OAuth secret、数据库密码、JWT、GitHub 私钥或 SSH 私钥写入仓库、普通导出配置或桌面端。

Plane 只要求 URL 配置和可达性展示。没有 Plane 时 Multica 部署仍必须成功；Plane 的联动 API/auth 合同需要在后续阶段由双方服务确定。

## 一体化 onboarding 主流程

验收路径必须是：安装管理工具 → 部署服务器 → 验证浏览器入口和健康状态 → 完成登录授权 → 连接桌面端 → 在 Multica 中选择 GitHub、Gitea 或其他自托管 Git 代码源 → 完成仓库授权、选择和连接验证。

其中登录和代码源不能混为一个 secret 流程：Gitea OAuth 适合内网自托管登录；GitHub Device Flow 适合本地桌面授权，避免复制 GitHub 长期 token；GitHub App 适合仓库事件和 webhook，但 webhook 必须使用公网 HTTPS origin。没有公网 HTTPS 时，LAN/NetBird 地址只能用于本地访问和登录，不能被报告为 GitHub webhook 已验证。

第一阶段部署工具已经负责服务器地址角色、Multica/Gitea 登录配置、可选 Plane URL 和 GitHub App 基础参数。代码源 provider/仓库选择和最终连接验证应在 Multica 集成设置或官方 CLI 中完成；部署工具必须在报告中说明这一步，而不能把“容器健康”写成“代码源已连接”。

## 后续阶段：桌面端与本地服务器配对

当前客户端 seam 是：桌面端/本地 CLI 输入自托管服务器 URL，访问 `/health`，通过浏览器登录，配置官方 CLI，然后检查 `auth status` 和 `daemon status --output json`。当前没有配对码、二维码、设备码、撤销或版本握手接口。

后续实现必须先定义版本化配对协议，不得在部署工具中臆造新的长期密钥分发机制。协议至少应满足：

- 桌面端可选择并验证 LAN、NetBird 或域名地址；
- 服务端生成高熵、短时、单次消费的 pairing id/配对码；
- 配对必须经过已登录浏览器确认；
- bootstrap credential 只能交换最小短期设备会话，不下发服务端长期密钥；
- 配对码和 bootstrap credential 过期、成功交换或失败次数超限后失效；
- 已绑定设备可撤销；重新配对必须生成新身份，旧会话失效；
- 握手包含协议版本、桌面端版本和能力，能区分需升级桌面端、需升级服务器和不兼容；
- 错误报告区分地址不可达、服务未启动、配对码过期、用户拒绝、认证失败和版本不兼容；
- 部署报告提供配对入口/状态，但当前版本只能报告“尚未实现自动配对”。

配对协议的端点命名、载荷和 token 交换必须在 Multica 服务端与桌面端共同评审后再实现。现状、生命周期、撤销、兼容性和测试要求见 `docs/desktop-pairing-v1.md`。

## 测试与验收

测试必须覆盖：

- 配置模型和 URL 角色分离；
- LAN、NetBird、域名及自定义端口；
- Caddy 和 OAuth callback 构造；
- 配置 JSON 序列化与秘密排除；
- 无固定参考 IP/Plane 地址的静态检查；
- SSH/Docker/存储/端口 preflight 的成功和失败原因；
- Gitea、邮箱和可选 Plane 配置；
- 健康检查、重复部署、持久化数据和现有登录路径；
- 桌面端配对的地址选择、TTL、单次消费、撤销/重新配对、版本兼容和错误分类。

## 非目标

- 重命名 GitHub 仓库；
- 替换 Gitea、GitHub、Plane 或 NetBird；
- 把 Plane 强制安装成 Multica 的固定依赖；
- 把长期服务端密钥复制到桌面端；
- 在没有服务端/桌面端协议评审前实现新的配对 API；
- 破坏现有 Gitea OAuth、Multica 登录、数据库和重复部署数据。
