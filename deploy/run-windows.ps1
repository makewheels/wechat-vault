# Scheduled incremental sync for the Windows WeChat account.
# Secrets come from Infisical (cloud credentials + this device's keys) and are only placed in this process's
# environment. The run is logged under %LOCALAPPDATA%\wechat-vault\logs.
param(
    [string] $AccountId = 'wxid_vizj1s0qmxl922',
    [string] $DeviceId = 'windows',
    [string] $AccountRoot = (Join-Path $env:USERPROFILE 'Documents\xwechat_files\wxid_vizj1s0qmxl922_2b0d'),
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra = @()
)

$ErrorActionPreference = 'Continue'
if ($AccountId -notmatch '^[A-Za-z0-9_]+$' -or $DeviceId -notmatch '^[A-Za-z0-9_\-]+$') { throw 'Invalid account/device identifier' }

$ProjectId = '79c61872-d992-44c7-a5ae-d3e70e004e7f'
$Repo = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $env:LOCALAPPDATA 'wechat-vault\logs'
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir ("incremental-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
Get-ChildItem $LogDir -Filter 'incremental-*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force

function Write-Log([string] $Text) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Text" | Add-Content -Encoding utf8 $Log }

# Proxies in preference order; a proxy is used only when its port accepts connections.
$Proxies = @(
    @{ Name = 'beijing'; Host = '125.35.57.17'; Port = 80 },
    @{ Name = 'relay'; Host = '127.0.0.1'; Port = 18888 }
) | Where-Object {
    $client = [Net.Sockets.TcpClient]::new()
    try { $client.ConnectAsync($_.Host, $_.Port).Wait(3000) -and $client.Connected } catch { $false } finally { $client.Dispose() }
}
if (-not $Proxies) { Write-Log 'FAILED: no proxy reachable'; exit 3 }

function Use-Proxy($Proxy) {
    $env:HTTPS_PROXY = $env:HTTP_PROXY = "http://$($Proxy.Host):$($Proxy.Port)"
    $env:NO_PROXY = 'localhost,127.0.0.1'
}

function Import-Secrets([string] $Path) {
    $json = infisical export --projectId $ProjectId --env dev --path $Path --format json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return $false }
    foreach ($item in ($json | ConvertFrom-Json)) { Set-Item "env:$($item.key)" $item.value }
    return $true
}

$code = 1
foreach ($proxy in $Proxies) {
    Use-Proxy $proxy
    Write-Log "[run-windows] proxy=$($proxy.Name)"
    if (-not ((Import-Secrets '/wechat-vault') -and (Import-Secrets "/wechat-vault/$AccountId/$DeviceId"))) {
        Write-Log '[run-windows] could not read secrets; trying the next proxy'
        continue
    }
    Set-Location $Repo
    $env:PYTHONIOENCODING = 'utf-8'
    $notify = if ($proxy -eq $Proxies[-1]) { @() } else { @('--no-notify') }  # only the last attempt may notify
    uv run --system-certs --offline python -u -m wechat_vault.incremental --account-root $AccountRoot --source win @notify @Extra 2>&1 |
        ForEach-Object { "$_" } | Add-Content -Encoding utf8 $Log
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }
    Write-Log "[run-windows] exit $code; trying the next proxy"
}
Write-Log "[run-windows] finished with exit $code"
exit $code
