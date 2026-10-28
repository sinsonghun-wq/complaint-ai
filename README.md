# ComplaintAI — 다른 컴퓨터에서 설치하고 실행하기

민원 접수·분류·답변과 AI 추천 답변을 제공하는 React + FastAPI 프로젝트입니다.
이 문서는 **Windows 11 64비트 + VS Code + PowerShell**을 기준으로 설명합니다.
처음 설치할 때는 1~8번을 순서대로 진행하고, 다음 실행부터는 9번을 보면 됩니다.

**어댑터 다운로드·변환·적용 방법은 기존 [어댑터 적용 가이드](<어댑터_적용_가이드(꼭 확인).md>)를 따릅니다.**
이 README에서는 그 문서의 설정을 다시 정의하지 않습니다. 저장소를 받은 뒤 가이드나 모델 파일이 보이지 않으면 담당자에게 별도로 전달받으세요.

## 1. 먼저 설치할 프로그램

| 프로그램 | 받을 버전·설치 방법 | 용도 |
| --- | --- | --- |
| [VS Code](https://code.visualstudio.com/download) | Windows x64 설치 파일 | 코드 확인, 터미널 실행 |
| [Git for Windows](https://git-scm.com/install/windows) | Windows x64 설치 파일 | GitHub에서 프로젝트 내려받기 |
| [Python](https://www.python.org/downloads/windows/) | **Python 3.14 계열, Windows installer (64-bit)**. 설치 시 PATH 및 Python 실행 관리자/런처를 포함 | FastAPI 백엔드 실행 |
| [Node.js](https://nodejs.org/en/download) | **24 LTS, 24.15.0 이상**, Windows x64 `.msi`. npm도 함께 설치 | React 설치·실행 |
| [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) | Windows x86_64. WSL 2 기반 Linux 컨테이너 사용 | PostgreSQL + pgvector 실행 |
| [Ollama](https://ollama.com/download/windows) | Windows 설치 파일 | 민원·법률 자료의 임베딩 생성 |
| llama-server와 모델 파일 | **기존 어댑터 적용 가이드의 다운로드·배치 절차 참고** | 학습 모델을 사용하는 AI 분석·답변 |

작성 시 이 프로젝트의 로컬 실행 환경은 Python **3.14.7**, Node.js **24.19.0**, PostgreSQL **17**입니다.
Node.js 버전 조건은 `dcm-front/package-lock.json`의 의존성 요구사항에 맞췄습니다.
Python·Node.js는 프로젝트 폴더 안에 설치할 필요가 없습니다.
모델과 DB도 함께 실행하므로 메모리 16GB 이상을 권장하며, 처리 속도는 GPU와 메모리에 따라 달라집니다.

Docker 설치 중 WSL 2 설정이 필요하다는 안내가 나오면 설치 화면과 공식 문서를 따라 진행하고 재부팅하세요.
이 문서에서는 pgvector가 포함된 Docker 이미지를 사용하므로 **별도의 Windows용 PostgreSQL 설치는 필요하지 않습니다.**
pgAdmin은 DB를 화면으로 확인하고 싶은 경우에만 별도로 설치하면 됩니다.

설치 후 **VS Code를 완전히 닫았다가 다시 열고**, 상단 **터미널 → 새 터미널**에서 PowerShell을 선택합니다.
다음을 한 줄씩 실행해 버전이 나오는지 확인하세요.

```powershell
git --version
py -3.14 --version
node --version
npm.cmd --version
docker --version
ollama --version
```

`명령을 찾을 수 없습니다`가 나오면 해당 프로그램 설치와 PATH 설정을 확인한 뒤 터미널을 새로 여세요.
아래에서 `npm.cmd`를 사용하는 이유는 PowerShell의 `npm.ps1` 실행 정책 오류를 피하기 위해서입니다.

## 2. GitHub에서 프로젝트 내려받기

이 문서에서는 **`C:\work\doc-main`**에 프로젝트를 받는 것으로 통일합니다.
다른 폴더를 사용한다면 이후 명령의 `C:\work\doc-main`을 자신의 프로젝트 경로로 바꾸세요.
예를 들어 기존 개발 PC의 경로는 `C:\Users\1Class_07\doc-main`입니다.

처음 받는 PC에서 다음을 실행합니다. 이미 같은 폴더에 프로젝트가 있다면 다시 clone하지 않습니다.

```powershell
New-Item -ItemType Directory -Path "C:\work" -Force
cd "C:\work"
git clone https://github.com/usriddle/doc-classifier-ml.git doc-main
cd "C:\work\doc-main"
code .
```

GitHub 로그인을 요청하면 저장소 접근 권한이 있는 계정으로 로그인하세요.
`code` 명령이 안 되면 VS Code의 **파일 → 폴더 열기**에서 `C:\work\doc-main`을 선택해도 됩니다.
ZIP으로 받았다면 압축을 푼 뒤 아래 폴더들이 직접 들어 있는 폴더를 여세요.

```text
doc-main/
├─ README.md
├─ dcm-back/           # FastAPI: requirements.txt, .env.example, app/
├─ dcm-front/          # React: package.json, package-lock.json, src/
├─ database/           # DB 초기화 SQL과 마이그레이션
├─ model-training/     # 모델 관련 파일
├─ scripts/            # 실행·설치 스크립트
└─ start-doc-main.cmd  # 설치 완료 후 사용할 수 있는 간편 실행 파일
```

**`.env`, `.venv`, `node_modules`, 모델 GGUF, 업로드 파일, 실제 DB 데이터는 GitHub에서 자동으로 내려오지 않습니다.**
가상환경과 npm 패키지는 아래 절차로 새로 설치하고, 모델·기존 데이터는 별도로 준비합니다.
다른 PC의 `.venv`나 `node_modules` 폴더를 그대로 복사해서 사용하지 마세요.

## 3. Python 패키지와 React 패키지 설치 — 처음 한 번

### 3-1. 백엔드 가상환경 만들기

VS Code 터미널에서 다음을 실행합니다.

```powershell
cd "C:\work\doc-main\dcm-back"
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

`requirements.txt`에 적힌 Python 패키지를 **`dcm-back\.venv`**에 설치하는 과정입니다.
마지막 명령에서 `No broken requirements found.`가 나오면 설치된 패키지 사이의 의존성 검사를 통과한 것입니다.
설치 중 오류가 나면 그 상태로 다음 단계로 넘어가지 말고 오류 내용을 확인하세요.

이 문서는 가상환경의 `python.exe`를 직접 실행하므로 `activate`나 PowerShell 실행 정책 변경이 필요 없습니다.
**저장소 최상위가 아니라 `dcm-back` 안에 `.venv`를 만듭니다.** 자동 실행 스크립트도 이 위치를 사용합니다.

### 3-2. 프론트엔드 패키지 설치

```powershell
cd "C:\work\doc-main\dcm-front"
npm.cmd ci
```

`package-lock.json`에 기록된 버전으로 설치합니다. 다른 PC에서 처음 실행할 때도 같은 명령을 사용하세요.

## 4. PostgreSQL 17 + pgvector 준비 — 처음 한 번

### 4-1. Docker Desktop 실행

시작 메뉴에서 **Docker Desktop**을 실행하고 엔진이 준비될 때까지 기다립니다.
VS Code 터미널에서 다음 명령에 서버 정보가 나와야 합니다.

```powershell
docker info
```

이 문서의 신규 DB 이름은 `complaintai`, 접속 계정은 `complaintai`, PC에서 접속하는 포트는 **5433**입니다.
컨테이너 이름은 **`doc-main-db`**, 데이터를 보관하는 볼륨 이름은 `doc-main-pg17-data`로 정합니다.
기존 PC에서 사용하던 컨테이너 이름과 다를 수 있습니다.

### 4-2. 새 DB 컨테이너 생성

**새 PC에서 아직 DB를 만들지 않았을 때만** 실행합니다. 5433 포트를 이미 다른 DB가 사용 중이면 먼저 10번의 포트 확인 방법을 보세요.
아래 비밀번호는 새 로컬 개발 DB용 예시이며, 바꾸면 5번의 `.env`에도 똑같이 적어야 합니다.

```powershell
docker pull pgvector/pgvector:pg17
docker volume create doc-main-pg17-data
docker run -d --name doc-main-db --restart unless-stopped --shm-size=512m -p 127.0.0.1:5433:5432 -e "POSTGRES_DB=complaintai" -e "POSTGRES_USER=complaintai" -e "POSTGRES_PASSWORD=DocMain_Local_2026!" -v doc-main-pg17-data:/var/lib/postgresql/data pgvector/pgvector:pg17
```

[pgvector 프로젝트의 PostgreSQL 17 이미지](https://github.com/pgvector/pgvector#docker)를 사용합니다.
컨테이너를 중지해도 DB 데이터는 볼륨에 남습니다.
이미 만들어 둔 컨테이너는 다음 실행부터 `docker start doc-main-db`로 켜며, `docker run`을 반복하지 않습니다.
기존 볼륨을 재사용할 때 `POSTGRES_PASSWORD`만 바꿔 실행해도 저장된 계정 비밀번호가 바뀌지는 않습니다.

준비 상태를 확인합니다.

```powershell
docker exec doc-main-db pg_isready -U complaintai -d complaintai
```

`accepting connections`가 나오면 다음 단계로 진행하세요. 아직 준비 중이면 잠시 뒤 다시 확인합니다.

### 4-3. 새 DB에 테이블·테스트 계정 생성

**주의: `database/init.sql`은 기존 프로젝트 테이블을 삭제하고 다시 만듭니다. 아래 명령은 방금 만든 빈 개발 DB에만 실행하세요.**
기존 데이터가 들어 있는 DB나 팀 공용 DB에는 실행하지 않습니다.

```powershell
cd "C:\work\doc-main"
docker cp ".\database\init.sql" doc-main-db:/tmp/doc-main-init.sql
docker exec doc-main-db psql -U complaintai -d complaintai -v ON_ERROR_STOP=1 -f /tmp/doc-main-init.sql
```

현재 `init.sql`에는 테이블과 부서 7개, 일반 사용자 2명, 관리자 7명이 포함되어 있습니다.
**`seed_demo_accounts.sql`은 추가 실행하지 않습니다.** 백엔드 시작 시 나머지 추가 스키마도 보완합니다.

다음 명령으로 확인합니다. 계정 수는 9, 부서 수는 7이어야 합니다.

```powershell
docker exec doc-main-db psql -U complaintai -d complaintai -c "SELECT extname FROM pg_extension WHERE extname='vector'; SELECT COUNT(*) AS accounts FROM app_users; SELECT id,name FROM departments ORDER BY id;"
```

pgAdmin으로 확인하려면 새 서버 연결에 아래 값을 입력하세요.
pgAdmin의 자체 로그인/마스터 비밀번호와 PostgreSQL 접속 비밀번호는 서로 다릅니다.

| 항목 | 이 문서의 신규 DB 설정 |
| --- | --- |
| Name | `doc-main 로컬 DB`처럼 원하는 이름 |
| Host name/address | `127.0.0.1` |
| Port | `5433` |
| Maintenance database | `complaintai` |
| Username | `complaintai` |
| Password | 위 `docker run`에서 지정한 비밀번호 |

## 5. 백엔드 환경 설정 파일 만들기

새 PC에서 `.env.example`을 복사합니다. 기존 `.env`가 있으면 덮어쓰지 않고 내용을 확인합니다.

```powershell
cd "C:\work\doc-main\dcm-back"
if (-not (Test-Path -LiteralPath ".env")) { Copy-Item -LiteralPath ".env.example" -Destination ".env" }
notepad .env
```

메모장에서 아래 항목을 확인·수정한 뒤 저장하세요. **실제로 읽는 파일은 `dcm-back\.env`입니다.**
예제 파일만 고치거나 `.env.txt`로 저장하면 적용되지 않습니다.

```dotenv
DATABASE_URL=postgresql://complaintai:DocMain_Local_2026!@127.0.0.1:5433/complaintai
PORT=8000
WEB_ORIGIN=http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000,http://localhost:8000
AUTH_TOKEN_SECRET=여기를_아래에서_생성한_값으로_교체
FILE_STORAGE_DIR=./data/uploads
MAX_BATCH_SIZE=500
LLM_BACKEND=llama_server
LLAMA_SERVER_URL=http://127.0.0.1:8080
OLLAMA_URL=http://127.0.0.1:11434
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=BAAI/bge-m3
OLLAMA_EMBEDDING_MODEL=bge-m3:latest
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TIMEOUT_MS=600000
EMBEDDING_NUM_CTX=8192
EMBEDDING_NUM_BATCH=8192
LLM_NUM_CTX=32768
LLM_TIMEOUT_MS=600000
LLM_IMPORT_ENABLED=false
```

이 블록은 확인할 주요 항목입니다. `.env.example`에서 복사한 나머지 항목은 유지하고,
`LLM_ADAPTER_TASKS`·`LLAMA_SERVER_LORA_ID`·`LLAMA_SERVER_ADAPTER_NAME` 등 **어댑터 관련 항목은 기존 가이드에 따라 설정**하세요.
현재 예제의 `LLM_IMPORT_ENABLED=false`는 파일 일괄 처리에서 행별 LLM 분석을 끄는 설정으로,
개별 민원이나 챗봇의 LLM까지 끄는 값은 아닙니다.

`AUTH_TOKEN_SECRET`에 넣을 값을 생성하려면 다음을 실행하고, 출력된 문자열을 위 자리로 복사합니다.

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

`.env`에는 각 설정을 한 줄씩 적습니다. DB 비밀번호·호스트·포트는 실제로 만든 DB와 일치해야 합니다.
설정을 바꾼 뒤에는 백엔드를 다시 실행해야 적용됩니다. `.env`는 GitHub에 올리지 않습니다.

## 6. 임베딩 모델과 학습 모델 준비

### 6-1. Ollama 임베딩 모델

Ollama 앱을 실행한 뒤 터미널에서 다음을 실행합니다. 다운로드는 처음 한 번만 필요합니다.

```powershell
ollama pull bge-m3:latest
ollama list
curl.exe http://127.0.0.1:11434/api/tags
```

목록에 `bge-m3:latest`가 보여야 합니다. [Ollama의 BGE-M3 모델](https://ollama.com/library/bge-m3)을 사용합니다.
현재 DB는 **1024차원 BGE-M3 벡터**를 사용하므로 다른 임베딩 모델로 임의 변경하지 않습니다.
기존 하위 README의 `qwen3-embedding:4b` 안내 대신, 현재 설정 파일과 이 문서의 `bge-m3:latest`를 기준으로 준비하세요.

Ollama 앱이 실행되지 않아 접속할 수 없다면 **새 터미널**에서 다음을 실행하고 창을 유지하세요.
이미 Ollama가 켜져 있다면 중복 실행할 필요가 없습니다.

```powershell
ollama serve
```

### 6-2. llama-server·베이스 모델·어댑터

[기존 어댑터 적용 가이드](<어댑터_적용_가이드(꼭 확인).md>)의 다운로드 → 파일 배치 → `.env` 설정 → 서버 실행 절차를 완료하세요.
모델 파일은 GitHub clone이나 `pip install`로 설치되지 않습니다.
이 프로젝트의 `scripts/run_llama_server.ps1`은 저장소 최상위에서 실행합니다.

서버를 켠 다음 새 터미널에서 확인합니다.

```powershell
curl.exe http://127.0.0.1:8080/health
curl.exe http://127.0.0.1:8080/lora-adapters
```

`/health`에 `{"status":"ok"}`가 나오고 어댑터 목록에 설정한 ID가 있어야 합니다.
모델 로딩 중에는 준비될 때까지 기다리세요. **8080 서버 터미널은 계속 열어둡니다.**
학습 모델의 답변 생성은 llama-server가, 임베딩은 Ollama가 담당하므로 두 서버를 모두 켜야 합니다.

### 6-3. 기존 민원·법률 자료도 필요한 경우

GitHub에는 현재 PC의 DB 데이터가 포함되지 않습니다. 새 DB에는 테스트 계정만 있으므로
로그인 후 직접 민원을 작성하거나 파일로 민원을 가져와야 목록이 채워집니다.
법률·사례에 근거한 AI 답변을 확인하려면 관리자의 **파일 자료 관리 → RAG용 데이터 삽입**에서
팀이 준비한 법률·사례 CSV를 가져오거나, 팀에서 제공한 호환 DB 백업을 별도로 복원해야 합니다.
빈 DB에서 실행만 했다고 기존 PC의 법률 자료나 답변 사례까지 자동으로 생기지는 않습니다.

기존 백업으로 옮길 때는 DB 스키마·계정 ID·벡터 차원과 사용 모델을 맞추고,
이미 같은 데이터가 들어 있는 곳에 반복 복원하지 마세요. 파일 경로를 참조하는 데이터는 원본 업로드 파일도 옮겨야 합니다.
초기화용 SQL과 실제 데이터 복원은 구분해서 진행합니다.

## 7. HWP·스캔 PDF·이미지도 처리하려면

CSV·XLSX와 텍스트가 들어 있는 PDF의 기본 추출 패키지는 `requirements.txt`로 설치됩니다.
**HWP 파서와 OCR 실행 프로그램·언어 데이터는 별도 준비가 필요합니다.**

### 7-1. HWP 텍스트 파서

HWP 파서는 메인 백엔드와 분리된 **Python 3.11** 가상환경을 사용합니다.
[Python 3.11.9 Windows 64비트 설치 파일](https://www.python.org/downloads/release/python-3119/)로 3.11도 함께 설치한 뒤 실행하세요.
백엔드의 Python 3.14를 지우거나 바꿀 필요는 없습니다.

```powershell
cd "C:\work\doc-main"
py -3.11 --version
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_hwp_parser.ps1
& ".\dcm-back\.hwp-parser\Scripts\hwp5txt.exe" --help
```

파서는 `dcm-back\.hwp-parser`에 설치되고 앱이 이 위치를 찾아 사용합니다.
위 명령의 실행 정책 옵션은 해당 PowerShell 프로세스에만 적용됩니다.

### 7-2. Tesseract OCR과 한국어·영어 데이터

1. [Tesseract 공식 문서의 Windows 안내](https://tesseract-ocr.github.io/tessdoc/Installation.html#windows)에 연결된 **UB Mannheim Windows 64비트 설치 프로그램**으로 설치합니다.
2. 기본 설치 경로는 `C:\Program Files\Tesseract-OCR`로 둡니다.
3. 프로젝트에서 사용할 한국어·영어 데이터를 다음 명령으로 받습니다.

```powershell
cd "C:\work\doc-main"
New-Item -ItemType Directory -Path ".\dcm-back\data\tessdata" -Force
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/kor.traineddata" -OutFile ".\dcm-back\data\tessdata\kor.traineddata"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata" -OutFile ".\dcm-back\data\tessdata\eng.traineddata"
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --tessdata-dir ".\dcm-back\data\tessdata" --list-langs
```

결과에 `kor`, `eng`가 모두 있어야 합니다. 프로그램 설치와 언어 데이터 준비는 별개입니다.
앱의 기본 언어 데이터 위치는 `dcm-back\data\tessdata`이므로 시스템에만 언어 파일이 있어도 이 폴더는 따로 준비해야 합니다.
다른 위치에 설치했다면 `dcm-back\.env`에 실제 경로를 지정한 뒤 백엔드·파일 워커를 재시작합니다.

```dotenv
TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe
TESSDATA_DIR=C:/work/doc-main/dcm-back/data/tessdata
```

PDF 페이지를 이미지로 만드는 데는 설치된 PyMuPDF를 사용하므로, 이 기본 경로에서는 Poppler를 추가 설치하지 않아도 됩니다.

### 7-3. 이미지 위주 HWP의 변환기

HWP의 텍스트 추출 결과가 부족하면 **HWP → PDF 변환 → 텍스트 추출/OCR**을 시도합니다.
이 경로도 사용하려면 [LibreOffice Windows x86-64](https://www.libreoffice.org/download/)를 기본 경로에 설치하세요.
앱이 `C:\Program Files\LibreOffice\program\soffice.exe`를 찾습니다.
HWP 형식이나 파일 손상에 따라 변환이 실패할 수 있으므로 실제 사용할 파일로 확인해야 합니다.

## 8. 서버 실행 후 화면·로그인 확인

VS Code 터미널 오른쪽 **`+` 버튼**으로 새 PowerShell 터미널을 만들 수 있습니다.
서버가 실행 중인 터미널에는 다음 서버 명령을 이어서 입력하지 않고, **서버마다 새 터미널**을 사용합니다.

| 실행 대상 | 주소 | 준비 확인 |
| --- | --- | --- |
| PostgreSQL 17 + pgvector | `127.0.0.1:5433` | `pg_isready`의 `accepting connections` |
| llama-server | `http://127.0.0.1:8080` | `/health`의 `status: ok` |
| Ollama | `http://127.0.0.1:11434` | `/api/tags`의 모델 목록 |
| FastAPI | `http://127.0.0.1:8000` | `/health/database`의 DB 연결 확인 |
| React | `http://localhost:5173` | 브라우저 로그인 화면 |

### 8-1. DB·AI 서버 준비

Docker Desktop을 실행하고 DB를 켭니다.

```powershell
docker start doc-main-db
```

6번과 기존 어댑터 가이드에 따라 **llama-server와 Ollama**를 켭니다.
GPU 메모리가 작은 환경에서는 llama-server를 먼저 준비하고 임베딩 요청을 시작하세요.

### 8-2. 백엔드 실행 — 새 터미널

```powershell
cd "C:\work\doc-main\dcm-back"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`Application startup complete.`와 `Uvicorn running on http://127.0.0.1:8000`이 나오면 실행된 것입니다.
LLM 시작 로그의 `backend=llama_server`, `health=200`, 어댑터 ID도 확인하세요.
백엔드가 켜져도 모델 연결이 실패하면 AI 기능은 정상 동작하지 않을 수 있습니다.

### 8-3. 프론트엔드 실행 — 새 터미널

```powershell
cd "C:\work\doc-main\dcm-front"
npm.cmd run dev -- --host localhost --port 5173 --strictPort
```

브라우저에서 **[http://localhost:5173](http://localhost:5173)**을 엽니다.
화면의 **처리 서버 주소**는 프론트 주소가 아니라 **`http://127.0.0.1:8000`**으로 설정합니다.
로그인은 맨 아래 테스트 계정을 사용하세요.

### 8-4. 새 터미널에서 연결 확인

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/health/database
curl.exe http://127.0.0.1:8080/health
curl.exe http://127.0.0.1:11434/api/tags
```

로그인 → 일반 사용자로 민원 작성 → 관리자로 해당 부서 목록 확인 → AI 추천 답변 요청 순서로 확인합니다.
파일 민원은 관리자 계정에서 업로드합니다. 기본 민원 CSV는 `제목,내용`, RAG 근거 CSV는 `제목,본문` 등의 열로 준비합니다.
업로드 시 필요한 파일 워커는 자동 실행됩니다. 직접 실행해 로그를 보고 싶다면 별도 터미널에서 다음을 사용합니다.

```powershell
cd "C:\work\doc-main\dcm-back"
.\.venv\Scripts\python.exe -m app.worker
```

수동 실행한 서버·워커를 끌 때는 각 터미널에서 **Ctrl+C**를 누릅니다.
이 문서의 DB를 중지하려면 `docker stop doc-main-db`를 사용하며, 볼륨은 삭제하지 않습니다.

## 9. 다음 실행부터 간단하게 켜는 방법

처음 설치·DB 생성·모델 다운로드를 매번 반복할 필요는 없습니다.

1. Docker Desktop을 실행하고 `docker start doc-main-db`로 DB를 켭니다.
2. Ollama 앱을 실행합니다.
3. 어댑터 가이드의 8080 실행 명령, 8-2의 백엔드 명령, 8-3의 프론트 명령을 각각 실행합니다.

또는 이미 들어 있는 **`start-doc-main.cmd`**를 사용할 수 있습니다.
이 실행 파일은 **Windows 작업 스케줄러에 수동 실행 작업을 등록**해 llama-server·백엔드·프론트를 숨김 실행합니다.
예약 시각이나 PC 로그인 시 자동 실행하는 기능은 아닙니다. **DB와 Ollama는 이 파일이 켜 주지 않으므로 먼저 실행해야 합니다.**

간편 실행은 `scripts/run_local_service.ps1`의 다음 고정 경로·설정이 준비된 경우에 사용하세요.

- llama-server: `C:\llama-server\llama-server.exe`
- 베이스: 프로젝트의 `model-training\modelfiles\llama-server\gemma-4-E4B-it-Q4_K_M.gguf`
- 어댑터: 같은 폴더의 `complaint-lora-version_3-f16.gguf`
- Python: 프로젝트의 `dcm-back\.venv\Scripts\python.exe`
- 모델 설정: 컨텍스트 32768, 동시 슬롯 1, GPU 레이어 10

다른 모델 이름·설치 경로·GPU 설정을 사용한다면 먼저 수동 실행 방식으로 확인하세요.
고정 경로가 준비되어 있다면 프로젝트 루트에서 다음 한 줄로 실행합니다.

```powershell
cd "C:\work\doc-main"
.\start-doc-main.cmd
```

준비가 완료되면 `http://localhost:5173`을 엽니다. 실행 로그는 프로젝트의 `logs` 폴더에 남습니다.
숨김 실행된 서버는 터미널을 닫아도 계속 실행되므로, 다시 같은 포트에 수동 실행하지 마세요.
중지할 때는 Windows **작업 스케줄러**에서 `ComplaintAI-doc-main-llama`, `ComplaintAI-doc-main-backend`,
`ComplaintAI-doc-main-frontend` 작업을 찾아 **끝내기**로 중지하고 아래 포트 확인 명령으로 남은 프로세스가 있는지 확인합니다.

## 10. 자주 막히는 부분

| 증상 | 확인·해결 방법 |
| --- | --- |
| `No suitable Python runtime found` | Python 3.14 64비트를 설치하고 `py -3.14 --version` 확인. HWP 설치 단계는 별도로 3.11이 필요 |
| `ModuleNotFoundError`, `langchain_openai` 없음 | `dcm-back`에서 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` 실행. 서버도 같은 Python으로 실행 |
| npm 실행 정책 오류 | `npm` 대신 이 문서처럼 `npm.cmd` 사용 |
| `EBADENGINE` 또는 Node 버전 오류 | Node.js 24 LTS, 24.15.0 이상으로 설치 후 VS Code 재시작 |
| Docker 엔진에 연결할 수 없음 | Docker Desktop이 실행 중이고 Linux 컨테이너/WSL 2 엔진이 준비됐는지 확인 |
| `type vector does not exist` | 일반 PostgreSQL 이미지가 아니라 pgvector 이미지인지, 초기화 SQL이 성공했는지 확인 |
| DB `password authentication failed` | `.env`의 DB 계정·비밀번호·포트를 실제 컨테이너와 비교. 신규 DB 예시와 기존 PC의 비밀번호는 다를 수 있음 |
| 로그인이 안 됨 | `/health/database`, 화면의 처리 서버 주소, 아래 계정 확인. `.env` 변경 후 백엔드 재시작 |
| 관리자 권한 403 | 담당 부서 계정인지 확인. 다른 부서 민원은 조회만 가능하며 변경·답변은 담당 관리자만 가능 |
| AI 답변이 안 나옴 | 8080 `/health`와 `/lora-adapters`, 11434 `/api/tags`, 백엔드 로그 확인. 검색할 법률·사례 자료가 실제 DB에 있는지도 확인 |
| 임베딩 실패·검색 결과 없음 | `bge-m3:latest` 다운로드·서버 실행 여부와 1024차원 설정 확인. 다른 모델의 벡터를 혼합하지 않음 |
| HWP 추출기 없음 | 7-1의 별도 파서 설치 확인. 이미지 위주 문서는 LibreOffice·OCR도 확인 |
| 한국어 OCR이 안 됨 | Tesseract 실행 파일과 앱에서 사용하는 `tessdata`의 `kor`·`eng` 두 파일 확인 |
| 파일 진행률이 멈춤 | `dcm-back/data/csv-worker.stderr.log` 확인 또는 8-4의 워커 직접 실행. 기존 워커는 설정 변경 후 재시작 |
| `Port ... is already in use`, `[WinError 10048]` | 같은 포트에 이미 서버가 실행 중. 기존 창에서 Ctrl+C로 종료하거나 해당 서버 화면을 그대로 사용 |

포트 점유 상태는 PowerShell에서 확인할 수 있습니다.

```powershell
Get-NetTCPConnection -LocalPort 5433,8000,8080,5173,11434 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess
```

`OwningProcess`가 PID입니다. 작업 관리자에서 어떤 프로그램인지 확인한 뒤 본인이 실행한 서버만 종료하세요.
5433이 이미 사용 중이라고 기존 DB나 볼륨을 지우지 마세요. 기존 DB를 사용할지 새 포트로 별도 구성할지 먼저 정하고,
새 포트를 쓴다면 Docker의 호스트 포트·`.env`·pgAdmin 접속 포트를 함께 맞춥니다.

## 개발용 로그인 계정

아래 계정은 **현재 `database/init.sql`로 새 DB를 만들었을 때** 사용할 수 있습니다.
팀의 기존 DB 백업을 복원했다면 그 백업에 들어 있는 계정 정보를 사용하세요.

### 일반 사용자

| 계정 이름 | 로그인 ID | 비밀번호 |
| --- | --- | --- |
| 테스트 일반 사용자 A | `user-a` | `UserA!2026` |
| 테스트 일반 사용자 B | `user-b` | `UserB!2026` |

### 관리자

아래는 현재 `database/init.sql`로 새 DB를 생성했을 때의 개발용 계정입니다.
`admin-fire`는 기존 로그인 ID를 유지하며 새 초기 부서 구성에서는 노동·기업을 담당합니다.
기존 DB의 부서 번호는 유지하며, 부서 미지정 초기 관리자 계정은 해당 부서로 연결을 복구합니다.
DB에서 부서명을 변경하면 같은 부서 ID에 연결된 관리자 이름도 자동으로 갱신됩니다.
자세한 동작과 적용 결과는 [관리자 부서 동기화](docs/admin-department-sync-2026-10-26.md)를 참고하세요.
초기화·업데이트 방법과 이번 통합 내용은 [변경 검토](docs/upstream-integration-2026-10-21.md)를 참고하세요.

| 계정 이름 | ID | 비밀번호 | 담당 부서 |
| --- | --- | --- | --- |
| 문화·행정·안전 관리자 | `admin-administration-safety` | `AdminService!2026` | 문화·행정·안전 |
| 교통·국토 관리자 | `admin-land-transport` | `AdminLand!2026` | 교통·국토 |
| 주택·건축 관리자 | `admin-housing` | `AdminHousing!2026` | 주택·건축 |
| 환경·위생 관리자 | `admin-environment` | `AdminEnvironment!2026` | 환경·위생 |
| 보건·복지 관리자 | `admin-welfare` | `AdminWelfare!2026` | 보건·복지 |
| 노동·기업 관리자 | `admin-fire` | `AdminFire!2026` | 노동·기업 |
| 기타 관리자 | `admin-other` | `AdminOther!2026` | 기타 |
