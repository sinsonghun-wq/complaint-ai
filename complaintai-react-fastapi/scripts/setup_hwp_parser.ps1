$ErrorActionPreference = "Stop"

# pyhwp is installed in a project-local Python 3.11 environment because the
# main FastAPI runtime may use a newer Python version that pyhwp does not support.
$projectRoot = Split-Path -Parent $PSScriptRoot
$parserEnvironment = Join-Path $projectRoot ".hwp-parser"
$parserPython = Join-Path $parserEnvironment "Scripts\python.exe"
$hwp5txt = Join-Path $parserEnvironment "Scripts\hwp5txt.exe"

& py -3.11 -m venv $parserEnvironment
& $parserPython -m pip install "pyhwp==0.1b15" "six==1.17.0"

if (-not (Test-Path $hwp5txt)) {
    throw "hwp5txt 설치에 실패했습니다. Python 3.11 설치 상태와 네트워크 연결을 확인해 주세요."
}

Write-Host "HWP 파서 준비 완료: $hwp5txt"
