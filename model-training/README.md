# model-training - Gemma 4 E4B 파인튜닝

complaint-ai 의 LLM(`dcm-back`)을 Gemma 4 E4B(`google/gemma-4-E4B-it` = Ollama `gemma4:e4b`)로 QLoRA 학습합니다.
학습 결과는 두 가지 방식으로 앱에 붙일 수 있습니다. (`dcm-back/.env` 의 `LLM_BACKEND`)

| 방식 | 내보내기 | 앱에서 |
|---|---|---|
| `ollama` | 노트북 11번 - 어댑터를 **병합**한 GGUF + Modelfile | 모든 작업이 병합 모델 하나를 씀 (CSV 열 매핑만 `CSV_MAPPING_LLM_MODEL` 로 다른 모델 지정 가능) |
| `llama_server` (**지금 쓰는 방식**) | 노트북 11-B - **어댑터 GGUF(f16)만** 받음. 베이스 GGUF(Q4_K_M)는 PC 에서 만들어 씀 (병합 안 함) | llama-server 에 베이스 하나만 올리고, 요청마다 어댑터를 켜고 끔. 기본은 분석·챗봇 = 켬, CSV 열 매핑 = 베이스 (`LLM_ADAPTER_TASKS`) |

| 학습 대상 | 앱의 위치 | 데이터 |
|---|---|---|
| 챗봇 모드 0 - 도구 선택·인자·안내 문구 | `dcm-back/app/agent.py` | `data/chat_records.jsonl` (2,601건 - 아래 "데이터 현황") |
| 민원 분석 - 카테고리·요약·핵심어 JSON | `dcm-back/app/ai.py` 의 `analyze()` | `data/analysis_records.jsonl` (862건) |

챗봇 모드 1(문의)은 학습하지 않습니다. 정답 기준은 `data/label_guide.md` 에 있습니다.

## 전체 흐름

```
[PC]    export_prompts.py  ─┐                       (프롬프트·도구 설명을 바꿨을 때만)
                            ▼
[Colab] colab_train.ipynb : 데이터 검증 → 4bit 로드 → 학습 전 평가 → 학습 → 학습 후 평가·비교 → 어댑터 저장 → 병합·GGUF·Modelfile
                            ▼
[PC]    ollama create complaint-gemma4 -f Modelfile → evaluate.py --ollama 로 평가 → .env 의 LLM_MODEL 변경
        또는
[Colab] 11-B : 어댑터 GGUF 만 (베이스 Q4_K_M 은 PC 에서 만든 것을 계속 씀)
[PC]    scripts/run_llama_server.ps1 → evaluate.py --llama-server 로 평가 → .env 의 LLM_BACKEND=llama_server
```

**처음에는 노트북 4번 셀에서 `SMOKE = True` 로 끝까지 돌려** 학습 → GGUF → Ollama 등록까지 경로가 되는지 먼저 확인하세요.

## 파일

| 파일 | 역할 |
|---|---|
| `colab_train.ipynb` | Colab 학습 노트북 (위에서부터 한 셀씩) |
| `common.py` | 경로·베이스 모델·최대 길이 등 공통 설정 |
| `records.py` | 학습 데이터 검증기 + 불러오기·분할 (`python model-training/records.py`) |
| `samples.py` | 레코드 → 학습 샘플 (Gemma 4 채팅 템플릿으로 조립, 정답 부분만 loss) |
| `trainer.py` | 모델 로드(4bit), LoRA, 정답 전용 loss 학습, 저장 |
| `plots.py` | 학습 중 train/val loss 실시간 그래프(콜백), 학습 전·후 지표 막대그래프 → `outputs/plots/` 에 PNG 저장 |
| `evaluate.py` | 과제별 지표. Colab(HF 모델)과 PC(Ollama / llama-server) 에서 같은 지표 |
| `export.py` | bf16 병합 → GGUF(llama.cpp) → Modelfile (`export_all`) / 베이스 GGUF + 어댑터 GGUF (`export_llama_server`) |
| `export_prompts.py` | 앱이 보내는 도구 목록 JSON·메시지 모양(Ollama 용, OpenAI 호환 용)을 `data/tools_mode0.json` 으로 저장 |
| `requirements-train.txt` | Colab 설치 목록 |
| `data/` | `label_guide.md`(라벨 규칙), `chat_records.jsonl`, `analysis_records.jsonl`, `tools_mode0.json` |

