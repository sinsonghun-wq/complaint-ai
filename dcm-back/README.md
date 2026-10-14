# dcm-back - 실행 방법

FastAPI 백엔드입니다. 민원 분석·챗봇·CSV 열 매핑에 LLM을, 민원 검색에 임베딩 모델을 씁니다.
아래 명령은 모두 **Windows PowerShell, 저장소 루트**(`doc-classifier-ml`) 기준입니다.

## 구성

| 구성 요소 | 역할 | 주소 |
|---|---|---|
| PostgreSQL 16 + pgvector | 민원·계정·임베딩 저장 | `.env` 의 `DATABASE_URL` (기본 예시 `127.0.0.1:5433`) |
| Ollama | **임베딩** (`qwen3-embedding:4b`). `LLM_BACKEND=ollama` 면 LLM 도 | `http://127.0.0.1:11434` |
| llama-server | **LLM** - 베이스 모델 + 학습한 어댑터 (`LLM_BACKEND=llama_server` 일 때만) | `http://127.0.0.1:8080` |
| 백엔드 (FastAPI) | API 서버 | `http://127.0.0.1:8000` |
| 프론트 (Vite) | 화면 | `http://127.0.0.1:5173` |

## 실행 순서 (매번)

```
① PostgreSQL → ② llama-server → ③ Ollama → ④ 백엔드 → ⑤ 프론트
```

창 하나에 하나씩, PowerShell 창을 여러 개 띄워 실행합니다.

### ① PostgreSQL
설치형이면 Windows 서비스로 이미 떠 있습니다. Docker 를 쓰면 `docker start complaintai-db`.

### ② llama-server (`LLM_BACKEND=llama_server` 일 때만)
Ollama 보다 **먼저** 띄웁니다. VRAM 이 작아(GTX 1060 3GB) 먼저 띄운 쪽이 GPU 를 잡기 때문입니다.

**프로젝트 최상위 폴더**(`dcm-back`, `model-training`, `scripts` 가 들어 있는 폴더)로 `cd` 한 뒤 실행합니다.
다른 위치에서 실행하면 `-File 매개 변수에 대한 인수 ... 이(가) 없습니다` 오류가 납니다.

- `<llama-server 폴더>` : llama-server.exe 를 압축 푼 폴더
- `<RUN_NAME>` : 사용할 어댑터 이름 (예: `smoke_1013_0642`)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_llama_server.ps1 `
  -LlamaServer <llama-server 폴더>\llama-server.exe `
  -BaseModel .\model-training\modelfiles\llama-server\gemma-4-E4B-it-Q4_K_M.gguf `
  -Adapter .\model-training\modelfiles\llama-server\complaint-lora-<RUN_NAME>-f16.gguf `
  -GpuLayers 10 -Parallel 1
```

`server is listening on http://127.0.0.1:8080` 이 나오면 준비 완료. **이 창은 닫지 않습니다.** 확인:

```powershell
curl.exe http://127.0.0.1:8080/health          # {"status":"ok"}
curl.exe http://127.0.0.1:8080/lora-adapters   # [{"id":0, ...}]
```

### ③ Ollama
작업 표시줄 트레이에 Ollama 아이콘이 있으면 이미 실행 중입니다. 없으면 Ollama 앱을 실행합니다.
임베딩 모델이 없다면 한 번만: `ollama pull qwen3-embedding:4b`

### ④ 백엔드
```powershell
.venv\Scripts\activate
cd dcm-back
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

시작 로그에서 LLM 연결 상태를 확인합니다.

| 로그 | 의미 |
|---|---|
| `[LLM] backend=llama_server ... health=200 adapters=[{'id': 0, ...}]` | llama-server + 어댑터 연결 정상 |
| `[LLM] backend=ollama ...` | Ollama 방식으로 동작 중 |
| `llama-server(...)에 연결하지 못했습니다` | ② 가 꺼져 있음. 이 상태로 접수하면 분석이 규칙 기반(fallback)으로 저장됨 |
| `어댑터 id=0 가 llama-server 에 없습니다` | ② 를 `-Adapter` 없이 띄웠거나 `LLAMA_SERVER_LORA_ID` 가 틀림 |

`.env` 를 고치면 백엔드를 **재시작**해야 반영됩니다.

### ⑤ 프론트
```powershell
cd dcm-front
npm run dev
```
`http://127.0.0.1:5173` 접속 → 로그인 화면의 **처리 서버 주소**를 `http://127.0.0.1:8000` 으로 둡니다.

### 끌 때
각 창에서 `Ctrl + C`.

## LLM 백엔드 바꾸기 (`dcm-back\.env`)

**llama-server + 어댑터** (현재 방식)
```
LLM_BACKEND=llama_server
LLAMA_SERVER_URL=http://127.0.0.1:8080
LLM_ADAPTER_TASKS=analysis,chat          # 어댑터를 켤 작업. csv_mapping 은 빠져 있어 베이스 모델로 답함
LLAMA_SERVER_LORA_ID=0
LLAMA_SERVER_BASE_NAME=gemma4-e4b
LLAMA_SERVER_ADAPTER_NAME=complaint-lora-<RUN_NAME>   # DB llm_model 에 남는 이름 (기록용)
LLM_TIMEOUT_MS=600000                    # LLM 응답 대기 최대 10분. CPU 위주 실행이라 분석 1건에 30~40초, 요청이 몰리면 대기가 길어짐
```

