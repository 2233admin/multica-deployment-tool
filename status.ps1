[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NasHost,
    [ValidateRange(0, 65535)]
    [int]$SshPort = 0,
    [ValidatePattern("^(?:\d{1,3}\.){3}\d{1,3}$")]
    [Parameter(Mandatory = $true)]
    [string]$NasIp,
    [ValidateRange(1, 65535)]
    [int]$AppPort = 3010,
    [ValidatePattern("^/[A-Za-z0-9._/-]+$")]
    [string]$NasTarget = "/opt/multica",
    [string]$DockerPath = "docker"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$compose = "cd '$NasTarget' && sudo -n '$dockerPath' compose --env-file .env -f docker-compose.selfhost.yml -f docker-compose.nas.yml"
$sshOptions = @()
if ($SshPort -gt 0) { $sshOptions = @("-p", $SshPort.ToString()) }

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "找不到 ssh。"
}

Write-Host "Multica NAS 状态" -ForegroundColor Cyan
Write-Host "地址: http://$NasIp`:$AppPort"
Write-Host "`n容器:" -ForegroundColor Cyan
& ssh @sshOptions $NasHost "$compose ps"
if ($LASTEXITCODE -ne 0) { throw "读取 compose 状态失败。" }

Write-Host "`n就绪检查:" -ForegroundColor Cyan
& ssh @sshOptions $NasHost "curl -fsS --max-time 5 http://$NasIp`:$AppPort/readyz"
if ($LASTEXITCODE -ne 0) {
    throw "readyz 检查失败。先运行 .\logs.ps1 -NasHost $NasHost 查看脱敏日志。"
}
Write-Host ""

try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "http://$NasIp`:$AppPort/health" -TimeoutSec 5
    Write-Host "Windows 端 HTTP: $($response.StatusCode) $($response.StatusDescription)" -ForegroundColor Green
}
catch {
    Write-Warning "NAS 自检通过，但当前 Windows 访问不到 http://$NasIp`:$AppPort。检查 VLAN、防火墙或路由。"
}
