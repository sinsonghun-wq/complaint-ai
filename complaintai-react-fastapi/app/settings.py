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
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "60000"))
# 민원 분석 프롬프트의 컨텍스트 길이. 카테고리 정의 블록(약 2,200자)이 들어가므로 4096 이상을 권장합니다.
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "4096"))
LLM_IMPORT_ENABLED = os.getenv("LLM_IMPORT_ENABLED", "true").lower() == "true"
LLM_IMPORT_CONCURRENCY = max(1, int(os.getenv("LLM_IMPORT_CONCURRENCY", "1")))
CSV_LLM_CONFIDENCE_THRESHOLD = min(1.0, max(0.0, float(os.getenv("CSV_LLM_CONFIDENCE_THRESHOLD", "0.5"))))
CSV_RULE_OTHER_MIN_CONTENT_CHARS = max(1, int(os.getenv("CSV_RULE_OTHER_MIN_CONTENT_CHARS", "80")))
IMPORT_PROGRESS_ROWS = max(1, int(os.getenv("IMPORT_PROGRESS_ROWS", "10")))
IMPORT_HEARTBEAT_TIMEOUT_SECONDS = max(60, int(os.getenv("IMPORT_HEARTBEAT_TIMEOUT_SECONDS", "300")))
IMPORT_WORKER_POLL_SECONDS = max(1, int(os.getenv("IMPORT_WORKER_POLL_SECONDS", "2")))
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-4B")
OLLAMA_EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b")
EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "").rstrip("/")
EMBEDDING_TIMEOUT_MS = int(os.getenv("EMBEDDING_TIMEOUT_MS", "60000"))
# 실제로 임베딩에 쓰는 모델 이름 (EMBEDDING_PROVIDER 에 따라 .env 값 중 하나). DB 기록·API 응답에 이 값을 씁니다.
ACTIVE_EMBEDDING_MODEL = OLLAMA_EMBEDDING_MODEL if EMBEDDING_PROVIDER == "ollama" else EMBEDDING_MODEL
TESSDATA_DIR = Path(os.getenv("TESSDATA_DIR", str(ROOT / "data" / "tessdata"))).resolve()
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()
