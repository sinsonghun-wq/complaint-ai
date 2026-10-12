# 라벨 규칙 (학습 데이터 정답 기준)

Gemma 4 E4B 파인튜닝용 정답 데이터를 만들 때 따르는 규칙입니다.
**정답이 흔들리면 모델도 흔들립니다.** 애매한 문장은 이 문서에 규칙을 추가한 뒤 라벨링하세요.

| 과제 | 대상 | 데이터 파일 |
|---|---|---|
| 1. 챗봇 툴 선택 + 인자 추출 | `dcm-back/app/agent.py` 의 **모드 0 (접수·조회)** | `chat_records.jsonl` |
| 2·3. 민원 분석 (카테고리·요약·핵심어) | `dcm-back/app/ai.py` 의 `analyze()` | `analysis_records.jsonl` |

**학습하지 않는 것**
- 챗봇 **모드 1 (문의)**: 업무 지식 답변을 학습하면 낡거나 지어낸 정보가 나오고, 거절·안내 예시만 넣으면
  진짜 문의까지 거절하는 쪽으로 치우칩니다. 나중에 FAQ 검색(RAG)을 붙일 때 다시 다룹니다.
  (모드 0 학습 후 모드 1 답변이 망가지지 않았는지는 평가에서 몇 문장으로 확인합니다)
- CSV 헤더 매핑(`infer_csv_mapping`)

---

## 공통 규칙

- **원문에 없는 내용을 만들지 않습니다.** 날짜·기관·장소·법령·해결책을 추측해 넣지 않습니다.
- 개인정보(전화번호·이메일·주민등록번호)는 앱이 LLM 에 넘기기 전에 `[개인정보 제외]` 로 가립니다.
  정답 데이터의 원문도 같은 형태여야 합니다.
- 카테고리는 `dcm-back/app/categories.py` 의 7종 정의(괄호 안 경계 규칙 포함)를 따릅니다.
  backend `training/records.py` 머리말의 카테고리 경계 규칙과 같은 기준입니다.
- 같은 원문을 말만 바꿔 늘린 변형은 같은 `group` 으로 묶습니다.
  같은 group 은 반드시 같은 쪽(학습/검증/평가)에 들어가 평가 점수가 부풀지 않습니다.

---

## 과제 1. 챗봇 모드 0 (접수·조회)

### 1-1. 툴 선택

| # | 사용자 발화 | 정답 | 출처 |
|---|---|---|---|
| A1 | 문제 상황을 설명 (접수) | `insertRequestT` | backend `records.jsonl` 의 `접수` |
| A2 | 이미 넣은 민원의 조회·수정·삭제 요청 | `selectComplaintT` | backend `조회`·`수정`·`삭제` |
| A3 | 접수 의사만 있고 내용이 없음 ("민원 넣고 싶어요") | `insertRequestT(title="", content="")` | **새로 작성** (100~200건) |
| A4 | 잡담·광고·무의미한 말 | 툴 없이 거절 문구 (A15) | backend `해당없음` |
| A5 | 문의 ("민원실 몇 층이에요?") | 툴 없이 문의 모드 안내 문구 (A16) | backend `문의` |

- 행정 업무의 처리 지연·절차 불편을 알리는 말(전입신고가 안 돼요 등)은 **접수**입니다.
- 이 창구에 이미 넣은 민원의 진행을 묻는 말("제가 넣은 OO 민원 처리됐나요")은 **조회**입니다.

### 1-2. 툴 인자

| # | 인자 | 규칙 | 예 |
|---|---|---|---|
| A6 | `insertRequestT.title` | 무엇이 어떤 상태인지 짧은 명사형. 위치·기간·요청 문구는 넣지 않음. backend 의 `arguments.content` 를 그대로 사용 | "공원 벤치 파손" |
| A7 | `insertRequestT.content` | **사용자 발화 원문 그대로** (인사말 포함, 가공하지 않음) | "한빛힐스에 사는 주민인데요, 학산어린이공원 벤치 나무가 썩어서 부서졌어요. 고쳐 주세요" |
| A8 | `selectComplaintT.content` | 찾을 민원의 **대상 명사만** 원문 그대로. backend 의 `arguments.keyword` 를 그대로 사용. 번호·동·호·층·장소 수식·상태·요청 말은 뺌. 대상을 알 수 없으면 `""` | "101동 엘리베이터 고장 민원 취소" → "엘리베이터" |
| A9 | `selectComplaintT.is_described` | `content` 가 비어 있지 않으면 `true`, 비어 있으면 `false` | "어제 넣은 거 취소해 줘" → `false` |