**과제별 val loss** - 검증 때마다 전체(`eval_all_loss`)와 과제별(`eval_chat_call_loss`, `eval_chat_reply_loss`, `eval_analysis_loss`) loss 를 따로 기록합니다.
best 체크포인트·early stopping 은 전체 기준이고, 노트북 8번 그래프 오른쪽 칸과 학습 뒤 표에서 분석 loss 만 따로 볼 수 있습니다. (`trainer.build_trainer(per_task_eval=False)` 면 예전처럼 전체만)

결과물 (`.gitignore` 대상): `outputs/`(체크포인트·어댑터·평가 결과), `modelfiles/`(GGUF·Modelfile, `modelfiles/llama-server/` 에 베이스·어댑터 GGUF)

## 학습과 실행을 같게 만드는 장치

학습 샘플의 입력이 실제 실행 때 모델이 받는 입력과 다르면 학습 효과가 떨어집니다.

| 부분 | 어디서 가져오나 |
|---|---|
| 시스템 프롬프트·안내 문구·분석 프롬프트 | `dcm-back/app/prompts.py` 를 그대로 import |
| 분석 프롬프트의 원문 처리 (공백 정리·개인정보 가림·1,500자 자르기) | `dcm-back/app/ai.py` 의 `prompt()` 를 그대로 호출 |
| 도구 목록 JSON, 도구 결과 메시지 모양 | `data/tools_mode0.json` (LangChain 이 실제로 만든 것) |
| 대화 → 토큰 | Gemma 4 공식 채팅 템플릿 (`apply_chat_template`) |
| Ollama 쪽 조립 | Modelfile 의 `RENDERER gemma4` / `PARSER gemma4` (기본 gemma4:e4b 와 같음) |
| llama-server 쪽 조립 | GGUF 안에 들어간 베이스의 Gemma 4 채팅 템플릿 (`--jinja`) - 학습 때 `apply_chat_template` 과 같은 템플릿. 사고 모드는 요청마다 끔 |

**`dcm-back/app/prompts.py` 나 `categories.py` 를 고치면 다시 학습해야 합니다.**
도구 설명이나 도구 함수 인자를 고쳤다면 학습 전에 PC 에서 `export_prompts.py` 도 다시 실행하세요.

### 분석 프롬프트 길이: long / short

분석 프롬프트는 두 가지가 있고 환경변수 `ANALYSIS_PROMPT_STYLE` 로 고릅니다. **이 프로젝트는 `short` 로 통일합니다.** (앱 `.env`, 노트북 4번 셀, `evaluate.py --prompt-style` 기본값 모두 `short`)

| 값 | 내용 | 프롬프트 버전 |
|---|---|---|
| `long` | 카테고리 정의·경계 규칙·작성 규칙(C1~C8)을 모두 실은 원래 프롬프트 (약 3,100자) | `complaintai-ko-v7` |
| `short` | 역할·카테고리 이름·출력 스키마만 실은 짧은 프롬프트 (약 350자). 규칙은 학습 데이터로 익힘 - **어댑터를 켠 분석에만** 쓰세요 | `complaintai-ko-v7-short` |

**학습과 실행의 값이 같아야 합니다.** 짧은 프롬프트로 학습한 어댑터를 긴 프롬프트로 부르면(또는 그 반대) 점수가 떨어집니다.

- 학습: 노트북 4번 셀의 `ANALYSIS_PROMPT_STYLE = 'short'` (기본값). `run_info.json` 에 `analysis_prompt_style`, `prompt_version` 이 남습니다.
- 평가: `python model-training\evaluate.py --llama-server http://127.0.0.1:8080` - `--prompt-style` 기본이 `short` (결과 파일 이름 끝에 `_short`). long 으로 학습한 예전 어댑터를 잴 때만 `--prompt-style long`
- 앱: `dcm-back/.env` 에 `ANALYSIS_PROMPT_STYLE=short`. 서버 시작 로그에 스타일이 찍히고, short 인데 분석에 어댑터가 꺼져 있으면 경고가 나옵니다.
- 베이스 모델(어댑터 끔)로 분석할 때는 `long` 을 쓰세요. 짧은 프롬프트에는 규칙이 없어 베이스 모델이 규칙을 모릅니다.
  그래서 노트북 6-1(학습 전 평가)과 `--lora-scale 0` 평가의 분석 점수는 short 에서 낮게 나오는 것이 정상입니다. (어댑터가 규칙을 얼마나 배웠는지 보는 기준선)
