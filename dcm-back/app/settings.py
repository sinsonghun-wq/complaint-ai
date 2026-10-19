from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
# 개발 이식 기간에는 원본 Node 백엔드의 로컬 DB 설정을 읽을 수 있다.
# 새 환경에서는 이식본 자체의 .env가 우선이며, 원본 .env를 저장소에 복사하지 않는다.
if not (ROOT / ".env").exists():
    load_dotenv(ROOT.parent / "backend" / ".env")

PORT = int(os.getenv("PORT", "8000"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
AUTH_TOKEN_SECRET = os.getenv("AUTH_TOKEN_SECRET", "change-this-development-secret")
WEB_ORIGINS = [value.strip() for value in os.getenv("WEB_ORIGIN", "*").split(",")]
FILE_STORAGE_DIR = Path(os.getenv("FILE_STORAGE_DIR", str(ROOT / "data" / "uploads"))).resolve()
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "500"))
CSV_MAX_UPLOAD_BYTES = int(os.getenv("CSV_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
DOCUMENT_MAX_UPLOAD_BYTES = int(os.getenv("DOCUMENT_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
# --- LLM 백엔드 선택 -------------------------------------------------------------
# ollama       : 지금까지의 방식. LLM_MODEL(병합 모델 또는 일반 모델) 하나를 모든 작업이 같이 씁니다.
# llama_server : llama.cpp 의 llama-server 에 베이스 GGUF + 어댑터(LoRA) GGUF 를 올려 두고,
#                작업마다 요청에 어댑터 on/off(scale 1/0)를 실어 보냅니다. (모델은 메모리에 하나만 올라감)
# 임베딩은 이 값과 상관없이 아래 EMBEDDING_PROVIDER 설정(기본 Ollama)을 그대로 씁니다.
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama").strip().lower()
if LLM_BACKEND not in ("ollama", "llama_server"):
    raise ValueError(f"LLM_BACKEND 는 ollama 또는 llama_server 여야 합니다: {LLM_BACKEND!r}")

LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
# [ollama 전용] CSV 열 매핑에만 다른 모델을 쓸 때 (비우면 LLM_MODEL). 파인튜닝 모델이 학습하지 않은 작업이라 일반 모델을 지정
CSV_MAPPING_LLM_MODEL = os.getenv("CSV_MAPPING_LLM_MODEL", "").strip() or LLM_MODEL

# [llama_server 전용]
LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
# DB(llm_model 컬럼)·API 응답에 남길 이름. llama-server 는 모델 이름을 보지 않으므로 기록용입니다.
LLAMA_SERVER_BASE_NAME = os.getenv("LLAMA_SERVER_BASE_NAME", "gemma4-e4b").strip()
LLAMA_SERVER_ADAPTER_NAME = os.getenv("LLAMA_SERVER_ADAPTER_NAME", "complaint-lora").strip()
# llama-server 를 --lora 로 띄울 때의 어댑터 순번 (첫 번째 --lora 가 0). GET /lora-adapters 로 확인할 수 있습니다.
LLAMA_SERVER_LORA_ID = int(os.getenv("LLAMA_SERVER_LORA_ID", "0"))
# 어댑터를 켤 작업 (쉼표 구분). analysis=민원 분석, chat=챗봇, csv_mapping=CSV 열 매핑
# 기본은 학습·평가한 두 작업만 켜고, CSV 열 매핑은 베이스(일반) 모델로 둡니다.
LLM_ADAPTER_TASKS = frozenset(t.strip() for t in os.getenv("LLM_ADAPTER_TASKS", "analysis,chat").split(",") if t.strip())
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "600000"))
# LLM 컨텍스트 길이 (민원 분석·챗봇·CSV 매핑 공통). 분석 프롬프트 고정 부분(약 3,000자) + 원문(최대 1,500자) + 출력(512토큰)이
# 들어가야 하므로 8192 를 기본으로 합니다. 로그의 prompt_eval_count 를 보고 조정하세요.
#8192로 하면 초과 오류가 너무 자주 발생해서 더 넉넉하게 32768로 수정
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "32768"))
LLM_IMPORT_ENABLED = os.getenv("LLM_IMPORT_ENABLED", "true").lower() == "true"
LLM_IMPORT_CONCURRENCY = max(1, int(os.getenv("LLM_IMPORT_CONCURRENCY", "1")))
CSV_LLM_CONFIDENCE_THRESHOLD = min(1.0, max(0.0, float(os.getenv("CSV_LLM_CONFIDENCE_THRESHOLD", "0.5"))))
CSV_RULE_OTHER_MIN_CONTENT_CHARS = max(1, int(os.getenv("CSV_RULE_OTHER_MIN_CONTENT_CHARS", "80")))
IMPORT_PROGRESS_ROWS = max(1, int(os.getenv("IMPORT_PROGRESS_ROWS", "10")))
IMPORT_HEARTBEAT_TIMEOUT_SECONDS = max(60, int(os.getenv("IMPORT_HEARTBEAT_TIMEOUT_SECONDS", "300")))
IMPORT_WORKER_POLL_SECONDS = max(1, int(os.getenv("IMPORT_WORKER_POLL_SECONDS", "2")))
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "").rstrip("/")
EMBEDDING_TIMEOUT_MS = int(os.getenv("EMBEDDING_TIMEOUT_MS", "60000"))
# 실제로 임베딩에 쓰는 모델 이름 (EMBEDDING_PROVIDER 에 따라 .env 값 중 하나). DB 기록·API 응답에 이 값을 씁니다.
ACTIVE_EMBEDDING_MODEL = OLLAMA_EMBEDDING_MODEL if EMBEDDING_PROVIDER == "ollama" else EMBEDDING_MODEL
TESSDATA_DIR = Path(os.getenv("TESSDATA_DIR", str(ROOT / "data" / "tessdata"))).resolve()
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()
