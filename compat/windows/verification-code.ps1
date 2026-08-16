[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NasHost,
    [ValidateRange(0, 65535)]
    [int]$SshPort = 0,
    [ValidatePattern("^\d+[smhd]$")]
    [string]$Since = "15m",
    [string]$DockerPath = "docker"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$sshOptions = @()
if ($SshPort -gt 0) { $sshOptions = @("-p", $SshPort.ToString()) }

Write-Warning "下面可能包含登录验证码；只在你自己的终端查看，不要复制到聊天、工单或日志。"
$command = "sudo -n '$dockerPath' logs --since '$Since' multica-backend-1 2>&1 | grep 'Verification code' | tail -n 5"
& ssh @sshOptions $NasHost $command
if ($LASTEXITCODE -ne 0) {
    Write-Host "没有找到验证码。可能已过期、服务名变化，或邮件服务已配置。" -ForegroundColor Yellow
    exit 1
}
