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

## 원본과의 관계

- 원본: `../backend`, `../complaintai-web`
- 이식본: 이 디렉터리 전체
- 두 구현은 같은 PostgreSQL 스키마를 사용할 수 있으나 동시에 같은 대용량 CSV 작업을 실행하지 않는다.