- 수정 요청의 새 내용·새 위치(backend 의 `content`·`location`)와 `period`·`category`·`complaint_id` 는
  complaint-ai 툴에 해당 인자가 없으므로 **쓰지 않습니다.**
- 민원 ID·번호를 묻거나 언급하지 않습니다. (시스템 프롬프트 규칙)

### 1-3. 툴 호출 뒤 안내 문장 (두 번째 응답)

툴 결과에 따라 아래 **정해진 문구**를 정답으로 씁니다. 학습 샘플을 만들 때 툴 결과(민원 목록 등)는 가짜로 만들어 넣습니다.

| # | 상황 | `reply_kind` | 정답 문구 |
|---|---|---|---|
| A10 | 접수, 내용 있음 | `insert_filled` | 말씀하신 내용으로 접수 양식을 채워 두었어요. 왼쪽 양식을 확인하시고 '접수' 버튼을 눌러 주세요. 고칠 부분이 있으면 직접 수정한 뒤 접수하시면 됩니다. |
| A11 | 접수, 내용 없음 | `insert_empty` | 왼쪽에 접수 양식을 열어 두었어요. 불편하신 내용을 적고 '접수' 버튼을 눌러 주세요. |
| A12 | 조회, 결과 있음 | `select_found` | 말씀하신 내용과 비슷한 민원을 왼쪽에 보여 드렸어요. 접수 대기·진행 중인 민원은 수정하거나 삭제할 수 있고, 처리가 끝났거나 취소된 민원은 조회만 할 수 있어요. |
| A13 | 조회, 결과 없음 | `select_none` | 말씀하신 내용과 비슷한 민원을 찾지 못했어요. 민원 내용을 조금 더 자세히 알려 주시겠어요? |
| A14 | 조회, 내용 묘사 없음 | `select_undescribed` | 어떤 민원인지 내용을 알려 주세요. (예: '집 앞 가로등 고장 민원') |
| A15 | 거절 (A4) | `reject` | 죄송하지만 민원 접수·조회와 관련된 내용만 도와드릴 수 있어요. |
| A16 | 문의 안내 (A5) | `inquiry_redirect` | 궁금하신 점은 '문의' 모드에서 물어봐 주세요. |

- `select_found` 와 `select_none` 은 같은 발화라도 툴 결과에 따라 달라지므로, 학습 샘플 생성기가
  `is_described=true` 레코드마다 결과 있음/없음 중 하나를 골라 만듭니다. 레코드에는 적지 않습니다.
- 문구를 바꾸면 다시 학습해야 합니다.

### 1-4. `chat_records.jsonl` 형식

한 줄에 발화 하나.

```json
{"id": "c-r01220", "group": "reg-203", "text": "한빛힐스에 사는 주민인데요, 학산어린이공원 벤치 나무가 썩어서 부서졌어요. 고쳐 주세요",
 "tool": "insertRequestT", "arguments": {"title": "공원 벤치 파손", "content": "한빛힐스에 사는 주민인데요, 학산어린이공원 벤치 나무가 썩어서 부서졌어요. 고쳐 주세요"},
 "source": "backend:r01220"}
{"id": "c-f01503", "group": "can-9-민원 건-r12", "text": "안녕하세요. 마지막으로 신고했던 민원 건 이제 처리 안 하셔도 돼요. 차주랑 연락이 됐어요",
 "tool": "selectComplaintT", "arguments": {"content": "", "is_described": false},
 "source": "backend:f01503"}
{"id": "c-r00006", "group": "oos-058", "text": "중고차 최저가 매입합니다",
 "tool": null, "arguments": null, "reply_kind": "reject", "source": "backend:r00006"}
```

| 필드 | 필수 | 내용 |
|---|---|---|
| `id` | ✅ | 고유 id. backend 에서 변환한 것은 `c-<backend id>` |
| `group` | | 변형 묶음. 없으면 id |
| `text` | ✅ | 사용자 발화 |
| `tool` | ✅ | `insertRequestT` / `selectComplaintT` / `null`(툴 없음) |
| `arguments` | 툴이 있을 때 ✅ | 위 1-2 규칙 |
| `reply_kind` | 툴이 없을 때 ✅ | `reject` / `inquiry_redirect` |
| `source` | | `backend:<id>` 또는 `new` |
| `split` | | `train` / `val` / `test` 로 강제할 때만 |

### 1-5. backend `records.jsonl` 변환 규칙

backend `records.jsonl`(5,058건)을 아래처럼 바꾸면 위 자동 검증을 모두 통과합니다. (미리 확인함)
접수 2,025 / 조회 묘사 있음 1,620 / 조회 묘사 없음 640 / 거절 428 / 문의 안내 345