- `dcm-back/app/prompts.py` 는 환경변수가 없을 때만 `long` 을 씁니다. 앱은 `.env` 의 값을 쓰므로 `.env` 에 `ANALYSIS_PROMPT_STYLE=short` 가 있는지 확인하세요.

## Colab 준비

1. 저장소 폴더 `doc-classifier-ml-main` 을 **내 드라이브 맨 위**에 올립니다. (다른 위치면 노트북의 `REPO` 수정)
2. 드라이브에서 `model-training/colab_train.ipynb` 를 Colab 으로 엽니다.
3. 런타임 → 런타임 유형 변경 → **L4 이상** GPU.
4. 0번 셀부터 차례로 실행합니다. 2번(설치) 뒤에는 3번으로 런타임을 재시작하고 4번부터 이어 갑니다.

| GPU | 비고 |
|---|---|
| T4 (무료) | bfloat16 이 없어 float16 으로 돌아 loss 가 nan 이 될 수 있음 - 비권장 |
| **L4** | 권장. 4번 셀 기본값 `batch_size=2, grad_accum=4` (분석 프롬프트가 길어 메모리 여유를 둠) |
| A100 | 가장 빠름. `batch_size=4, grad_accum=2` 로 올려도 됨 |

메모리가 부족하면(`CUDA out of memory`) `batch_size=1, grad_accum=8` 로 바꾸세요. (곱이 8 로 같으면 학습 결과는 같음)

## PC 에서 Ollama 등록·평가

```powershell
# modelfiles/<RUN_NAME>/ 폴더(.gguf + Modelfile)를 내려받아 그 폴더에서
ollama create complaint-gemma4 -f Modelfile
# GGUF 를 bf16 으로 만들었다면:  ollama create complaint-gemma4 -f Modelfile --quantize q4_K_M

# 저장소 루트에서 (dcm-back 가상환경)
python model-training\evaluate.py --ollama complaint-gemma4:latest --limit 600
```

- 노트북 9번(HF 모델) 결과와 비슷하면 정상입니다. **Ollama 쪽 점수가 크게 낮으면** Ollama 가 프롬프트를 학습 때와 다르게
  조립하고 있다는 뜻이니 결과 파일(`outputs/eval_ollama_*.json`)의 `_틀린 예` 를 보고 알려 주세요.
- 앱에 적용: `dcm-back/.env` 의 `LLM_BACKEND=ollama`, `LLM_MODEL=complaint-gemma4:latest`

## PC 에서 llama-server 실행·평가 (베이스 + 어댑터)

