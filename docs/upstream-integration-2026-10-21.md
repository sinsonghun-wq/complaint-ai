# 2026-10-21 원격 main 변경 검토 및 통합

## 기준

- 가져온 저장소 및 최종 반영 대상: `usriddle/doc-classifier-ml`, `main`.
- 기존 로컬 기준: `2f0fb542`.
- pull 시점의 원격 main: `4fc3e80b`.
- 기존 로컬 기능: `complaint-ai`에 게시했던 `0e02a336`과 같은 코드.
- `C:\Users\1Class_07\doc-main`은 읽기만 하고 별도 작업 폴더에서 pull·충돌 해결·검증을 수행했다.

## 원격에서 달라진 6개 파일

| 파일 | 변경 내용 | 통합 처리 |
| --- | --- | --- |
| `README.md` | 장식 문구 추가 | 반영. 새 DB 기준 관리자 부서 안내도 갱신 |
| `database/init.sql` | AI 답변 테이블 삭제 순서 및 초기 부서명 변경 | 반영 후 초기화 오류 수정 |
| `dcm-back/app/prompts.py` | 안내 문구 단축, 문의 모드 답변 및 사례 표현 지침 변경 | 최신 원격 내용 반영 |
| `dcm-back/app/dependencies.py` | 관리자 부서 ID를 문자열 목록으로 반환 | 기존 DB의 부서 ID·이름을 이용하는 현재 구현 유지 |
| `dcm-back/app/routers/complaints.py` | 부서 ID를 이름으로 바꿔 관리 가능 여부 판정 | 현재 `can_manage`의 부서 ID 판정 유지 |
| `dcm-back/app/utils/util.py` | 중복 함수 제거, 부서 ID를 이름으로 변환 | 중복이 제거된 현재 구현과 잠금 후 권한 재검사 유지 |

권한 관련 파일 3개에서 실제 충돌이 발생했다. 번호를 이름으로 고정 변환하면 DB마다 다른 부서 배치와 일반 사용자의 소유자 ID를 잘못 처리할 수 있어, 이미 검증한 부서 ID·소유자 ID 기반 구현으로 해결했다.

## 초기화 SQL에서 수정한 오류

1. 새 빈 DB에서 `type "vector" does not exist`를 재현했다. `CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public`을 초기화 트랜잭션에 추가했다.
2. 새 부서 이름과 관리자 계정 생성 쿼리의 옛 이름이 달라 5명의 `department_id`가 NULL이 되는 문제를 수정했다. 신규 생성 계정의 표시 이름도 해당 부서에 맞췄다.
3. 기존 로그인 ID와 비밀번호 해시는 유지한다. `admin-fire`는 새 초기 부서 구성의 6번 부서인 노동·기업에 연결되며 표시 이름은 노동·기업 관리자다. 이는 새 DB 초기화 기준이다.

실제 서비스 DB에는 초기화 SQL이나 부서·계정 변경을 실행하지 않았다. 기존 DB의 부서 번호·이름·계정은 그대로 사용한다.

## 기존 DB에서 코드만 업데이트할 때

`database/init.sql`은 기존 데이터를 삭제하는 재생성용 SQL이므로 pull을 적용하기 위해 실행하지 않는다. 기존 DB에서는 앱 시작 시 필요한 삭제 데이터·중복 판정·RAG 작업 스키마를 보완한다.

완전히 새 개발용 DB라면 PostgreSQL 17 서버에 pgvector가 설치된 상태에서 `database/init.sql`을 실행한다. 이 파일은 현재 개발용 일반 계정 2개와 관리자 7개도 생성하므로 `seed_demo_accounts.sql`을 추가 실행할 필요가 없다. 실제 연결 값은 각 PC의 `.env`에 설정한다.

## 유지되는 기능

- 현재 `/api/chat`과 `department_documents`를 이용하는 RAG 검색 구조.
- 1,024차원 임베딩과 8,192토큰 임베딩 옵션. 문서 분할 기능은 추가하지 않는다.
- CSV 필드당 최대 200만 자, 초과 행과 실패 행을 건너뛰고 진행하는 처리.
- RAG CSV 원문 보존, 정확히 같은 내용의 중복 제외, 100행 단위 처리·저장과 취소.
- 부서 전체 민원 삭제 시 기본 사유를 적용하는 보관함 이동 및 삭제된 데이터 UI.
- 부서별 권한, 민원 접수·상태·답변 연동, 파일 파서 개선.

## 검증

`dcm-back/tests/test_upstream_init_integration.py`는 실제 `database/init.sql`을 별도 임시 DB에 실행한다. 초기 관리자 7명의 로그인·부서 접근, DB의 실제 부서 번호에 따른 조회·변경 권한, CSV에서 RAG 원문·임베딩 저장 및 중복 제외를 검사한다. 임시 DB는 테스트 후 삭제한다.

통합 후 검증 결과:

- 백엔드 전체: 102개 중 101개 통과, 1개 건너뜀, 실패·오류 0개.
- 프런트엔드 `test:admin`: 화면 테스트 79개와 진행률 테스트 4개 통과.
- 프런트엔드 프로덕션 빌드 성공.
- 이번 변경의 공백 검사와 병합 충돌 표시 검사 통과.

프런트엔드의 예전 `AIRecommendations.test.jsx`와 `ChatToolsIntegration.test.jsx`에는 `/api/ai-recommendations`를 전제로 한 기존 실패 28개가 남아 있다. 현재 `/api/chat` 검증은 `CurrentAIChat.test.jsx`가 수행하며, 이번 통합에서 AI 구현을 이전 API로 바꾸지 않았다. 전체 프런트엔드 테스트가 모두 통과했다는 뜻은 아니다.

`.env`, 업로드 원본, 실제 DB 데이터, 모델 바이너리 및 의존성 설치 폴더는 Git에 포함하지 않는다.
