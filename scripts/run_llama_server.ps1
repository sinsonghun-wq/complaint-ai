# llama-server 실행 - 베이스 모델 GGUF + 학습한 어댑터(LoRA) GGUF
#
# 어댑터는 --lora-init-without-apply 로 '올려만' 둡니다. (기본 세기 0 = 베이스 모델)
# 백엔드(dcm-back/app/llm.py)가 요청마다 "lora": [{"id": 0, "scale": 1 또는 0}] 을 보내 작업별로 켜고 끕니다.
#   민원 분석·챗봇 -> 어댑터 켬 / CSV 열 매핑 -> 베이스 모델   (.env 의 LLM_ADAPTER_TASKS)
#
# 준비물 (Colab 노트북 11-B 에서 내려받은 파일)
#   model-training/modelfiles/llama-server/gemma-4-E4B-it-q8_0.gguf           <- 베이스 (한 번 만들면 계속 씀)
#   model-training/modelfiles/llama-server/complaint-lora-<RUN_NAME>-f16.gguf  <- 어댑터 (학습할 때마다 새로)
# llama-server.exe : https://github.com/ggml-org/llama.cpp/releases 에서 GPU 에 맞는 Windows 빌드
#                    (NVIDIA 면 cuda 빌드, 그 외 vulkan 빌드)를 받아 압축을 풉니다. Gemma 4 를 지원하는 최근 빌드여야 합니다.
#
# 실행 예
#   .\scripts\run_llama_server.ps1 -LlamaServer C:\llama.cpp\llama-server.exe -Adapter .\model-training\modelfiles\llama-server\complaint-lora-v1013_1500-f16.gguf
#
# 확인
#   curl http://127.0.0.1:8080/health          -> {"status":"ok"}
#   curl http://127.0.0.1:8080/lora-adapters   -> [{"id":0,"path":"...","scale":0.0}]  (scale 0 이 정상 - 요청마다 켬)
param(
    [string]$ProjectRoot = "",
    [string]$LlamaServer = "llama-server.exe",
    [string]$BaseModel = "",
    [Parameter(Mandatory = $true)][string]$Adapter,
    [int]$Port = 8080,
    # 슬롯(동시 요청) 수와 슬롯당 컨텍스트. 전체 컨텍스트 = 슬롯 수 x 슬롯당 컨텍스트
    # 챗봇(API 서버)과 CSV 가져오기(워커)가 동시에 요청할 수 있어 2 슬롯으로 둡니다.
    [int]$Parallel = 2,
    [int]$CtxPerSlot = 8192,
    # GPU 에 올릴 레이어 수 (99 = 전부). VRAM 이 모자라면 낮추세요.
    [int]$GpuLayers = 99
)

# Windows PowerShell 5.1 에서는 param 기본값 안의 $PSScriptRoot 가 비어 있을 수 있어 본문에서 정합니다.
if (-not $ProjectRoot) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $ProjectRoot = Split-Path -Parent $scriptDir
}
if (-not $BaseModel) {
    $BaseModel = Join-Path $ProjectRoot "model-training\modelfiles\llama-server\gemma-4-E4B-it-q8_0.gguf"
}
foreach ($file in @($BaseModel, $Adapter)) {
    if (-not (Test-Path -LiteralPath $file)) { throw "파일을 찾지 못했습니다: $file" }
}

$ctx = $Parallel * $CtxPerSlot
$serverArgs = @(
    "-m", $BaseModel,
    "--lora", $Adapter,
    "--lora-init-without-apply",   # 어댑터를 메모리에 올리되 기본은 꺼 둠 (요청마다 켬)
    "--jinja",                     # GGUF 안의 Gemma 4 채팅 템플릿 사용 + 도구 호출 해석 (챗봇에 필수)
    "-c", $ctx,
    "-np", $Parallel,
    "-ngl", $GpuLayers,
    "--host", "127.0.0.1",
    "--port", $Port
)
Write-Host "llama-server 시작: 베이스=$BaseModel"
Write-Host "                   어댑터=$Adapter (id 0, 기본 꺼짐)"
Write-Host "                   컨텍스트=$ctx (슬롯 $Parallel 개 x $CtxPerSlot) / http://127.0.0.1:$Port"
& $LlamaServer @serverArgs
