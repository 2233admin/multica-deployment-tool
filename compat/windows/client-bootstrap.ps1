[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^https?://[^/]+(?::\d+)?$")]
    [string]$ServerUrl,
    [string]$AppUrl,
    [string]$Profile,
    [string]$WorkspaceId,
    [string]$DeviceName,
    [string]$RuntimeName,
    [switch]$SkipInstall,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ServerUrl = $ServerUrl.TrimEnd("/")
$AppUrl = if ($AppUrl) { $AppUrl.TrimEnd("/") } else { $ServerUrl }
$cliPath = (Get-Command multica.exe -ErrorAction SilentlyContinue).Source
if (-not $cliPath) {
    $cliPath = Join-Path $env:USERPROFILE ".multica\bin\multica.exe"
}

try {
    Invoke-WebRequest -Uri "$ServerUrl/health" -UseBasicParsing -TimeoutSec 10 | Out-Null
}
catch {
    throw "无法访问 Multica 服务端 $ServerUrl/health。先确认 NAS 服务已启动、地址正确且网络可达。"
}

if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    if ($SkipInstall) {
        throw "找不到 Multica CLI: $cliPath。去掉 -SkipInstall，让脚本调用官方安装脚本。"
    }

    Write-Host "调用 Multica 官方 Windows 安装脚本..." -ForegroundColor Cyan
    $previousMode = $env:MULTICA_MODE
    Remove-Item Env:MULTICA_MODE -ErrorAction SilentlyContinue
    try {
        Invoke-RestMethod "https://raw.githubusercontent.com/multica-ai/multica/main/scripts/install.ps1" | Invoke-Expression
    }
    finally {
        if ($null -eq $previousMode) {
            Remove-Item Env:MULTICA_MODE -ErrorAction SilentlyContinue
        }
        else {
            $env:MULTICA_MODE = $previousMode
        }
    }
}

if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    throw "安装脚本结束后仍找不到 $cliPath。请重新打开 PowerShell 后重试。"
}

$profileArgs = @()
if ($Profile) { $profileArgs = @("--profile", $Profile) }

if (-not $VerifyOnly) {
    Write-Host "配置自托管服务: $ServerUrl" -ForegroundColor Cyan
    Write-Host "浏览器会打开一次登录回调；完成后脚本会启动本机 daemon。" -ForegroundColor DarkCyan
    if ($DeviceName) { $env:MULTICA_DAEMON_DEVICE_NAME = $DeviceName }
    if ($RuntimeName) { $env:MULTICA_AGENT_RUNTIME_NAME = $RuntimeName }
    & $cliPath setup self-host @profileArgs --server-url $ServerUrl --app-url $AppUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Multica self-host 配置失败。检查 $ServerUrl/health 和注册状态。"
    }
    if ($WorkspaceId) {
        & $cliPath workspace switch $WorkspaceId @profileArgs
        if ($LASTEXITCODE -ne 0) { throw "Multica workspace 绑定失败: $WorkspaceId" }
    }
}

Write-Host "`n认证状态:" -ForegroundColor Cyan
& $cliPath auth status @profileArgs
if ($LASTEXITCODE -ne 0) { throw "Multica 登录状态无效。" }

if ($WorkspaceId) {
    Write-Host "`nWorkspace 绑定（JSON）:" -ForegroundColor Cyan
    & $cliPath workspace get $WorkspaceId @profileArgs --output json
    if ($LASTEXITCODE -ne 0) { throw "Multica workspace 无法读取: $WorkspaceId" }
}

Write-Host "`n本机 daemon 状态（JSON）:" -ForegroundColor Cyan
& $cliPath daemon status @profileArgs --output json
if ($LASTEXITCODE -ne 0) { throw "daemon 未能启动或未连接服务端。" }

Write-Host "`n客户端已接入。以后可直接运行: $cliPath daemon status" -ForegroundColor Green
