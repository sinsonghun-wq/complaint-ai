# ComplaintAI

메인 구현은 `codex/react-fastapi-migration` 브랜치의 React + FastAPI 이식본이다.

## 처음 실행하기

1. Docker Desktop을 실행한 뒤 프로젝트 루트에서 `docker compose up -d`를 실행한다.
2. 빈 PostgreSQL 볼륨이라면 `database/init.sql`이 스키마를 만들고, `database/seed_demo_accounts.sql`이 개발용 일반 사용자 2개와 9개 부서 관리자 9개를 생성한다.
3. `complaintai-react-fastapi/.env.example`을 `complaintai-react-fastapi/.env`로 복사한다.
4. `complaintai-react-fastapi`에서 Python 의존성을 설치하고, `frontend`에서 `npm install` 후 `npm run build`를 실행한다.
5. `complaintai-react-fastapi`에서 `uvicorn app.main:app --host 127.0.0.1 --port 8000`으로 실행한다.

브라우저에서 `http://127.0.0.1:8000`을 연다. PostgreSQL은 로컬 포트 `5433`을 사용한다.

## 데이터베이스 초기화 주의사항

- `database/init.sql`은 Docker가 **새로운** `postgres_data` 볼륨을 만들 때 한 번 실행된다.
- 기존 DB에 수동 실행하면 **프로젝트 테이블 11개와 기존 데이터가 삭제되고 최신 스키마로 재생성된다.** 운영 DB나 보존해야 하는 데이터에 실행하지 않는다.
- 기존 DB에는 자동으로 다시 실행되지 않는다. 팀원 DB 재생성 및 데이터 복원 절차는 [database/README.md](database/README.md)를 참고한다.
- 데이터 보존이 목적이면 `init.sql`이 아닌 별도 마이그레이션을 사용한다. 초기화는 하나의 트랜잭션이며, 외부 의존성이 있으면 삭제를 중단하고 롤백한다.
- 초기 계정은 개발·검증 전용이다. 운영 환경에서는 초기 비밀번호와 `AUTH_TOKEN_SECRET`을 반드시 교체해야 한다.
