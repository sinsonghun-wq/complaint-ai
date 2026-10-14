from pathlib import Path
from .ai import CATEGORIES
from .settings import CSV_LLM_CONFIDENCE_THRESHOLD, CSV_MAX_UPLOAD_BYTES, CSV_RULE_OTHER_MIN_CONTENT_CHARS, DOCUMENT_MAX_UPLOAD_BYTES, FILE_STORAGE_DIR, IMPORT_PROGRESS_ROWS, LLM_IMPORT_CONCURRENCY, LLM_IMPORT_ENABLED, MAX_BATCH_SIZE, TESSDATA_DIR, TESSERACT_CMD, WEB_ORIGINS

DEPARTMENT_CATEGORIES = {category: [category] for category in CATEGORIES}
STATUSES = ["접수", "진행중", "완료", "취소"]
STATIC_DIR = Path(__file__).resolve().parents[1] / "frontend" / "dist"
FILE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

TITLE_HEADER_WORDS = ["민원 제목", "민원제목", "제목", "title", "subject", "질문명", "문의제목", "사건명"]
CONTENT_HEADER_WORDS = ["민원 내용", "민원내용", "신청원인", "신청내용", "신청사항", "문의내용", "상세내용", "원문", "내용", "complaint", "content", "질문"]