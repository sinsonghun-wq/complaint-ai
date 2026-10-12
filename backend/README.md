# 민원 벡터 다시 만들기 (`scripts.reembed_complaints`)

민원 검색(챗봇 조회, `/api/analyze` 유사 민원)은 `complaints.embedding` 컬럼에 저장된 벡터로 동작합니다.
이 스크립트는 **이미 저장된 민원의 벡터를 현재 설정 기준으로 다시 만듭니다.**

> 스크립트 위치는 `complaintai-react-fastapi/scripts/reembed_complaints.py` 입니다.
> 이 폴더(`backend/`)가 아니라 **`complaintai-react-fastapi` 폴더에서 실행**해야 합니다.

```bash
cd complaintai-react-fastapi
python -m scripts.reembed_complaints                 # 삭제·취소되지 않은 민원 전부
python -m scripts.reembed_complaints --only-missing  # 벡터가 비어 있는 민원만
```

---

## 1. 왜 필요한가

벡터 검색은 **저장된 벡터와 검색어 벡터가 같은 방식으로 만들어졌을 때만** 의미가 있습니다.
둘 중 하나라도 다른 모델·다른 입력 형식으로 만들어지면 유사도 점수가 엉뚱하게 나와, 관련 없는 민원이 검색되거나
찾던 민원이 빠집니다. 오류는 나지 않기 때문에 **겉으로는 정상처럼 보여서 알아채기 어렵습니다.**

새로 접수·수정되는 민원은 그때그때 현재 설정으로 임베딩되지만, **이미 저장된 민원은 자동으로 다시 만들어지지 않습니다.**
그래서 임베딩 방식이 바뀌면 예전 벡터와 새 벡터가 한 컬럼에 섞이게 되고, 이 스크립트로 한 번에 맞춰 줘야 합니다.

| 항목 | 지금 기준 |
|---|---|
| 임베딩 입력 | 민원 본문(`content`)만, 개인정보를 가린 뒤 **형식 없이 그대로** |
| 임베딩 함수 | `app/ai.py` 의 `embeddingForSelect()` — 저장과 검색(챗봇·`/api/analyze`)이 같은 함수를 씀 |
| 모델 | `.env` 의 `EMBEDDING_PROVIDER` 에 따라 `OLLAMA_EMBEDDING_MODEL` 또는 `EMBEDDING_MODEL` |
| 차원 | 1536 (모델 출력의 앞 1536개를 잘라 L2 정규화) |

---

## 2. 실행해야 하는 경우

### 반드시 실행

| 상황 | 이유 |
|---|---|
| **이번 변경(임베딩 입력을 본문만으로 통일)을 처음 배포할 때** | 기존 벡터는 `민원 제목/원문/요약/카테고리/핵심어` 를 합친 구조화 문서로 만들어져 있어, 본문만으로 만든 검색어 벡터와 비교가 맞지 않음 |
| `.env` 의 **`OLLAMA_EMBEDDING_MODEL` 또는 `EMBEDDING_MODEL` 을 바꿨을 때** | 모델이 다르면 벡터 공간 자체가 달라 서로 비교할 수 없음 |
| `.env` 의 **`EMBEDDING_PROVIDER` 를 바꿨을 때** (`ollama` ↔ OpenAI 호환 API) | 이름이 비슷한 모델이어도 배포 방식(양자화 등)이 달라 벡터가 달라짐 |
| 임베딩 입력을 만드는 코드가 바뀌었을 때 (`embeddingForSelect()` 에 넘기는 텍스트, `redact()` 규칙 등) | 같은 민원이라도 입력 텍스트가 달라지면 벡터가 달라짐 |
| 이번 변경 **이전에 만든 DB 백업을 복원했을 때** | 복원된 데이터에 예전 형식 벡터가 들어 있음 |

### `--only-missing` 으로 실행

| 상황 | 이유 |
|---|---|
| 스크립트 실행 결과에 **실패 건수가 있을 때** | 실패한 민원은 `embedding` 이 비어 있으므로 그것만 다시 처리 |
| 임베딩 서버(Ollama)가 꺼져 있던 동안 민원이 접수됐을 때 | 접수 시 임베딩에 실패한 민원은 벡터 없이 저장되고, 다시 만드는 코드가 따로 없어 **검색에서 계속 빠짐** |