| backend `intent` | `tool` | `arguments` / `reply_kind` |
|---|---|---|
| 접수 | `insertRequestT` | `title` = `arguments.content`, `content` = `text` |
| 조회·수정·삭제 | `selectComplaintT` | `content` = `arguments.keyword`, `is_described` = keyword 가 비어 있지 않음 |
| 해당없음 | `null` | `reply_kind` = `reject` |
| 문의 | `null` | `reply_kind` = `inquiry_redirect` |

`id`·`group`·`split` 은 그대로 옮깁니다. backend 의 `category` 는 과제 1 에서 쓰지 않습니다.

---

## 과제 2·3. 민원 분석 JSON

입력은 앱과 같이 **민원 제목 + 개인정보를 가린 민원 원문**, 출력은 아래 JSON 하나입니다.

| # | 필드 | 규칙 |
|---|---|---|
| C1 | (필드 구성) | `title`, `summary`, `key_points`, `urgency`, `needs_review`, `review_reason`, `category`, `keywords`. 모델이 스스로 적던 `confidence`, `reason` 은 **없앰** |
| C2 | `title` | 원문 표현을 쓴 짧은 명사형, 20자 이내 |
| C3 | `summary` | 2~3문장. 원문에 없는 날짜·기관·장소·해결책은 넣지 않음 |
| C4 | `key_points` | 1~3개. 민원 대상, 문제 상태, 요청 사항 위주 |
| C5 | `category` | `categories.py` 7종 중 하나. 정의와 괄호 안 경계 규칙을 따름 |
| C6 | `keywords` | **원문에 글자 그대로 있는 명사** 3~5개 |
| C7 | `urgency` | `high`: 인명·안전 위험(화재, 붕괴, 사고 우려 등) / `medium`: 생활 불편 / `low`: 단순 문의·제안·칭찬 |
| C8 | `needs_review`, `review_reason` | 내용이 너무 짧거나, 대상·위치를 알 수 없거나, 여러 민원이 섞여 있으면 `true` 와 이유 한 문장. 아니면 `false`, `null` |

### `analysis_records.jsonl` 형식

```json
{"id": "a00001", "group": "a00001", "title": "벤치 파손", "content": "학산어린이공원 벤치 나무가 썩어서 부서졌어요. 아이들이 앉다가 다칠 것 같아요. 고쳐 주세요",
 "output": {"title": "학산어린이공원 벤치 파손", "summary": "학산어린이공원의 벤치 나무가 썩어 부서졌다는 민원이다. 아이들이 앉다가 다칠 우려가 있어 수리를 요청했다.",
            "key_points": ["공원 벤치 파손", "이용자 부상 우려", "수리 요청"], "urgency": "medium",
            "needs_review": false, "review_reason": null, "category": "기타", "keywords": ["학산어린이공원", "벤치", "나무"]},
 "source": "teacher", "reviewed": true}
```

| 필드 | 필수 | 내용 |
|---|---|---|
| `id`, `group` | ✅ / | 과제 1 과 같음 |
| `title`, `content` | ✅ | 앱에 들어오는 입력과 같은 값 (`content` 는 개인정보를 가린 원문) |
| `output` | ✅ | 위 C1~C8 규칙의 정답 |
| `source` | | `teacher`(큰 모델이 만든 정답) / `human` / `backend:<id>` |
| `reviewed` | | 사람이 검수했으면 `true`. **학습에는 `true` 인 것만 씁니다** |

**규모**: 긴 민원 원문 1,000~2,000건. 큰 모델로 정답을 만들고 사람이 검수합니다.
backend `접수` 문장 중 긴 것(100자 이상, 약 189건)도 포함합니다. 카테고리별 건수가 비슷하도록 맞춥니다.

---

## 자동 검증 (학습 전에 `records.py` 가 확인)

| 대상 | 검사 |
|---|---|
| 공통 | `id` 중복 없음, 필수 필드 있음 |
| 과제 1 | `tool` 값이 허용 목록에 있음 / `selectComplaintT.content` 가 `text` 에 글자 그대로 있음 / `is_described` 가 `content` 유무와 일치 / `insertRequestT.content` 가 `text` 와 같음(또는 둘 다 `""`) / `reply_kind` 가 허용 목록에 있음 |
| 과제 2·3 | `output` 필드가 정확히 C1 의 8개 / `category` 가 7종 중 하나 / `urgency` 가 low·medium·high / `keywords` 3~5개이고 모두 `content` 에 글자 그대로 있음 / `title` 20자 이내 / `needs_review=false` 면 `review_reason=null` |
