param([string]$PythonPath = "", [int]$Port = 8001)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectDirectory
if (-not $PythonPath) { $PythonPath = Join-Path $projectDirectory '.rag-venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Create .rag-venv with Python 3.12, install CPU torch and requirements-reranker.txt first. See docs/ai-expected-answer/rag-implementation.md.'
}
$env:RAG_RERANKER_REVISION = 'e61197ed45024b0ed8a2d74b80b4d909f1255473'
& $PythonPath -m uvicorn rag_service.reranker:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
