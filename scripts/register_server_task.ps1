$ErrorActionPreference = "Stop"

$payload = ($input | Out-String) | ConvertFrom-Json
$secureKey = ConvertTo-SecureString $payload.key -AsPlainText -Force
$encryptedKey = ConvertFrom-SecureString $secureKey

$taskName = "SPHERExTimeMachine"
$scriptPath = Join-Path $PSScriptRoot "run_server.ps1"
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
    $scriptPath + '" -EncryptedKey "' + $encryptedKey + '" -Port 8008'

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Output "TASK_STARTED"
