# ComplaintAI React + FastAPI 이식본

기존 정적 JavaScript + Node/Express 구현을 보존한 채 별도로 만든 React + Python/FastAPI 구현이다.

## 현재 부서·분류 체계

- 행정·안전
- 국토·교통
- 주택건축
- 환경·위생
- 보건복지
- 소방
- 기타

`국토·교통`은 기존 `교통·주차`와 `도로·시설물`을 통합한 분류다. 법률은 독립 분류하지 않으며, 새 민원은 실제 내용의 담당 분야로 분류한다. 기존 법률 민원은 사후에 담당 분야를 안전하게 판단할 수 없으므로 `기타`로 이전한다.

기존 데이터베이스를 7개 체계로 이전할 때는 `scripts/migrate_to_seven_departments.sql`을 실행한다. 이 스크립트는 기존 분류·부서 참조를 함께 바꾼다.

## 테스트 계정

아래 계정은 로컬 개발·검증 전용 계정이다. 실제 서비스 환경에서는 초기 비밀번호를 문서에 저장하지 않고, 각 계정의 비밀번호를 별도로 설정해야 한다.

### 일반 사용자

| 계정 이름 | ID | 비밀번호 |
| --- | --- | --- |
| 테스트 일반 사용자 A | `user-a` | `UserA!2026` |
| 테스트 일반 사용자 B | `user-b` | `UserB!2026` |

### 관리자

| 계정 이름 | ID | 비밀번호 | 담당 부서 |
| --- | --- | --- | --- |
| 행정·안전 관리자 | `admin-administration-safety` | `AdminService!2026` | 행정·안전 |
| 국토·교통 관리자 | `admin-land-transport` | `AdminLand!2026` | 국토·교통 |
| 주택건축 관리자 | `admin-housing` | `AdminHousing!2026` | 주택건축 |
| 환경·위생 관리자 | `admin-environment` | `AdminEnvironment!2026` | 환경·위생 |
| 보건복지 관리자 | `admin-welfare` | `AdminWelfare!2026` | 보건복지 |
| 소방 관리자 | `admin-fire` | `AdminFire!2026` | 소방 |
| 기타 관리자 | `admin-other` | `AdminOther!2026` | 기타 |

## 실행

```powershell
cd complaintai-react-fastapi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env

# React 개발 서버(처음 한 번 npm install 필요)
cd frontend
npm install
npm run build
cd ..

# 터미널 1: FastAPI가 frontend/dist를 제공한다.
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 터미널 2: 대용량 CSV 작업 전용 워커를 별도로 실행한다.
.\scripts\run_import_worker.ps1
```

브라우저에서 `http://127.0.0.1:8000`을 연다. 개발 중에는 `frontend`에서 `npm run dev`를 별도로 실행할 수 있다.

CSV 작업은 PostgreSQL 작업 큐에 등록되고 별도 워커가 처리한다. 웹 서버를 재시작해도 워커가 계속 실행 중이면 작업은 유지된다. 워커가 중단돼 heartbeat가 5분 이상 갱신되지 않은 작업은 `실패`로 전환되며, 업로드 화면의 `재처리` 버튼으로 마지막 500건 저장 지점부터 다시 시작할 수 있다.

## 민원 상태와 권한

| 상태 | 민원인 수정 | 민원인 삭제(취소) | 관리자 답변 | 관리자 삭제(취소) |
|---|---|---|---|---|
| 접수 대기 | 가능 | 가능 | 가능 | 사유 입력 후 가능 |
| 접수 됨 | 불가 | 가능 | 가능 | 사유 입력 후 가능 |
| 완료 | 불가 | 불가 | 불가 | 불가 |
| 취소 | 불가 | 불가 | 불가 | 불가 |

