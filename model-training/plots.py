"""
노트북 그래프 - 학습 중 loss 실시간 그래프, 학습 전·후 지표 막대그래프.

  - LiveLossPlot : transformers TrainerCallback. 로그가 찍힐 때마다(logging_steps 마다) train / val loss 그래프를
                   같은 자리에서 다시 그립니다. 학습이 끝날 때까지 기다리지 않고 진행을 볼 수 있습니다.
                   검증 셋이 과제별로 나뉘어 있으면(trainer.build_trainer 의 per_task_eval) 세 번째 칸에 과제별 val loss 를 그립니다.
  - plot_comparison : evaluate.compare_table() 결과를 과제(챗봇·분석)별 가로 막대그래프로 그립니다.

Colab 기본 글꼴에는 한글이 없어 지표 이름이 네모로 깨집니다. 노트북 2번 셀이 fonts-nanum 을 설치하고,
여기서 나눔고딕을 찾아 씁니다. (못 찾으면 경고만 내고 그대로 그림)
"""

from __future__ import annotations

import time
from pathlib import Path

TRAIN_COLOR = "#2563eb"
VAL_COLOR = "#ea580c"
TASK_COLORS = {"chat_call": "#0891b2", "chat_reply": "#7c3aed", "analysis": "#dc2626"}
TASK_LABELS = {"chat_call": "챗봇 도구 호출", "chat_reply": "챗봇 안내 문구", "analysis": "민원 분석"}
BEFORE_COLOR = "#9ca3af"
AFTER_COLOR = "#2563eb"
_font_ready = False


def setup_korean_font() -> None:
    """나눔고딕을 matplotlib 에 등록 (한 번만)."""
    global _font_ready
    if _font_ready:
        return
    import matplotlib
    from matplotlib import font_manager

    root = Path("/usr/share/fonts")
    candidates = list(root.rglob("NanumGothic*.ttf")) if root.exists() else []
    regular = [p for p in candidates if p.stem == "NanumGothic"] or candidates
    if not regular and root.exists():   # 나눔이 없으면 Noto Sans CJK (한글 포함)
        candidates = regular = sorted(root.rglob("NotoSansCJK-Regular.tt*"))
    if regular:
        for p in candidates:
            font_manager.fontManager.addfont(str(p))
        matplotlib.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular[0])).get_name()
    else:
        print("⚠️ 한글 글꼴(나눔고딕)을 찾지 못했습니다. 그래프의 한글이 네모로 보이면 2번 셀을 다시 실행하고 런타임을 재시작하세요.")
    matplotlib.rcParams["axes.unicode_minus"] = False
    _font_ready = True


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _ema(values: list[float], alpha: float = 0.1) -> list[float]:
    out, cur = [], None
    for v in values:
        cur = v if cur is None else alpha * v + (1 - alpha) * cur
        out.append(cur)
    return out


# =============================================================================
# 1. 학습 중 loss 실시간 그래프
# =============================================================================
def _callback_base():
    try:
        from transformers import TrainerCallback
        return TrainerCallback
    except ImportError:          # transformers 가 없는 환경(PC 에서 import 만 할 때)
        return object


