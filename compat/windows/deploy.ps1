[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NasHost,
    [ValidateRange(0, 65535)]
    [int]$SshPort = 0,
    [ValidatePattern("^(?:\d{1,3}\.){3}\d{1,3}$")]
    [Parameter(Mandatory = $true)]
    [string]$NasIp,
    [ValidatePattern("^/[A-Za-z0-9._/-]+$")]
    [string]$NasTarget = "/opt/multica",
    [ValidatePattern("^v\d+\.\d+\.\d+$")]
    [string]$ImageTag = "v0.4.28",
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$AppPort,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 3011,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 3012,
    [ValidatePattern("^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")]
    [string]$NetworkSubnet = "10.253.0.0/24",
    [ValidatePattern("^[A-Za-z0-9._-]+$")]
    [string]$NasOwner = "multica",
    [ValidatePattern("^[A-Za-z0-9._-]+$")]
    [string]$NasGroup = "multica",
    [string]$DockerPath = "docker",
    [switch]$NoPull
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$packageRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$requiredFiles = @(
    "docker-compose.selfhost.yml",
    "docker-compose.nas.yml",
    "Caddyfile",
    ".env.template"
)

foreach ($file in $requiredFiles) {
    $path = Join-Path $packageRoot $file
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "部署包缺少文件: $path"
    }
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "找不到 ssh。请安装 OpenSSH Client，并确认 ssh $NasHost 可以登录 NAS。"
}
if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw "找不到 scp。请安装 OpenSSH Client。"
}

$sshOptions = @()
$scpOptions = @()
if ($SshPort -gt 0) {
    $sshOptions = @("-p", $SshPort.ToString())
    $scpOptions = @("-P", $SshPort.ToString())
}

function Invoke-Nas {
    param([Parameter(Mandatory)][string]$Command)

    & ssh @sshOptions $NasHost $Command
    if ($LASTEXITCODE -ne 0) {
        throw "NAS 命令失败（退出码 $LASTEXITCODE）: $Command"
    }
}

function Copy-NasFile {
    param([Parameter(Mandatory)][string]$LocalPath)

    # Synology 的 SFTP 子系统在部分 Container Manager 环境不可用；-O
    # 强制 scp 协议，避免 OpenSSH 新版本默认切到 SFTP 后部署失败。
    & scp -O @scpOptions -q $LocalPath "$NasHost`:$NasTarget/"
    if ($LASTEXITCODE -ne 0) {
        throw "复制到 NAS 失败: $LocalPath"
    }
}

Write-Host "[1/6] 检查 NAS SSH 与 Docker 权限..." -ForegroundColor Cyan
Invoke-Nas "test -x '$dockerPath' && sudo -n '$dockerPath' version --format '{{.Server.Version}}' >/dev/null"

Write-Host "[2/6] 创建受限部署目录..." -ForegroundColor Cyan
Invoke-Nas "sudo -n install -d -m 0750 -o '$NasOwner' -g '$NasGroup' '$NasTarget'"

Write-Host "[3/6] 上传 Compose、Caddy 和环境模板..." -ForegroundColor Cyan
foreach ($file in $requiredFiles) {
    Copy-NasFile (Join-Path $packageRoot $file)
}

# Render the Caddy address from the operator's parameters. The checked-in
# Caddyfile contains only a placeholder, so the same package is usable on any
# private NAS without hand-editing it.
$caddyTemplate = Get-Content -LiteralPath (Join-Path $packageRoot "Caddyfile") -Raw
$caddyRendered = [regex]::Replace(
    $caddyTemplate,
    "(?m)^http://[^:\r\n]+:\d+ \{",
    "http://$NasIp`:$AppPort {"
)
$tempCaddy = Join-Path ([IO.Path]::GetTempPath()) ("multica-caddy-{0}.Caddyfile" -f ([guid]::NewGuid().ToString("N")))
[IO.File]::WriteAllText($tempCaddy, $caddyRendered, [Text.UTF8Encoding]::new($false))
try {
    Copy-NasFile $tempCaddy
    Invoke-Nas "mv '$NasTarget/$(Split-Path -Leaf $tempCaddy)' '$NasTarget/Caddyfile'"
}
finally {
    Remove-Item -LiteralPath $tempCaddy -Force -ErrorAction SilentlyContinue
}

