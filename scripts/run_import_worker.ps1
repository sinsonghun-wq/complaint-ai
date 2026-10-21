param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경 Python을 찾지 못했습니다: $python"
}

Set-Location -LiteralPath $ProjectRoot
& $python -m app.worker
