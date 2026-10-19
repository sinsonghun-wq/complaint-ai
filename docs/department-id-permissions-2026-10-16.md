# 부서 ID 통일 및 관리자 권한 수정 (2026-10-16)

## 작업 범위

별도 폴더 `C:\Users\1Class_07\complaint AI-main`의 수정 브랜치 `codex/department-id-permissions`에서 작업했다.
기준 main 커밋은 `1eb09ab95c2fa7b2b56b78175bfef336a297feb8`이다.
GitHub main·기존 complaintAI 폴더·기존 5433 DB는 변경하지 않았다. 원격 push는 수행하지 않았다.

## 고정 부서 ID

| ID | 코드 | 화면 이름 |
| --- | --- | --- |
| 1 | labor_business | 노동·기업 |
| 2 | traffic_land | 교통·국토 |
| 3 | housing | 주택·건축 |
| 4 | environment | 환경·위생 |
| 5 | culture_admin_safety | 문화·행정·안전 |
| 6 | welfare | 보건·복지 |
| 7 | etc | 기타 |

ID는 배열 순서나 이름으로 매번 새로 생성하지 않는다. `departments` 테이블이 표시 이름을 제공하며, 공통 JSON과 SQL의 ID·코드·이름 일치를 테스트한다.
기존 계정 ID·소유자 ID·비밀번호는 유지한다. 일반 사용자의 department_id는 NULL이다.

## 인증·권한·화면 계약

- 로그인과 /api/auth/context: `department_id`와 DB JOIN으로 읽은 `department_name` 반환.
- 토큰의 부서·역할 값만으로 권한을 주지 않는다. 모든 인증 요청에서 계정을 DB로 다시 조회한다. 계정 부서·역할이 바뀌면 이전 토큰에도 즉시 적용된다.
- 권한 검사: 관리자 계정의 department_id와 민원의 department_id가 일치할 때만 변경 허용.
- 변경 트랜잭션은 민원 행을 잠그고 현재 부서를 다시 검사한다. 사전 조회 직후 부서가 전달되더라도 이전 부서가 변경할 수 없다.
- 모든 관리자는 전체 부서의 목록·원문·상태를 읽을 수 있다. can_manage는 민원별로 계산한다.
- 담당 부서만 접수 시작, 답변 초안 작성/수정/삭제, 답변 전송, 부서 전달, 취소 및 담당 부서 일괄 취소 가능.
- 다른 부서 변경 요청은 HTTP 403. 일반 사용자는 관리자 업무를 사용할 수 없다.
- 원문 수정은 기존 민원인 소유권·상태 규칙을 유지한다. 관리자에게 민원인의 원문을 고칠 권한을 새로 부여하지 않았다.
- 완료·취소 상태의 답변/수정 제한은 유지한다. 임시 저장 후 고친 최종 답변은 전송 요청의 content를 원자적으로 반영한다.
- React는 부서 ID로 선택·강조·권한 판단·전달한다. 이름은 인증 응답과 /api/departments의 DB 목록을 사용한다. 기존 저장 세션도 /api/auth/context로 갱신한다.
- 전체 부서 데이터를 지우는 테스트 예외 기능은 화면에서 숨기고 API에서 403으로 비활성화했다. 부서 권한을 우회하는 예외를 남기지 않았다.

## DB 호환 구조

app_users, complaints, complaint_responses, department_documents에 department_id 외래키를 적용했다.
기존 department와 category 텍스트는 기존 분류·LLM·워커·검색 호환용으로 유지하되 DB 트리거가 ID와 동기화한다. 이름/ID 충돌이나 알 수 없는 부서 값은 거부한다. 권한에는 텍스트를 사용하지 않는다.
분류 대기 민원은 부서 ID가 NULL이며, 분류 결과를 category에 저장하면 DB가 해당 ID를 자동 연결한다. 부서 전달은 ID로 저장하고 표시용 이름도 함께 갱신된다.