**Ollama** (이전 방식 - 되돌릴 때)
```
LLM_BACKEND=ollama
LLM_MODEL=complaint-gemma4:latest        # 또는 일반 모델
CSV_MAPPING_LLM_MODEL=                   # CSV 열 매핑만 다른 모델로 (비우면 LLM_MODEL)
```

| 작업 | 코드 | 기본 설정에서 쓰는 모델 (`llama_server`) |
|---|---|---|
| 민원 분석 | `ai.analyze()` | 베이스 + 어댑터 |
| 챗봇 | `agent.py` | 베이스 + 어댑터 |
| CSV 열 매핑 | `ai.infer_csv_mapping()` | 베이스만 (학습하지 않은 작업) |
| 임베딩 | `ai.embeddingForSelect()` | `LLM_BACKEND` 와 상관없이 Ollama `qwen3-embedding:4b` |

DB 의 `complaints.llm_model` 로 어떤 모델이 분석했는지 확인할 수 있습니다.
```sql
SELECT id, processing_mode, llm_model, created_at FROM complaints ORDER BY id DESC LIMIT 5;
```
`processing_mode = fallback` 이면 LLM 호출이 실패해 규칙 기반으로 저장된 것입니다. 백엔드 로그의 오류를 확인하세요.

**임베딩 모델은 바꾸지 마세요.** 바꾸면 이미 저장된 민원 벡터와 비교가 안 돼 유사 민원 검색·챗봇 조회가 틀어집니다.

## 새로 학습한 어댑터로 교체

1. Colab 노트북 11-B 를 `MAKE_BASE_GGUF = False` 로 실행 → 어댑터 `.gguf` 하나만 받음
2. `model-training\modelfiles\llama-server\` 에 넣기
3. ② llama-server 를 `-Adapter` 경로만 바꿔 다시 띄우기
4. `.env` 의 `LLAMA_SERVER_ADAPTER_NAME` 을 새 RUN_NAME 으로 → 백엔드 재시작
5. 평가: `python model-training\evaluate.py --llama-server http://127.0.0.1:8080 --limit 30`
   (`--lora-scale 0` 을 붙이면 같은 서버의 베이스 모델 점수)

자세한 학습·내보내기 절차는 `model-training\README.md`.

## 처음 한 번만 (새 PC)

1. **Python 가상환경** (저장소 루트)
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r dcm-back\requirements.txt
   ```
2. **환경 설정**: `dcm-back\.env.example` 을 `dcm-back\.env` 로 복사하고 `DATABASE_URL`, `AUTH_TOKEN_SECRET` 등을 채웁니다.
3. **DB**: PostgreSQL 16 + pgvector 에 `database\init.sql` 실행 (기존 데이터 삭제됨 - `database\README.md` 참고).
   개발용 계정이 필요하면 `database\seed_demo_accounts.sql`.
4. **Ollama**: 설치 후 `ollama pull qwen3-embedding:4b`
5. **llama-server**: [llama.cpp 릴리스](https://github.com/ggml-org/llama.cpp/releases)에서 Windows x64 **CUDA 12** 본체 + **CUDA 12.4 DLLs** 를 받아 같은 폴더에 풉니다.
   (GTX 10xx 는 CUDA 13 미지원. NVIDIA 가 아니면 Vulkan 빌드)
6. **모델 파일**: `model-training\modelfiles\llama-server\` 에 베이스 GGUF + 어댑터 GGUF.
   베이스는 Q8_0 을 받아 PC 에서 줄여 씁니다 (메모리 16GB 기준):
   ```powershell
   cd model-training\modelfiles\llama-server
   <llama-server 폴더>\llama-quantize.exe --allow-requantize gemma-4-E4B-it-q8_0.gguf gemma-4-E4B-it-Q4_K_M.gguf Q4_K_M
   ```
7. **프론트**: `cd dcm-front` → `npm install`
8. (선택) **HWP 업로드**: `powershell -ExecutionPolicy Bypass -File .\scripts\setup_hwp_parser.ps1` (Python 3.11 필요)

모델 파일(`*.gguf`)·가상환경·`.env` 는 `.gitignore` 대상이라 커밋되지 않습니다. 새 PC 에는 따로 옮겨야 합니다.

## 자주 나는 문제

| 증상 | 원인·해결 |
|---|---|
| `'llama-server.exe' 용어가 ... 인식되지 않습니다` | 현재 폴더 실행은 `.\llama-server.exe` 처럼 `.\` 를 붙이거나 전체 경로 사용 |
| `-File parameter does not exist` | 저장소 루트가 아닌 곳에서 실행함. `cd` 로 루트 이동 |
| `python.exe ... WindowsApps` / `No module named ...` | 가상환경이 안 켜짐. `.venv\Scripts\activate` |
| `curl` 결과가 이상함 | Windows PowerShell 의 `curl` 은 다른 명령. `curl.exe` 로 입력 |
| llama-server 시작 중 `out of memory` | `-GpuLayers` 를 낮춤 (예: 10 → 5, 0 이면 CPU 만) |
| 분석이 `fallback` 으로만 저장됨 | llama-server 꺼짐 / 시간 초과 → `LLM_TIMEOUT_MS` 늘리기, 백엔드 로그 확인 |
| 챗봇 응답이 오래 걸림 | `-Parallel 1` 이라 요청을 하나씩 처리해 앞 요청을 기다림. 메모리 여유가 있으면 `-Parallel 2` |