class LiveLossPlot(_callback_base()):
    """
    trainer.build_trainer(..., extra_callbacks=[LiveLossPlot()]) 로 붙입니다.
    on_log 가 불릴 때마다 (= logging_steps 마다, 검증 직후에도) 그래프를 갱신합니다.
      왼쪽: train loss (옅은 선 = 원래 값, 진한 선 = 이동 평균)
      가운데: 전체 val loss (★ = 지금까지 가장 낮은 값 = 학습 끝에 되돌아갈 체크포인트)
      오른쪽: 과제별 val loss (로그 눈금) - 전체 val loss 는 쉬운 챗봇 샘플이 대부분이라
              분석 loss 가 따로 올라가도 가운데 그래프에는 잘 안 보입니다. 이 칸에서 확인합니다.
    """

    def __init__(self, title: str = "", save_path: str | Path | None = None):
        self.title = title
        self.save_path = Path(save_path) if save_path else None
        self.train: list[tuple[int, float]] = []
        self.val: list[tuple[int, float]] = []                    # 전체 검증
        self.val_tasks: dict[str, list[tuple[int, float]]] = {}   # 과제별 검증
        self.fig = None
        self._handle = None
        self._started = None
        self._max_steps = 0

    # --- 콜백 ---------------------------------------------------------------
    def on_train_begin(self, args, state, control, **kwargs):
        setup_korean_font()
        from matplotlib.figure import Figure   # pyplot 을 쓰지 않아 셀 끝에 그래프가 한 번 더 찍히지 않음
        self.fig = Figure(figsize=(16, 3.8), dpi=90)
        self._started = time.time()
        self._max_steps = state.max_steps
        self._draw(state)
        try:
            from IPython.display import display
            self._handle = display(self.fig, display_id=True)
        except Exception:
            self._handle = None

    def on_log(self, args, state, control, logs=None, **kwargs):
        from trainer import VAL_ALL, eval_losses

        logs = logs or {}
        if "loss" in logs:
            self.train.append((state.global_step, float(logs["loss"])))
        losses = eval_losses(logs)   # 검증 셋이 여러 개면 셋마다 on_log 가 따로 불림
        for name, value in losses.items():
            if name == VAL_ALL:
                self.val.append((state.global_step, value))
            else:
                self.val_tasks.setdefault(name, []).append((state.global_step, value))
        if "loss" in logs or losses:
            self._refresh(state)

    def on_train_end(self, args, state, control, **kwargs):
        self._refresh(state, final=True)
        if self.save_path and self.fig is not None:
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            self.fig.savefig(self.save_path, bbox_inches="tight")
            print("loss 그래프 저장:", self.save_path)

    # --- 그리기 -------------------------------------------------------------
    def _refresh(self, state, final: bool = False):
        if self.fig is None:
            return
        self._draw(state, final)
        if self._handle is not None:
            self._handle.update(self.fig)

    def _draw(self, state, final: bool = False):
        fig = self.fig
        fig.clear()
        ax_t, ax_v, ax_k = fig.subplots(1, 3)
        step, total = state.global_step, self._max_steps or state.max_steps
        elapsed = time.time() - (self._started or time.time())
        eta = elapsed / step * (total - step) if step and total > step else 0
        status = "완료" if final else f"남은 시간 약 {_fmt_time(eta)}" if step else "시작"
        fig.suptitle(f"{self.title + ' | ' if self.title else ''}step {step}/{total} | 경과 {_fmt_time(elapsed)} | {status}",
                     fontsize=11)

        # train
        if self.train:
            xs, ys = zip(*self.train)
            ax_t.plot(xs, ys, color=TRAIN_COLOR, alpha=0.3, linewidth=1)
            ax_t.plot(xs, _ema(list(ys)), color=TRAIN_COLOR, linewidth=2, label="이동 평균")
            ax_t.set_title(f"train loss (최근 {ys[-1]:.4f})", fontsize=10)
            ax_t.legend(loc="upper right", fontsize=8, frameon=False)
        else:
            ax_t.set_title("train loss (첫 로그 대기 중)", fontsize=10)

        # val
        if self.val:
            xs, ys = zip(*self.val)
            ax_v.plot(xs, ys, "o-", color=VAL_COLOR, linewidth=2, markersize=4)
            best_i = min(range(len(ys)), key=ys.__getitem__)
            ax_v.plot([xs[best_i]], [ys[best_i]], marker="*", markersize=14, color=VAL_COLOR,
                      markeredgecolor="black", linestyle="none", label=f"최저 {ys[best_i]:.4f} (step {xs[best_i]})")
            ax_v.set_title(f"val loss (최근 {ys[-1]:.4f})", fontsize=10)
            ax_v.legend(loc="upper right", fontsize=8, frameon=False)
        else:
            ax_v.set_title("val loss (첫 검증 대기 중)", fontsize=10)

        # 과제별 val
        if self.val_tasks:
            last = []
            for name, pts in self.val_tasks.items():
                xs, ys = zip(*pts)
                ax_k.plot(xs, ys, "o-", color=TASK_COLORS.get(name), linewidth=1.6, markersize=3,
                          label=f"{TASK_LABELS.get(name, name)} {ys[-1]:.4f}")
                last.append(ys[-1])
            if min(last) > 0:
                ax_k.set_yscale("log")
            ax_k.set_title("과제별 val loss (로그 눈금)", fontsize=10)
            ax_k.legend(loc="best", fontsize=8, framealpha=0.8)
        else:
            ax_k.set_title("과제별 val loss (첫 검증 대기 중)", fontsize=10)

        for ax in (ax_t, ax_v, ax_k):
            ax.set_xlabel("step")
            ax.set_ylabel("loss")
            ax.grid(alpha=0.3)
            if total:
                ax.set_xlim(0, total * 1.03)
            ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()