### 실행하지 않아도 되는 경우

- **`LLM_MODEL` 만 바꿨을 때** — 임베딩 입력이 본문뿐이라 LLM 분석 결과(요약·카테고리·핵심어)가 벡터에 들어가지 않습니다.
- 카테고리·요약 프롬프트를 바꿨을 때 — 위와 같은 이유입니다.
- `LLM_TIMEOUT_MS`, `OLLAMA_URL` 등 연결 설정만 바꿨을 때 (모델이 그대로라면).

---

## 3. 실행 전 준비

1. `complaintai-react-fastapi/.env` 에 `DATABASE_URL` 이 설정되어 있어야 합니다.
2. 임베딩 서버가 떠 있고 모델이 받아져 있어야 합니다.
   ```bash
   ollama pull qwen3-embedding:4b   # .env 의 OLLAMA_EMBEDDING_MODEL 값
   ```
3. 새 모델로 바꾸는 경우, 그 모델의 출력 차원이 **1536 이상**인지 확인하세요.
   DB 컬럼이 `vector(1536)` 으로 고정되어 있어 그보다 작은 모델은 쓸 수 없습니다. (벡터가 저장되지 않음)
4. 민원 수가 많으면 시간이 걸립니다. 한 건씩 순서대로 임베딩하므로 대략 `민원 수 × 한 건 임베딩 시간` 입니다.
   이용자가 적은 시간에 실행하는 것을 권장합니다.

---

## 4. 스크립트가 하는 일

1. 대상 민원 조회 — 삭제되지 않았고(`deleted_at IS NULL`), 취소되지 않았고, 본문이 비어 있지 않은 민원.
   `--only-missing` 이면 그중 `embedding IS NULL` 인 민원만.
2. 각 민원의 본문에서 개인정보를 가리고(`redact()`) `embeddingForSelect()` 로 임베딩. 실패하면 1초 쉬고 한 번 더 시도.
3. 결과 저장
   - 성공: `embedding` 과 `embedding_model`(실제 사용한 모델 이름)을 갱신
   - 실패: `embedding` 과 `embedding_model` 을 **NULL 로 비움** — 예전 형식 벡터가 섞여 남지 않게 하기 위함입니다.
     비워진 민원은 검색에서 빠지므로 `--only-missing` 으로 다시 실행하세요.
4. 100건마다 진행 상황, 마지막에 성공·실패 건수를 출력.

**안전장치**

- 스크립트가 도는 동안 사용자가 민원을 수정하면, 그 민원은 `analysis_revision` 이 바뀌어 스크립트가 덮어쓰지 않습니다.
  (수정된 민원은 접수 흐름에서 새로 임베딩됨)
- 여러 번 실행해도 결과는 같습니다. 중간에 멈췄다면 처음부터 다시 실행하거나 `--only-missing` 을 쓰면 됩니다.

---

## 5. 실행 후 확인

```sql
-- 모델별 벡터 개수 (예전 모델 이름이 남아 있으면 덜 끝난 것)
SELECT embedding_model, COUNT(*)
FROM complaints
WHERE deleted_at IS NULL AND complaint_status <> '취소'
GROUP BY embedding_model;

-- 벡터가 비어 있는 민원 수 (0 이 아니면 --only-missing 으로 다시 실행)
SELECT COUNT(*)
FROM complaints
WHERE deleted_at IS NULL AND complaint_status <> '취소'
  AND COALESCE(content, '') <> '' AND embedding IS NULL;
```

`embedding_model` 은 `.env` 의 현재 임베딩 모델 이름 하나만 나와야 정상입니다.

> 참고: 이번 변경 이전에 저장된 벡터는 실제 사용 모델과 관계없이 `embedding_model` 이 항상 `Qwen/Qwen3-Embedding-4B` 로 기록되어 있었습니다.
> 그래서 `.env` 의 모델 이름이 `qwen3-embedding:4b` 라면, 스크립트를 실행하기 전의 예전 벡터와 실행 후의 새 벡터를 이 컬럼으로 구분할 수 있습니다.
