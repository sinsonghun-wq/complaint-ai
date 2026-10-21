# 접속 장애 복구: 2026-10-20

## 확인된 원인

14:05 KST 확인 당시 `localhost:5173/`은 HTTP 200이었지만,
`localhost:5173/health`는 HTTP 502였고 `127.0.0.1:8000/health`는 연결이 거절됐다.
포트 8000의 백엔드 프로세스와 포트 8080의 llama-server 프로세스가 없었다.
화면 서버와 PostgreSQL 17(5433)은 실행 중이었다.

요청 흐름은 `브라우저 → Vite(5173) → FastAPI(8000) → PostgreSQL(5433)`이다.
화면은 열리지만 로그인 API를 전달할 대상이 없어 Vite가 502를 반환했다.
기존 프론트 코드가 본문이 없는 오류 응답을 `{}`로 해석해
구체적인 연결 상태 대신 `요청을 처리하지 못했습니다.`를 표시했다.

기존 백엔드 로그는 정상 시작/조회 기록에서 끝났고 종료 원인을 나타내는 traceback은
없었다. 조회한 Windows 응용 프로그램 오류 기록에서도 관련 충돌을 확인하지 못했다.
따라서 종료의 직접적인 계기가 터미널 종료, 실행 세션 정리, 외부 종료 중 무엇인지는
단정하지 않는다. 기존에는 서버를 독립적인 서비스 관리 작업에 맡기지 않았다.

## 변경 내용

- `scripts/start_local.ps1`: AI 서버 → 백엔드 → 화면 서버 순서로 상태를 검사하고
  꺼진 구성 요소를 시작한다. 이미 정상인 서버는 다시 실행하지 않는다.
- `scripts/run_local_service.ps1`: 현재 사용자 권한으로 서버를 실행하고 실행 PID,
  시작/종료 시각, 종료 코드와 표준 출력/오류 파일을 `logs/`에 남긴다.
- `start-doc-main.cmd`: 프로젝트 폴더에서 더블클릭해 위 실행기를 호출한다.
- 현재 AI 서버와 백엔드는 Windows 작업 스케줄러의
  `ComplaintAI-doc-main-llama`, `ComplaintAI-doc-main-backend` 작업으로 실행한다.
  실행기 터미널이 끝나도 유지되며 실행 시간 제한은 없다.
  비정상 종료 시 1분 간격, 최대 3회 재시도하도록 설정했다.
- 이 작업에는 시간/로그온 트리거를 등록하지 않았다. PC 재부팅 또는 로그아웃 후에는
  Docker/PostgreSQL이 준비된 상태에서 `start-doc-main.cmd`를 실행한다.
- 이미 실행 중인 화면 서버는 재사용한다. 화면 서버가 꺼져 있으면 동일한 실행기가
  `ComplaintAI-doc-main-frontend` 작업을 만들고 실행한다.
- `dcm-front/src/App.jsx`: 빈 응답·비 JSON 응답의 502/503, 504, 네트워크 연결 오류를
  구분해 안내한다. 서버가 제공한 실제 인증/서비스 오류 메시지는 유지한다.

DB 접속 정보, 계정 비밀번호, 임베딩 모델, RAG, 모델/LoRA는 변경하지 않았다.
llama-server 컨텍스트는 기존 32768을 유지했다. GitHub에 업로드하지 않았다.

## 검증

- 직접 주소(8000)와 화면 프록시 주소(5173)에서 `/health`, `/health/database`,
  관리자 로그인, 분류 건수와 삭제 보관함 건수가 모두 HTTP 200.
- 실제 브라우저에 입력되어 있던 관리자 계정으로 로그인 성공.
- llama-server의 `/props`에서 32768 확인, LoRA를 사용한 짧은 생성 요청 성공.
- 실행기가 종료된 뒤에도 Windows 작업 및 서버 PID가 유지됨을 확인.
- 실행기 재실행 시 기존 서버를 재사용하고 중복 실행하지 않는 것을 확인.
- 오류 안내/관리자/현행 AI 관련 프론트 테스트 25개 및 `npm run build` 통과.

진단 로그: `logs/test-connection-fix.log`, `logs/local-service-events.jsonl`,
`logs/backend-*.stderr.log`, `logs/llama-*.stderr.log`.

## 수동 관리

재실행:

```powershell
cd C:\Users\1Class_07\doc-main
.\start-doc-main.cmd
```

관리 작업을 위해 서버를 중지할 때는 해당 작업을 중지한다.

```powershell
Stop-ScheduledTask -TaskName ComplaintAI-doc-main-backend
Stop-ScheduledTask -TaskName ComplaintAI-doc-main-llama
# 실행기가 만든 화면 서버가 있는 경우만:
# Stop-ScheduledTask -TaskName ComplaintAI-doc-main-frontend
```

수동 실행한 Vite 터미널은 기존처럼 Ctrl+C로 종료한다.
DB나 다른 프로젝트의 서버는 위 명령의 대상이 아니다.
