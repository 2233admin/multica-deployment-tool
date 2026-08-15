[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^https?://[^/]+(?::\d+)?$")]
    [string]$ServerUrl,
    [switch]$SkipInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ServerUrl = $ServerUrl.TrimEnd("/")
$cliPath = Join-Path $env:USERPROFILE ".multica\bin\multica.exe"

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

Write-Host "配置自托管服务: $ServerUrl" -ForegroundColor Cyan
Write-Host "浏览器会打开一次登录回调；完成后脚本会启动本机 daemon。" -ForegroundColor DarkCyan
& $cliPath setup self-host --server-url $ServerUrl --app-url $ServerUrl
if ($LASTEXITCODE -ne 0) {
    throw "Multica self-host 配置失败。检查 $ServerUrl/health 和注册状态。"
}

Write-Host "`n本机 daemon 状态:" -ForegroundColor Cyan
& $cliPath daemon status
if ($LASTEXITCODE -ne 0) { throw "daemon 未能启动。" }

Write-Host "`n客户端已接入。以后可直接运行: $cliPath daemon status" -ForegroundColor Green
