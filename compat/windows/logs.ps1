[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NasHost,
    [ValidateRange(0, 65535)]
    [int]$SshPort = 0,
    [ValidateSet("backend", "frontend", "postgres", "caddy")]
    [string]$Service = "backend",
    [ValidatePattern("^\d+[smhd]$")]
    [string]$Since = "15m",
    [ValidatePattern("^/[A-Za-z0-9._/-]+$")]
    [string]$NasTarget = "/opt/multica",
    [string]$DockerPath = "docker"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$command = "cd '$NasTarget' && sudo -n '$dockerPath' compose --env-file .env -f docker-compose.selfhost.yml -f docker-compose.nas.yml logs --since '$Since' --tail 200 '$Service' | sed -E 's/(Verification code[^0-9]*)([0-9]{6})/\1******/g'"
$sshOptions = @()
if ($SshPort -gt 0) { $sshOptions = @("-p", $SshPort.ToString()) }

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "找不到 ssh。"
}

Write-Host "显示 $Service 最近 $Since 的日志（验证码已脱敏）..." -ForegroundColor Cyan
& ssh @sshOptions $NasHost $command
if ($LASTEXITCODE -ne 0) { throw "读取日志失败。" }