새 init.sql은 같은 ID 스키마와 제약·트리거를 포함한다. 초기 계정은 seed_demo_accounts.sql에서 ID로 생성한다.
init.sql 내부에 중복으로 들어 있던 테스트 계정 삽입은 제거해 seed 선택 실행과 일치시켰다.
기존 DB 이전에는 파괴적인 init.sql을 사용하지 않는다.

## 기존 DB를 데이터 보존하여 이전

서버와 CSV 워커를 중단하고, 백업 후 대상 DB를 확인한다. 실행 위치는 complaintai-react-fastapi다.

```powershell
# .env가 가리키는 DB와 기대하는 DB 이름이 반드시 일치해야 한다.
.\.venv\Scripts\python.exe scripts\migrate_department_ids.py --expected-database YOUR_DATABASE_NAME
.\.venv\Scripts\python.exe scripts\migrate_department_ids.py --expected-database YOUR_DATABASE_NAME --apply
```

마이그레이션: `database/migrations/20261016_department_ids.sql`.
계정·민원·답변을 삭제하거나 계정 비밀번호를 재설정하지 않는다. 재실행 가능하다.
기존 부서 정보가 충돌하면 트랜잭션을 중단하므로, 임의로 다른 부서나 기타로 바꾸지 말고 데이터 원인을 확인한다.
새 DB는 최신 init.sql → seed_demo_accounts.sql → 필요한 RAG migration 순서로 초기화한다.
기존 9부서 통합용 스크립트는 과거용이다. 현재 7부서 DB의 ID 전환에는 위 전용 스크립트를 사용한다.

## 로컬 검증 환경

- UI: http://127.0.0.1:5174/
- API: http://127.0.0.1:8000/
- DB: 127.0.0.1:5440 / complaintai_main_preview
- 컨테이너 complaintai-main-db, 볼륨 complaintai-main-pgdata
- 실행 설정: tmp/main-runtime/compose.yaml 및 complaintai-react-fastapi/.env (Git 제외)

기존 5433 DB는 이전·초기화·데이터 삭제 대상으로 사용하지 않았다.

## 검증 명령

```powershell
# 백엔드: 임의 이름의 독립 DB를 만들고 해당 테스트 DB만 제거한다.
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_department_permissions.py -v
.\.venv\Scripts\python.exe -m unittest tests.test_init_schema.InitSchemaTests.test_all_live_columns_and_vector_dimensions_are_present tests.test_init_schema.InitSchemaTests.test_existing_outdated_table_is_replaced_and_data_cleared tests.test_init_schema.InitSchemaTests.test_external_dependency_blocks_reset_and_rolls_back -v

# frontend 디렉터리
node node_modules/vitest/vitest.mjs run tests/DepartmentPermissions.test.jsx tests/ComplaintWorkspace.test.jsx tests/CitizenComplaintTools.test.jsx
node --test tests/importProgress.test.mjs
npm run build
```

권한 테스트는 49개 관리자/부서 조합의 조회·can_manage와 42개 타 부서 조합의 변경 차단을 포함한다.
모든 관리자 계정의 자기 부서 초안 작성·수정·최종 전송, 부서 전달 즉시 권한 변경, 오래된 토큰의 역할/부서 갱신, DB 이전의 재실행·기존 데이터 보존을 검사한다.

권한·구형 데이터 보존 테스트 11개와 초기화 안전성 검사 3개가 통과했다. 관련 React 회귀 테스트는 41개, 진행률 테스트 4개가 통과했고 React 빌드도 성공했다.
전체 프론트엔드 검사에서는 AIRecommendations/ChatToolsIntegration의 기존 테스트 28개가 실패했다.
수정하지 않은 기준 main(1eb09ab9)에서도 같은 28개가 실패함을 별도로 확인했다. 이 권한 작업에서 AI 채팅 구현이나 해당 테스트를 바꾸지는 않았다.
따라서 이번 권한 수정 완료를 프로젝트 전체의 배포 준비 완료로 해석하면 안 된다.
