# ComplaintAI React + FastAPI 이식본

기존 정적 JavaScript + Node/Express 구현을 보존한 채 별도로 만든 React + Python/FastAPI 구현이다.

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

# FastAPI가 frontend/dist를 제공한다.
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

브라우저에서 `http://127.0.0.1:8000`을 연다. 개발 중에는 `frontend`에서 `npm run dev`를 별도로 실행할 수 있다.

## 검증용 CSV

`tests/fixtures/complaints-smoke.csv`는 다음을 확인하기 위한 2행 CSV다.

- 일반 사용자 업로드와 CSV 작업 완료 상태
- 행별 분류 및 개인 카테고리 보관함 저장
- 다른 일반 사용자의 목록 격리
- 교통·주차 및 환경·위생 관리자 부서 처리 목록 표시

테스트 후 생성된 민원은 삭제된 데이터에 남기지 말고 영구 삭제한다.

## 원본과의 관계

- 원본: `../backend`, `../complaintai-web`
- 이식본: 이 디렉터리 전체
- 두 구현은 같은 PostgreSQL 스키마를 사용할 수 있으나 동시에 같은 대용량 CSV 작업을 실행하지 않는다.
