# Registers the daily incremental sync as a Task Scheduler job for the current user (runs while logged on:
# the Infisical CLI login is per user; StartWhenAvailable runs a missed job once the machine is back).
param([string] $At = '13:30', [string] $TaskName = 'wechat-vault-incremental')

$script = Join-Path $PSScriptRoot 'run-windows.ps1'
$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force |
    Select-Object TaskName, State
