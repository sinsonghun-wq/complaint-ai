"""
학습 공통 설정 - 경로, 베이스 모델, 길이 제한.

노트북(colab_train.ipynb)과 각 스크립트가 이 값을 같이 씁니다. (import common) 학습마다 바꾸는 값(학습률, epoch 등)은
trainer.TrainConfig 에 있고, 노트북의 '학습 설정' 셀에서 바꿉니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent              # model-training/
REPO = ROOT.parent                                  # 저장소 루트 (dcm-back, dcm-front, model-training)
DCM_BACK = REPO / "dcm-back"
if str(DCM_BACK) not in sys.path:
    sys.path.insert(0, str(DCM_BACK))               # app.prompts / app.categories / app.ai 를 그대로 씀
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
CHAT_RECORDS = DATA_DIR / "chat_records.jsonl"
ANALYSIS_RECORDS = DATA_DIR / "analysis_records.jsonl"
TOOLS_FILE = DATA_DIR / "tools_mode0.json"          # export_prompts.py 결과

OUTPUT_DIR = ROOT / "outputs"                       # .gitignore 대상 - 체크포인트·어댑터·평가 결과
ADAPTERS_DIR = OUTPUT_DIR / "adapters"
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
EXPORT_DIR = ROOT / "modelfiles"                    # .gitignore 대상 - 병합 모델·GGUF·Modelfile

# 베이스 모델 = Ollama 의 gemma4:e4b
BASE_MODEL = "google/gemma-4-E4B-it"
OLLAMA_BASE_MODEL = "gemma4:e4b"

# 사고(thinking) 모드는 끕니다. 앱(ChatOllama, /api/chat)도 사고 모드를 켜지 않습니다.
ENABLE_THINKING = False

# 학습 샘플 최대 길이(토큰). 이보다 긴 샘플은 정답이 잘리지 않도록 버립니다.
# (분석 프롬프트 고정 부분 약 3,000자 + 원문 최대 1,500자 + 출력 = 약 4,600자)
# 한국어는 토큰 수가 글자 수에 가까울 수 있어, 1,500자로 잘리는 긴 원문까지 버려지지 않게 여유를 둡니다.
# 노트북 7번 셀의 '버려진 샘플(too_long_dropped)'이 0 인지 확인하세요.
MAX_LENGTH = 5120


# =============================================================================
# 진행 표시 - 오래 걸리는 반복 작업(평가·샘플 만들기·내보내기)에 "전체 N 중 몇 개 완료" 를 한 줄로 갱신
#   [학습 전] 챗봇 도구 호출  120/265 (45%) | 경과 3.2분 | 남은 시간 약 3.9분
# 추가 패키지 없이 '\r' 로 같은 줄을 덮어씁니다. (Colab 출력 칸·PC 터미널 모두 동작)
# =============================================================================
class Progress:
    def __init__(self, total: int, label: str, every_sec: float = 1.0):
        import time
        self._time = time.time
        self.total, self.label, self.every = max(int(total), 0), label, every_sec
        self.done = 0
        self.started = self._last = self._time()
        self._print(force=True)

    @staticmethod
    def _fmt(sec: float) -> str:
        return f"{sec:.0f}초" if sec < 60 else f"{sec / 60:.1f}분" if sec < 3600 else f"{sec / 3600:.1f}시간"

    def _line(self) -> str:
        elapsed = self._time() - self.started
        pct = f" ({self.done * 100 // self.total}%)" if self.total else ""
        line = f"{self.label}  {self.done}/{self.total}{pct} | 경과 {self._fmt(elapsed)}"
        if 0 < self.done < self.total:
            line += f" | 남은 시간 약 {self._fmt(elapsed / self.done * (self.total - self.done))}"
        return line

    def _print(self, force: bool = False, end: str = "") -> None:
        now = self._time()
        if force or now - self._last >= self.every:
            self._last = now
            print("\r" + self._line().ljust(90), end=end, flush=True)

    def step(self, n: int = 1) -> None:
        self.done += n
        self._print(force=self.done >= self.total)

    def close(self) -> None:
        self._print(force=True, end="\n")


def progress(items, label: str, total: int | None = None):
    """for x in progress(목록, '라벨'): ...  - 목록을 돌면서 진행 줄을 갱신합니다."""
    items = list(items) if total is None and not hasattr(items, "__len__") else items
    bar = Progress(len(items) if total is None else total, label)
    try:
        for x in items:
            yield x
            bar.step()
    finally:
        bar.close()
