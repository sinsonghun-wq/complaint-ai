# 관리자 기능 통합 (2026-10-20)

`complaint AI-main/complaintai-react-fastapi`의 업무 기능을 `doc-main`의
`dcm-back` / `dcm-front` 구조에 맞춰 통합했다. 바깥 `backend`, 원본 프로젝트의
RAG 서비스, 학습 파일, 환경 설정은 가져오지 않았다. GitHub 업로드는 하지 않았다.

## 유지한 설정과 기능

- `dcm-back/.env`의 DB 연결, 인증 키, 임베딩·LLM 설정을 유지했다.
- `app/llm.py`, `app/prompts.py`, `app/rag.py`, `app/rag_api.py`,
  `app/chat_intent.py`, `app/complaint_search.py`를 유지했다.
- `AIRecommendations.jsx`, `CitizenComplaintTools.jsx`, `ComplaintFilters.jsx`,
  기존 민원인용 `departments.json`을 유지했다.
- `agent.py`의 RAG 검색(`aiFAQ`), 대화 기억, 모델 호출은 그대로다.
  본인 민원 조회 도구(`selectComplaintT`)에만 취소·삭제 민원 제외 조건을 추가했다.
- 임베딩 생성 함수와 모델은 그대로다. 파일 가져오기의 벡터 차원이 맞지 않으면
  원문을 저장하고 검색 벡터만 보류한다.

## 추가·갱신한 업무 기능

- PDF/HWP/XLSX/XLS/CSV/이미지 추출, 다중 시트·500건 단위 처리,
  BOM/CP949 판별, 한글·영어 OCR, 손상 파일 오류 처리.
- PDF의 실제 줄바꿈 위치를 기록하는 중복 판별. 의미 있는 띄어쓰기·숫자·문장부호는
  구분하고, 개인정보 마스킹 전 해시와 서명된 미리보기 토큰을 사용한다.
  기존 중복 데이터는 지우지 않고 이후 중복 접수를 막는다.
- 별도 `services/file_rules.py`와 `rule_classifier.py`: 문맥·가중치 기반 파일 분류.
  모호한 결과는 검토 필요로 기록하며, `LLM_IMPORT_ENABLED` 설정을 그대로 따른다.
  일반 AI 대화·분석 프롬프트는 변경하지 않았다.
- 관리자 삭제 보관함 UI/API: 부서 필터, 페이지 크기, 원문 별도 조회,
  담당 부서 복원·선택/부서별 영구 삭제, 비밀번호 확인, 관리자 전용 복원.
  원본의 전체 10,000건 보관 및 초과 시 자동 정리 정책도 적용했다.
- 부서 ID를 기준으로 접수·답변·전달·삭제 권한을 검사한다.
  최종 편집 내용을 답변 전송과 같은 트랜잭션으로 저장하고 민원인 화면에 반영한다.
- CSV 워커를 웹 앱과 분리해 가져오기 작업·체크포인트·재시도를 유지한다.
  서버 수명 주기에서 중단된 일반 민원 분류도 재개한다.

## 기존 DB와의 연결

DB의 기존 부서 ID/이름, 계정, 원문, 분류명, 벡터는 유지했다.
관리자 화면은 `/api/departments`의 실제 DB 목록을 사용한다.
`보건·복지` → 기존 `보건복지`처럼 이름이 다른 경우 기존 ID로 연결한다.
현재 DB에 없는 부서 분류(예: 노동·기업)는 `기타` ID에 배정하되 원래 분류명은 보존한다.
부서를 새로 만들거나 계정을 재배정하지 않는다.

기존 민원 중 비어 있던 `department_id`만 연결했다. 삭제 보관함·중복 판별용 컬럼과
인덱스는 `database/migrations/20261019_*.sql`을 추가 방식으로 적용한다.
기존 `database/init.sql`과 계정 seed는 변경하거나 운영 DB에서 실행하지 않았다.
테스트용 초기화 SQL은 `dcm-back/tests/fixtures`에 별도로 둔다.

## 실행·검증

프로젝트 루트 PowerShell:

```powershell
# HWP 추출기 최초 설치 (Python 3.11 필요)
powershell -ExecutionPolicy Bypass -File .\scripts\setup_hwp_parser.ps1

# 백엔드
cd dcm-back
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 백엔드 검사: 별도의 임시 DB만 생성·삭제한다.
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 프론트 (별도 터미널)
cd C:\Users\1Class_07\doc-main\dcm-front
npm run dev
npm run test:admin
npm run build
```

백엔드 74개 검사와 추가 본인 민원 도구 검사 1개, 관리자·현행 AI 화면 검사 56개,
진행률 검사 4개가 통과했다. PDF/HWP/XLSX 72건 샘플의 입력 순서별 중복 제거도
임시 DB에서 검증했다. 실제 DB는 계정 9개, 민원 142건, 부서 자료 53,710건을
유지했고 원문·분류명·민원 임베딩 및 환경 파일의 변경 전후 지문이 일치했다.

기존 `npm test` 중 `AIRecommendations.test.jsx`와 `ChatToolsIntegration.test.jsx`는
과거 `/api/ai-recommendations/intent` 계약을 전제로 한다. 이식 전 원본 코드에서도
똑같이 28개가 실패하는 것을 별도로 재현했다. 이를 통과시키려고 현재 AI 동작을
교체하지 않았으며, 현재 `/api/chat` 계약을 검사하는 `CurrentAIChat.test.jsx`를 추가했다.
로그와 이식 전 사본은 Git에서 제외된 `logs/`에 보관한다.
