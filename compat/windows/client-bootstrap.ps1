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
    # LAN/NetBird health checks must not be sent through a workstation proxy.
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(10)
    $response = $client.GetAsync("$ServerUrl/health").GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        throw "HTTP $([int]$response.StatusCode)"
    }
}
catch {
    throw "Cannot reach Multica server $ServerUrl/health. Confirm the NAS service is running, the URL is correct, and the network is reachable."
}
finally {
    if ($client) { $client.Dispose() }
    if ($handler) { $handler.Dispose() }
}

if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    if ($SkipInstall) {
        throw "Multica CLI not found: $cliPath. Remove -SkipInstall so the official installer can run."
    }

    Write-Host "Running the official Multica Windows installer..." -ForegroundColor Cyan
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
    throw "The installer finished but $cliPath was not found. Reopen PowerShell and try again."
}

$profileArgs = @()
if ($Profile) { $profileArgs = @("--profile", $Profile) }

if (-not $VerifyOnly) {
    Write-Host "Configuring self-hosted service: $ServerUrl" -ForegroundColor Cyan
    Write-Host "A browser will open for one-time login; the local daemon starts after login." -ForegroundColor DarkCyan
    if ($DeviceName) { $env:MULTICA_DAEMON_DEVICE_NAME = $DeviceName }
    if ($RuntimeName) { $env:MULTICA_AGENT_RUNTIME_NAME = $RuntimeName }
    & $cliPath setup self-host @profileArgs --server-url $ServerUrl --app-url $AppUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Multica self-host setup failed. Check $ServerUrl/health and the registration state."
    }
    if ($WorkspaceId) {
        & $cliPath workspace switch $WorkspaceId @profileArgs
        if ($LASTEXITCODE -ne 0) { throw "Multica workspace binding failed: $WorkspaceId" }
    }
}

Write-Host "`nAuthentication status:" -ForegroundColor Cyan
& $cliPath auth status @profileArgs
if ($LASTEXITCODE -ne 0) { throw "Multica authentication is invalid." }

if ($WorkspaceId) {
    Write-Host "`nWorkspace binding (JSON):" -ForegroundColor Cyan
    & $cliPath workspace get $WorkspaceId @profileArgs --output json
    if ($LASTEXITCODE -ne 0) { throw "Unable to read Multica workspace: $WorkspaceId" }
}

Write-Host "`nLocal daemon status (JSON):" -ForegroundColor Cyan
& $cliPath daemon status @profileArgs --output json
if ($LASTEXITCODE -ne 0) { throw "The daemon did not start or is not connected to the server." }

Write-Host "`nClient connected. Run this later to check it: $cliPath daemon status" -ForegroundColor Green