Write-Host "[4/6] 初始化或更新非敏感配置（保留 NAS 上已有密钥）..." -ForegroundColor Cyan
$remoteInit = @'
set -eu
target='__TARGET__'
env_file="$target/.env"
if [ ! -f "$env_file" ]; then
  umask 077
  cp "$target/.env.template" "$env_file"
  printf '\nJWT_SECRET=%s\n' "$(openssl rand -hex 32)" >> "$env_file"
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> "$env_file"
  printf 'MULTICA_VCS_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> "$env_file"
fi
upsert() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}
upsert MULTICA_IMAGE_TAG '__IMAGE_TAG__'
upsert BACKEND_PORT '__BACKEND_PORT__'
upsert FRONTEND_PORT '__FRONTEND_PORT__'
upsert FRONTEND_ORIGIN 'http://__NAS_IP__:__APP_PORT__'
upsert CORS_ALLOWED_ORIGINS 'http://__NAS_IP__:__APP_PORT__'
upsert MULTICA_APP_URL 'http://__NAS_IP__:__APP_PORT__'
chmod 600 "$env_file"
chmod 640 "$target/docker-compose.selfhost.yml" "$target/docker-compose.nas.yml" "$target/Caddyfile" "$target/.env.template"
sudo -n chown __OWNER__:__GROUP__ "$env_file" "$target/docker-compose.selfhost.yml" "$target/docker-compose.nas.yml" "$target/Caddyfile" "$target/.env.template"
'@
$remoteInit = $remoteInit.Replace("__TARGET__", $NasTarget)
$remoteInit = $remoteInit.Replace("__IMAGE_TAG__", $ImageTag)
$remoteInit = $remoteInit.Replace("__BACKEND_PORT__", $BackendPort.ToString())
$remoteInit = $remoteInit.Replace("__FRONTEND_PORT__", $FrontendPort.ToString())
$remoteInit = $remoteInit.Replace("__NAS_IP__", $NasIp)
$remoteInit = $remoteInit.Replace("__APP_PORT__", $AppPort.ToString())
$remoteInit = $remoteInit.Replace("__OWNER__", $NasOwner)
$remoteInit = $remoteInit.Replace("__GROUP__", $NasGroup)
Invoke-Nas $remoteInit

# Apply the requested explicit Docker subnet after upload. This is deliberately
# outside .env because Compose reads it from the NAS override file.
$networkCommand = "sed -i -E 's|subnet: [0-9.]+/[0-9]+|subnet: $NetworkSubnet|' '$NasTarget/docker-compose.nas.yml'"
Invoke-Nas $networkCommand

$composeCommand = "cd '$NasTarget' && sudo -n '$dockerPath' compose --env-file .env -f docker-compose.selfhost.yml -f docker-compose.nas.yml"

Write-Host "[5/6] 校验 Compose 与 Caddy 配置..." -ForegroundColor Cyan
Invoke-Nas "$composeCommand config --quiet"
Invoke-Nas "sudo -n '$dockerPath' run --rm --network host -v '$NasTarget/Caddyfile:/etc/caddy/Caddyfile:ro' caddy:2.10-alpine caddy validate --config /etc/caddy/Caddyfile"

if (-not $NoPull) {
    Write-Host "拉取固定版本镜像 $ImageTag ..." -ForegroundColor DarkCyan
    Invoke-Nas "$composeCommand pull"
}

Write-Host "[6/6] 启动服务并等待数据库迁移完成..." -ForegroundColor Cyan
Invoke-Nas "$composeCommand up -d --remove-orphans"
$healthCommand = @"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 5 http://$NasIp`:$AppPort/readyz; then exit 0; fi
  sleep 3
done
exit 1
"@
Invoke-Nas $healthCommand

Write-Host "`n部署完成。" -ForegroundColor Green
Write-Host "地址: http://$NasIp`:$AppPort"
Write-Host "状态: .\status.ps1 -NasHost $NasHost -NasIp $NasIp"
Write-Host "日志: .\logs.ps1 -NasHost $NasHost"
Write-Host "升级: .\deploy.ps1 -NasHost $NasHost -NasIp $NasIp -ImageTag <版本>"
