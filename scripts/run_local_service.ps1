param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('backend', 'llama', 'frontend')]
    [string]$Service
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logRoot "$Service-$stamp-$PID.stdout.log"
$stderr = Join-Path $logRoot "$Service-$stamp-$PID.stderr.log"
$events = Join-Path $logRoot 'local-service-events.jsonl'

function Write-ServiceEvent($EventName, $Details) {
    @{ time = [DateTimeOffset]::Now.ToString('o'); service = $Service; event = $EventName; details = $Details } |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $events -Encoding UTF8
}

try {
    switch ($Service) {
        'backend' {
            $executable = Join-Path $projectRoot 'dcm-back\.venv\Scripts\python.exe'
            $workingDirectory = Join-Path $projectRoot 'dcm-back'
            $serviceArguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000')
        }
        'llama' {
            $executable = 'C:\llama-server\llama-server.exe'
            $workingDirectory = $projectRoot
            $baseModel = Join-Path $projectRoot 'model-training\modelfiles\llama-server\gemma-4-E4B-it-Q4_K_M.gguf'
            $adapter = Join-Path $projectRoot 'model-training\modelfiles\llama-server\complaint-lora-version_3-f16.gguf'
            foreach ($modelFile in @($baseModel, $adapter)) {
                if (-not (Test-Path -LiteralPath $modelFile)) { throw "Model file not found: $modelFile" }
            }
            $serviceArguments = @('-m', "`"$baseModel`"", '--lora', "`"$adapter`"", '--lora-init-without-apply',
                '--jinja', '-c', '32768', '-np', '1', '-ngl', '10', '--host', '127.0.0.1', '--port', '8080')
        }
        'frontend' {
            $executable = (Get-Command node.exe -ErrorAction Stop).Source
            $workingDirectory = Join-Path $projectRoot 'dcm-front'
            $vite = Join-Path $workingDirectory 'node_modules\vite\bin\vite.js'
            if (-not (Test-Path -LiteralPath $vite)) { throw 'Frontend dependencies missing. Run npm install in dcm-front.' }
            $serviceArguments = @("`"$vite`"", '--host', 'localhost', '--port', '5173', '--strictPort')
        }
    }
    if (-not (Test-Path -LiteralPath $executable)) { throw "Executable not found: $executable" }
    Write-ServiceEvent 'starting' @{ stdout = $stdout; stderr = $stderr }
    $serviceProcess = Start-Process -FilePath $executable -ArgumentList $serviceArguments -WorkingDirectory $workingDirectory `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-ServiceEvent 'started' @{ pid = $serviceProcess.Id; stdout = $stdout; stderr = $stderr }
    $serviceProcess.WaitForExit()
    $serviceExitCode = $serviceProcess.ExitCode
    Write-ServiceEvent 'exited' @{ exit_code = $serviceExitCode }
    exit $serviceExitCode
} catch {
    Write-ServiceEvent 'failed' @{ message = $_.Exception.Message }
    $_ | Out-String | Add-Content -LiteralPath $stderr -Encoding UTF8
    exit 1
}