# =============================================================================
# 2. 학습 전·후 지표 막대그래프
# =============================================================================
TASK_NAMES = {"chat": "챗봇 (모드 0)", "analysis": "민원 분석"}


def plot_comparison(rows: list[dict], save_path: str | Path | None = None):
    """
    rows = evaluate.compare_table(before, after). 과제별로 가로 막대 (회색 = 학습 전, 파랑 = 학습 후),
    막대 끝에 값, 오른쪽에 변화(%p).
    """
    setup_korean_font()
    import matplotlib.pyplot as plt

    tasks = []
    for r in rows:
        if r["과제"] not in tasks:
            tasks.append(r["과제"])
    groups = {t: [r for r in rows if r["과제"] == t
                  and (isinstance(r["학습 전"], (int, float)) or isinstance(r["학습 후"], (int, float)))]
              for t in tasks}
    groups = {t: g for t, g in groups.items() if g}
    if not groups:
        print("그릴 지표가 없습니다.")
        return None

    heights = [max(len(g), 1) * 0.55 + 1.0 for g in groups.values()]
    fig, axes = plt.subplots(len(groups), 1, figsize=(10, sum(heights)), dpi=90,
                             gridspec_kw={"height_ratios": heights}, squeeze=False)
    bar_h = 0.38
    for ax, (task, g) in zip(axes[:, 0], groups.items()):
        names = [r["지표"] for r in g][::-1]           # 위에서부터 표 순서대로
        num = lambda v: v * 100 if isinstance(v, (int, float)) else None   # 값이 없으면 None (막대 없이 '-')
        before = [num(r["학습 전"]) for r in g][::-1]
        after = [num(r["학습 후"]) for r in g][::-1]
        delta = [r["변화(%p)"] for r in g][::-1]
        ys = range(len(names))
        ax.barh([y + bar_h / 2 for y in ys], [b or 0 for b in before], height=bar_h, color=BEFORE_COLOR, label="학습 전")
        ax.barh([y - bar_h / 2 for y in ys], [a or 0 for a in after], height=bar_h, color=AFTER_COLOR, label="학습 후")
        for y, b, a, d in zip(ys, before, after, delta):
            ax.text((b or 0) + 0.8, y + bar_h / 2, "-" if b is None else f"{b:.1f}", va="center", fontsize=8, color="#4b5563")
            ax.text((a or 0) + 0.8, y - bar_h / 2, "-" if a is None else f"{a:.1f}", va="center", fontsize=8, color=AFTER_COLOR)
            if d is not None:
                color = "#16a34a" if d > 0 else "#dc2626" if d < 0 else "#6b7280"
                ax.text(113, y, f"{d:+.1f}%p", va="center", ha="left", fontsize=9, color=color, fontweight="bold")
        ax.set_yticks(list(ys))
        ax.set_yticklabels(names, fontsize=9)
        ax.set_xlim(0, 125)
        ax.set_xticks(range(0, 101, 20))
        ax.set_xlabel("%")
        ax.set_title(TASK_NAMES.get(task, task), loc="left", fontsize=11, fontweight="bold")
        ax.grid(axis="x", alpha=0.3)
        ax.spines[["top", "right"]].set_visible(False)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=2, fontsize=9, frameon=False)
    fig.suptitle("학습 전 → 학습 후 (평가셋)", fontsize=12, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(save_path, bbox_inches="tight")
        print("비교 그래프 저장:", save_path)
    plt.show()
    return fig
