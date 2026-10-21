param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot 'run_local_service.ps1'
$powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskPrefix = 'ComplaintAI-doc-main-'
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Test-ServiceUrl([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Ensure-LocalService([string]$Service, [string]$Url, [int]$TimeoutSeconds) {
    if (Test-ServiceUrl $Url) { Write-Host "$Service already available: $Url"; return }
    $taskName = "$taskPrefix$Service"
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Actions.WorkingDirectory -ne $projectRoot -or $existing.Actions.Arguments -notlike "*$runner*") {
            throw "Task name belongs to another checkout: $taskName"
        }
    } else {
        $action = New-ScheduledTaskAction -Execute $powershellExe -WorkingDirectory $projectRoot `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Service $Service"
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        # No trigger: services start only when this launcher is run.
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings `
            -Description "doc-main local $Service service. Manual start; no calendar or logon trigger." | Out-Null
    }
    $state = (Get-ScheduledTask -TaskName $taskName).State
    if ($state -ne 'Running') { Start-ScheduledTask -TaskName $taskName }
    Write-Host "Starting $Service; logs: $(Join-Path $projectRoot 'logs')"
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (Test-ServiceUrl $Url) { Write-Host "$Service ready: $Url"; return }
        $taskState = Get-ScheduledTask -TaskName $taskName
        $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
        if ($taskState.State -eq 'Ready' -and $taskInfo.LastTaskResult -notin @(0, 267009, 267011)) {
            throw "$Service exited with code $($taskInfo.LastTaskResult). See logs/local-service-events.jsonl."
        }
        Start-Sleep -Seconds 1
    }
    throw "$Service did not become ready in $TimeoutSeconds seconds. Inspect logs/local-service-events.jsonl."
}

Ensure-LocalService 'llama' 'http://127.0.0.1:8080/health' 180
Ensure-LocalService 'backend' 'http://127.0.0.1:8000/health/database' 60
Ensure-LocalService 'frontend' 'http://localhost:5173/' 30
Write-Host 'ComplaintAI ready: http://localhost:5173/'