준비: PC 에서 파워셸로 만든 `gemma-4-E4B-it-Q4_K_M.gguf`(베이스, `dcm-back/README.md` 6번)와 노트북 11-B 에서 받은 `complaint-lora-<RUN_NAME>-f16.gguf`(어댑터)를
`model-training/modelfiles/llama-server/` 에 둡니다. `llama-server.exe` 는 [llama.cpp 릴리스](https://github.com/ggml-org/llama.cpp/releases)의
Windows 빌드(NVIDIA 는 cuda)를 씁니다. Gemma 4 를 지원하는 최근 빌드여야 합니다.

```powershell
# 저장소 루트에서 - 어댑터는 올려만 두고(기본 꺼짐) 요청마다 켭니다
.\scripts\run_llama_server.ps1 -LlamaServer C:\llama.cpp\llama-server.exe -Adapter .\model-training\modelfiles\llama-server\complaint-lora-<RUN_NAME>-f16.gguf

curl http://127.0.0.1:8080/lora-adapters      # [{"id":0, ..., "scale":0.0}] 이면 정상

# 같은 서버에서 어댑터 켬(학습 모델) / 끔(베이스) 을 각각 평가
python model-training\evaluate.py --llama-server http://127.0.0.1:8080 --limit 600
python model-training\evaluate.py --llama-server http://127.0.0.1:8080 --lora-scale 0 --limit 600
```

- 어댑터 켬 점수가 노트북 9번(학습 후)과, 끔 점수가 6-1번(학습 전)과 비슷하면 정상입니다.
  **켬 점수가 크게 낮으면** llama-server 의 프롬프트 조립·도구 호출 해석이 학습 때와 다르다는 뜻이니
  `outputs/eval_llama_server_*.json` 의 `_틀린 예` 를 확인하세요. (특히 챗봇: 도구 호출이 글자로 나오는지)
- 앱에 적용: `dcm-back/.env`
  ```
  LLM_BACKEND=llama_server
  LLAMA_SERVER_URL=http://127.0.0.1:8080
  LLM_ADAPTER_TASKS=analysis,chat        # 어댑터를 켤 작업. csv_mapping 은 빠져 있어 베이스 모델로 답함
  LLAMA_SERVER_ADAPTER_NAME=complaint-lora-<RUN_NAME>   # DB 의 llm_model 에 남는 이름 (기록용)
  ```
  임베딩은 그대로 Ollama(`qwen3-embedding:4b`)를 씁니다. Ollama 도 계속 띄워 두세요.
- 새로 학습하면 어댑터 GGUF 만 바꿔 서버를 다시 띄우면 됩니다. (베이스 GGUF 는 그대로, 11-B 의 `MAKE_BASE_GGUF = False`)
- 서버를 띄울 때의 컨텍스트: 슬롯 2개 x 8,192 토큰 (`-Parallel`, `-CtxPerSlot`). `.env` 의 `LLM_NUM_CTX` 와 슬롯당 값을 같게 두세요.

## 평가 지표

| 과제 | 지표 |
|---|---|
| 챗봇 | 도구 선택 정확도, 접수 content 정확 일치, 조회 content 정확 일치, 도구 없는 답 고정 문구 일치, 도구 결과 뒤 안내 문구 일치 |
| 분석 | JSON 파싱 성공률(앱 기준 - ```json 코드블록 허용), JSON 그대로 파싱(코드블록 없음), 카테고리 정확도, 긴급도 정확도, 핵심어가 원문에 있는 비율, 요약 속 숫자가 원문·제목에 있는 비율 |
| 모드 1 | 점수 없음 - 학습 후에도 답변이 자연스러운지 눈으로 확인 |

- **요약 속 숫자가 원문·제목에 있는 비율**: 모델이 쓴 `summary` 의 아라비아 숫자(금액·날짜·횟수 등)가 입력 제목이나 (1,500자로 잘린) 원문에
  있는지 숫자 하나 단위로 셉니다. 지어낸 숫자를 잡는 지표입니다. `1,500`=`1500`, `03`=`3` 은 같은 숫자로 보고, 원문 `500` 이 요약의 `50` 을
  통과시키지는 않습니다. 요약에 숫자가 없으면 분모에 들어가지 않습니다. 틀린 예는 `_틀린 예` 에 `요약에 원문에 없는 숫자` 로 남습니다.

평가 레코드는 `records.pick_balanced()` 로 정답 종류(챗봇 6종, 분석: 카테고리 7종)별로 **같은 수**만큼 고릅니다. (반복 없음)

**묶음 생성 (`EVAL_BATCH`, 기본 8)**: 노트북(HF 모델) 평가는 여러 문장을 묶어 생성해 한 건씩보다 몇 배 빠릅니다. 레코드·채점은 그대로라 점수 비교에 영향이 없습니다.
- GPU 메모리가 모자라면 멈추지 않고 묶음 크기를 반씩 줄여(8 → 4 → 2 → 1) 다시 생성합니다.
- 세션의 첫 평가 전에 8건을 묶음/한 건씩 두 번 생성해 결과가 같은지 확인하고, 4분의 1 넘게 다르면 자동으로 한 건씩(1) 생성합니다.
- PC 의 Ollama·llama-server 평가는 지금처럼 한 건씩 요청합니다.
- 노트북은 학습 직후 **어댑터를 먼저 저장(8-1)** 하고 평가(9)합니다. 평가 점수는 끝난 뒤 `run_info.json` 에 덧붙입니다.
챗봇 결과의 `_종류별 도구 선택(맞음/전체)` 로 거절이 도구 호출로 쏠리는지 등을 확인하세요.

평가셋은 레코드의 group 해시(또는 레코드에 적은 `split`)로 정해지므로 학습 전후·HF/Ollama 결과를 공정하게 비교할 수 있습니다.
노트북 `EVAL_LIMIT` 과 PC 의 `--limit` 은 같은 값(기본 600)으로 두세요. 600 이면 챗봇 6종 x 100건, 분석은 한도와 상관없이 test 280건 전부입니다.
평가셋이 바뀐 적이 있으니(v8 정리: 챗봇 1,018 → 389건 / v9 보강: 챗봇 389 → 602건, 분석 109 → 280건), 예전 어댑터와 비교할 때는 **둘 다 지금 데이터 파일로** 평가하세요.

**학습 전·후 비교**: 노트북 6-1 이 LoRA 를 붙이기 전의 같은 모델로 같은 평가 레코드를 채점하고, 9번이 학습 후 점수와
나란히(`학습 전 / 학습 후 / 변화(%p)`) 보여 줍니다. 두 결과와 비교표는 어댑터 폴더의 `run_info.json` 에도 남습니다.
학습 전 점수는 `outputs/eval_before_<프롬프트 버전>_<평가 버전>_n<건수>….json` 에 저장되어, 같은 조건이면 다음 학습부터는 다시 재지 않습니다.
평가 방식을 바꾸면 `evaluate.EVAL_VERSION` 을 올리세요. (파일 이름이 바뀌어 예전 방식의 학습 전 점수를 불러오지 않음)

## 데이터 현황

| 데이터 | 건수 | 기준 |
|---|---|---|
| `chat_records.jsonl` | 2,601건 - v8 에서 6,077건의 문서 잡음·인사말만 다른 중복·같은 변형 반복을 정리(2,028건)하고 경계 사례 360건을 새로 씀 (2,388건). v9 에서 평가 전용 213건 추가 | label_guide 과제 1, "v8 데이터 정리", "v9 평가셋 보강" |
| `analysis_records.jsonl` | 862건 - v7 407건 + v8 경계·긴급도·검토 사례 284건(실민원 말투 51건 포함) + v9 평가 전용 171건. 분할 train 485 / val 97 / test 280 (카테고리별 40) | label_guide C1~C8, "v8 데이터 정리", "v9 평가셋 보강" |

챗봇 정답 종류별 건수: 접수 748 / 조회(대상 있음) 471 / 거절 385 / 조회(대상 없음) 380 / 문의 안내 353 / 접수(내용 없음) 264
(분할 train 1,623 / val 376 / test 602 - test 는 6종 각 100건, 접수만 102건)

v9 에서 넣은 레코드(`c-v9t-*`, `a-v9t-*`)는 모두 `"split": "test"` 로 고정한 평가 전용이라 학습·검증 데이터는 v8 과 같습니다.

### 종류별 비율 맞추기

학습·검증·평가 데이터는 모두 `records.pick_balanced()` 로 **정답 종류별로 같은 비율**이 되게 고릅니다.
챗봇은 6종이 각 1/6, 분석은 카테고리 7종이 각 1/7 입니다.

- 노트북 4번 셀의 `TRAIN_LIMIT` 을 500, 1000 처럼 바꿔도 비율은 같습니다. (예: 1000 → 종류마다 166~167건)
  `0` 이면 학습 분할 전체 건수(약 1,600건)를 같은 비율로 나눕니다. 많은 종류(접수·조회)는 일부만 쓰고 적은 종류는 반복합니다.
- 같은 건수면 항상 같은 레코드가 골라지고, 건수를 늘리면 앞서 고른 레코드는 그대로 두고 뒤에 더 붙습니다.
- 어떤 종류가 몫보다 적으면 `MAX_REPEAT`(기본 3)번까지 반복해 채우고, 그래도 모자란 자리만 다른 종류가 나눠 채웁니다.
- 분석은 `학습 분석 레코드 수 x ANALYSIS_REPEAT(기본 1)` 개를 카테고리별 같은 수로 고릅니다. (분석 데이터가 챗봇보다 적어 비중을 맞춤.
  분석 학습 485건, TRAIN_LIMIT=1000 이면 1 = 약 27%, 2 = 약 42%. v7(280건)의 2 = 약 30% 와 비슷한 값이 1 이라 기본값을 1 로 바꿨습니다.
  9번 분석 지표가 낮은데 8번 표의 analysis val loss 가 끝까지 내려가고 있었다면 2 로 올려 보세요)
- 비율을 고를 때 이미 맞추므로 `samples.build_samples()` 의 `analysis_repeat`, `no_tool_repeat` 은 1 로 둡니다. (예전의 `NO_TOOL_REPEAT` 설정은 없앴습니다)
데이터를 고친 뒤에는 `python model-training/records.py` 로 검사하세요.