- 관리자가 `민원 접수`로 원문을 열면 화면 상태가 `접수 대기 → 접수 됨`으로 바뀐다. 기존 데이터와 처리 규칙의 호환성을 위해 DB/API 상태 값 `접수`, `진행중`은 유지하며, 화면에서는 각각 `접수 대기`, `접수 됨`으로 표시한다.
- 답변 임시 저장은 완료가 아니다. 답변 전송 시에만 `완료`로 바뀐다. 초안을 지워도 이미 확인한 민원은 `진행중` 상태를 유지한다.
- 민원인이 취소하면 본인 목록에서 사라지고, 관리자 목록에는 취소 안내만 남는다. 관리자가 취소하면 민원인 목록에 취소 사유가 표시된다. 관리자에게는 취소된 원문·답변을 반환하지 않는다.
- 취소 사유와 주체는 DB에 보존한다. 업무상 취소(`complaint_status`)와 기존 보관함 삭제(`deleted_at`)는 구분한다. 취소된 민원은 복원할 수 없다.
- 민원 접수 화면은 2초마다 최신 상태를 확인해 취소·부서 변경 시 답변 입력을 닫는다. 서버는 행 잠금으로 취소와 답변 전송의 동시 실행을 차단한다.
- 완료 민원의 `재민원 작성`은 새 접수 건을 만들고 이전 제목·원문·전송 답변을 `previous_context` 스냅샷으로 보존한다. 재민원은 이전 담당 부서로 전달된다.
- 카테고리 일괄 취소는 사유를 요구하며 완료·취소 건은 제외한다. 전체 부서 테스트 정리도 완료·취소 건을 변경하지 않는다.
- 기존 DB의 필요한 컬럼은 FastAPI 시작 시 추가한다. 신규 DB는 루트 `database/init.sql`로 생성한다.

검증: `python -m unittest discover -s tests -v` (설정된 PostgreSQL 사용, 테스트 생성 데이터만 정리, LLM/임베딩 호출은 대체하여 업무 규칙 검증).

## 문서 추출 방식

- XLSX: `openpyxl`로 모든 시트를 순회한다. 각 시트에서 `제목`과 `신청원인`(또는 민원 본문 열)이 함께 있는 헤더 행을 찾고, 이후의 각 행을 별도 민원으로 처리한다. 안내 시트는 자동으로 건너뛴다.
- PDF: 페이지별로 `pypdf`와 `pdfplumber`의 텍스트 추출 결과를 비교한다. 제목·`신청원인` 구조를 가진 페이지는 각각 하나의 민원으로 분리한다. 텍스트 품질이 낮은 페이지에만 PyMuPDF 렌더링과 Tesseract OCR을 재시도한다.
- HWP: `hwp5txt`(pyhwp)가 있으면 먼저 텍스트를 추출한다. 낮은 품질이면 LibreOffice의 HWP-to-PDF 변환 결과를 PDF/OCR 흐름으로 다시 처리한다. Windows에서 HWP는 별도 설치가 필요하므로 `hwp5txt` 또는 LibreOffice가 없는 환경에서는 업로드가 안내 오류로 끝난다.

### HWP 파서 준비

HWP 기능은 프로젝트에 포함하지 않는 별도 파서 환경을 사용한다. 새 컴퓨터에서 처음 한 번 다음을 실행한다. Python 3.11과 인터넷 연결이 필요하다.

```powershell
cd C:\경로\complaintAI\complaintai-react-fastapi
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_hwp_parser.ps1
```

성공하면 `.hwp-parser\Scripts\hwp5txt.exe`가 생성된다. 이 폴더는 Git에 올리지 않으며, FastAPI 서버는 해당 실행 파일을 자동으로 찾는다. 파서 또는 LibreOffice를 찾지 못한 경우에는 서버가 HTTP 500 대신 설치 안내가 담긴 HTTP 503을 반환한다.

현재 HWP 표는 `hwp5txt`가 표의 셀 배치를 보장하지 않으므로, 표 구조를 그대로 보존하는 처리(예: `hwp5html` 기반 셀 파싱)는 별도 보완 항목이다.

## 검증용 CSV

`tests/fixtures/complaints-smoke.csv`는 다음을 확인하기 위한 2행 CSV다.

- 일반 사용자 업로드와 CSV 작업 완료 상태
- 행별 분류 및 개인 카테고리 보관함 저장
- 다른 일반 사용자의 목록 격리
- 국토·교통 및 환경·위생 관리자 부서 처리 목록 표시

테스트 후 생성된 민원은 삭제된 데이터에 남기지 말고 영구 삭제한다.

## 원본과의 관계

- 원본: `../backend`, `../complaintai-web`
- 이식본: 이 디렉터리 전체
- 두 구현은 같은 PostgreSQL 스키마를 사용할 수 있으나 동시에 같은 대용량 CSV 작업을 실행하지 않는다.
