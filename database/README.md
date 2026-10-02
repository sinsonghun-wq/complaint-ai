# 개발 DB 구조 동기화

`init.sql`은 PostgreSQL 16 + pgvector 기준의 **전체 재생성용** SQL이다. 기존 테이블이 있으면 삭제하고 최신 컬럼·관계·인덱스로 다시 생성한다. 스키마 동기화와 데이터 동기화는 별도 작업이다.

## 반드시 확인

- 실행 대상 DB의 기존 계정·민원·답변·파일 작업·문서·조직·감사 데이터가 삭제된다. 데이터가 필요하면 먼저 백업한다.
- FastAPI와 CSV 워커를 중단한다. 실행 중인 작업이 초기화된 DB에 다시 데이터를 쓰지 않도록 한다.
- PostgreSQL 서버/DB 이름을 확인한다. `DROP SCHEMA`/`DROP DATABASE`는 사용하지 않고, `public`의 명시된 프로젝트 테이블 11개만 재생성한다.
- SQL 전체를 실행한다. 한 트랜잭션이므로 오류가 나면 `ROLLBACK;`을 실행하고 원인을 해결한다. 다른 테이블이나 뷰가 의존하면 `CASCADE`로 함께 삭제하지 않고 실패한다.
- 현재 DB에 남아 있는 `department_documents`, `organizations`, `organization_members`, `audit_events`도 포함한다. 제거된 UI 기능을 다시 활성화하는 것은 아니다.

## 팀원이 이미 DB를 사용 중인 경우

1. 동기화 원본의 백업을 준비한다. 원본 서버도 쓰기 작업을 중단하면 작업 상태와 파일이 일관된 스냅샷으로 전달된다. 개인정보와 계정 비밀번호 해시는 GitHub에 올리지 않고 별도 안전한 경로로 전달한다.
2. 대상 PC의 FastAPI/CSV 워커를 중단하고 대상 DB를 백업한다.
3. pgAdmin에서 **대상 DB**의 Query Tool을 열고 `init.sql` 파일 전체를 실행한다. 테스트 계정은 생성되지 않아 실제 계정 데이터 복원 시 ID 충돌을 피한다.
4. 최신 구조와 호환되는 **data-only** 백업을 복원한다. schema+data 전체 백업을 다시 복원하면 옛 스키마로 덮어쓸 수 있으므로 구분한다. 테이블/컬럼 없는 오래된 데이터 백업은 최신 컬럼에 맞춰 변환한다.
5. SQL/COPY로 ID를 직접 넣었다면 시퀀스 값도 복원한다. `pg_dump --data-only` 백업은 시퀀스 상태를 포함한다.
6. 파일 경로를 담은 데이터가 있으면 업로드 원본 파일도 대상 PC로 복사하고 `storage_path`를 실제 경로에 맞춘다. 진행 중 작업은 원본 파일 없이 재개할 수 없다.
7. 계정·소유자 ID와 민원·답변·분류 건수를 확인하고 서버/워커를 다시 실행한다.

Docker 컨테이너를 쓰는 경우 프로젝트 루트의 PowerShell에서 다음과 같이 실행할 수 있다. **아래 초기화 명령은 기존 데이터를 지운다.**

```powershell
# 먼저 백업: 호스트의 파일명을 정해 보관한다.
docker exec complaintai-db pg_dump -U complaintai -d complaintai -Fc -f /tmp/complaintai-before-reset.dump
docker cp complaintai-db:/tmp/complaintai-before-reset.dump ./complaintai-before-reset.dump

# 최신 init.sql은 기존 컨테이너의 바인드 마운트로도 읽을 수 있다.
docker exec complaintai-db psql -U complaintai -d complaintai -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/01-init.sql
```

동기화 원본 측의 데이터 전용 백업 예시:

```powershell
docker exec complaintai-db pg_dump -U complaintai -d complaintai --data-only -Fc -f /tmp/complaintai-data.dump
docker cp complaintai-db:/tmp/complaintai-data.dump ./complaintai-data.dump
```

대상 PC에서 초기화 후 복원:

```powershell
docker cp ./complaintai-data.dump complaintai-db:/tmp/complaintai-data.dump
docker exec complaintai-db pg_restore -U complaintai -d complaintai --data-only --exit-on-error --single-transaction /tmp/complaintai-data.dump
```

외래키를 만족하려면 계정 → 민원/작업 → 답변/실패 행 순서로 복원되어야 한다. 재민원 자기참조는 `pg_dump`가 경고할 수 있으므로 먼저 별도 테스트 DB에서 복원해 확인한다. 임의로 외래키를 끄지 않는다.

## 데이터 없이 새 개발 환경 구성

빈 Docker 볼륨에서는 Compose가 `01-init.sql` → `02-seed-demo-accounts.sql` 순서로 실행한다. 이미 있는 볼륨에서는 자동으로 재실행되지 않는다.

수동 초기화 후 테스트 계정이 필요하면 `seed_demo_accounts.sql`을 별도로 실행한다. 데이터 동기화 예정이라면 이 파일을 실행하지 않는다.

## 검증

`complaintai-react-fastapi`에서 다음 테스트를 실행하면 임의 이름의 별도 테스트 DB에서 신규 생성, 구형 테이블 교체, 반복 실행, 실제 DB 컬럼·벡터 차원 비교, 외부 의존성에 대한 롤백을 검사한다. 현재 앱 DB는 초기화하지 않는다. 테스트용 DB 생성 권한(CREATEDB)이 필요하다.

```powershell
python -m unittest discover -s tests -p test_init_schema.py -v
```
