| 계정 이름 | 로그인 ID | 비밀번호 |
| --- | --- | --- |
| 테스트 일반 사용자 A | `user-a` | `UserA!2026` |
| 테스트 일반 사용자 B | `user-b` | `UserB!2026` |

### 관리자

아래는 현재 `database/init.sql`로 새 DB를 생성했을 때의 개발용 계정입니다.
`admin-fire`는 기존 로그인 ID를 유지하며 새 초기 부서 구성에서는 노동·기업을 담당합니다.
기존 DB의 부서 번호는 유지하며, 부서 미지정 초기 관리자 계정은 해당 부서로 연결을 복구합니다.
DB에서 부서명을 변경하면 같은 부서 ID에 연결된 관리자 이름도 자동으로 갱신됩니다.
자세한 동작과 적용 결과는 [관리자 부서 동기화](docs/admin-department-sync-2026-10-26.md)를 참고하세요.
초기화·업데이트 방법과 이번 통합 내용은 [변경 검토](docs/upstream-integration-2026-10-21.md)를 참고하세요.

| 계정 이름 | ID | 비밀번호 | 담당 부서 |
| --- | --- | --- | --- |
| 문화·행정·안전 관리자 | `admin-administration-safety` | `AdminService!2026` | 문화·행정·안전 |
| 교통·국토 관리자 | `admin-land-transport` | `AdminLand!2026` | 교통·국토 |
| 주택·건축 관리자 | `admin-housing` | `AdminHousing!2026` | 주택·건축 |
| 환경·위생 관리자 | `admin-environment` | `AdminEnvironment!2026` | 환경·위생 |
| 보건·복지 관리자 | `admin-welfare` | `AdminWelfare!2026` | 보건·복지 |
| 노동·기업 관리자 | `admin-fire` | `AdminFire!2026` | 노동·기업 |
| 기타 관리자 | `admin-other` | `AdminOther!2026` | 기타 |
