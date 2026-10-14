"""
QLoRA 학습 - 베이스 모델 로드, LoRA 부착, 학습, 저장.

backend(doc-classifier) 의 training/trainer.py 에서 Gemma 4 E4B 로 검증된 부분을 옮겨 왔습니다.
  - 4bit(NF4) 로 올리고 LoRA 가중치만 학습합니다. (Colab L4 24GB 기준)
  - LoRA 는 언어 모델의 어텐션·MLP 선형층(q/k/v/o, gate/up/down)에만 붙입니다. 비전·오디오 인코더,
    층별 임베딩(PLE) 투영층은 건드리지 않습니다.
  - loss 는 정답 토큰에만 겁니다. 정답 위치의 로짓만 계산(logits_to_keep)해서, 어휘 26만 개 x 전체 길이 크기의
    로짓을 만들지 않습니다. (메모리를 가장 크게 아끼는 부분)
  - loss 는 샘플마다 정답 토큰 평균을 낸 뒤 샘플끼리 평균합니다. (긴 분석 JSON 이 짧은 도구 호출을 압도하지 않게)
  - 길이가 비슷한 샘플끼리 배치로 묶고, 오른쪽 패딩을 씁니다.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import common
from samples import IGNORE, Sample

KST = timezone(timedelta(hours=9))


# =============================================================================
# 설정
# =============================================================================
@dataclass
class TrainConfig:
    # --- LoRA ---
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.1
    # --- 학습 ---
    learning_rate: float = 1e-4      # backend Gemma 4 v1 분석 결과 (2e-4 는 loss 가 튐)
    epochs: float = 1.0
    batch_size: int = 4              # 메모리 부족이면 2 로 (grad_accum 을 4 로 올려 곱 8 유지)
    grad_accum: int = 2
    warmup_ratio: float = 0.1
    scheduler: str = "cosine"
    weight_decay: float = 0.0
    max_grad_norm: float = 1.0
    evals_per_epoch: int = 8
    early_stopping_patience: int = 3  # 검증 loss 가 이 횟수만큼 연속으로 나아지지 않으면 멈춤 (0 이면 끝까지)
    logging_steps: int = 5
    save_total_limit: int = 2
    seed: int = 42
    gradient_checkpointing: bool = True   # Gemma 4 는 메모리를 많이 써서 켜 둠 (A100 에서만 끄기 시도)
    max_steps: int = -1               # 점검용: 양수면 그 스텝에서 멈춤

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# 모델 로드
# =============================================================================
def supports_bf16() -> bool:
    import torch
    return torch.cuda.is_available() and torch.cuda.is_bf16_supported()


def load_base_model(model_id: str = common.BASE_MODEL, four_bit: bool = True):
    """
    (tokenizer, model). four_bit=True 면 학습용 4bit(NF4), False 면 bf16 (병합·내보내기용).
    Gemma 4 는 멀티모달 모델이라 AutoModelForCausalLM 이 모르면 AutoModelForImageTextToText 로 올립니다.
    (텍스트만 넣어도 똑같이 동작하고, LoRA 는 언어 모델 쪽에만 붙습니다)
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    dtype = torch.bfloat16 if supports_bf16() else torch.float16
    kwargs: dict = {}
    if four_bit:
        from transformers import BitsAndBytesConfig
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype,
        )
        kwargs["device_map"] = {"": 0}
    else:
        kwargs["dtype"] = torch.bfloat16

    def _load(cls):
        try:
            return cls.from_pretrained(model_id, **kwargs)
        except TypeError:
            if "dtype" not in kwargs:
                raise
            return cls.from_pretrained(model_id, torch_dtype=kwargs.pop("dtype"), **kwargs)

    try:
        model = _load(AutoModelForCausalLM)
    except ValueError as exc:
        if "Unrecognized configuration class" not in str(exc):
            raise
        from transformers import AutoModelForImageTextToText
        model = _load(AutoModelForImageTextToText)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer, model


# =============================================================================
# LoRA
# =============================================================================
_SKIP_PARTS = ("visual", "vision", "lm_head", "embed", "merger", "mm_projector", "audio", "per_layer")
_STANDARD_TARGETS = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")


def pick_lora_targets(model) -> list[str]:
    """언어 모델 쪽 선형층 이름. (비전·오디오 인코더·출력층·임베딩·PLE 투영 제외)"""
    linear = []
    for name, module in model.named_modules():
        if type(module).__name__ not in ("Linear", "Linear4bit", "Linear8bitLt"):
            continue
        if any(part in name.lower() for part in _SKIP_PARTS):
            continue
        linear.append(name)
    names = [n for n in linear if n.rsplit(".", 1)[-1] in _STANDARD_TARGETS] or linear
    if not names:
        raise RuntimeError("LoRA 를 붙일 선형층을 찾지 못했습니다.")
    return names


def summarize_targets(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n in names:
        counts[n.rsplit(".", 1)[-1]] = counts.get(n.rsplit(".", 1)[-1], 0) + 1
    return dict(sorted(counts.items()))


def attach_lora(model, cfg: TrainConfig):
    from peft import LoraConfig, get_peft_model

    for p in model.parameters():
        p.requires_grad_(False)
    if cfg.gradient_checkpointing:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
    if hasattr(model, "config"):
        model.config.use_cache = False
    targets = pick_lora_targets(model)
    lora = LoraConfig(r=cfg.lora_r, lora_alpha=cfg.lora_alpha, lora_dropout=cfg.lora_dropout,
                      target_modules=targets, bias="none", task_type="CAUSAL_LM")
    return get_peft_model(model, lora), targets


def trainable_report(model) -> dict:
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    return {"학습되는 파라미터": trainable, "전체 파라미터(4bit 는 절반으로 셈)": total,
            "비율(%, 참고)": round(100 * trainable / max(total, 1), 3)}


# =============================================================================
# 데이터 / 손실
# =============================================================================
class SampleDataset:
    """항목 하나가 배치(샘플 묶음) 하나입니다. (Trainer 배치 크기는 1, collator 가 펼침)"""

    def __init__(self, batches: list[list[Sample]]):
        self.batches = batches

    def __len__(self) -> int:
        return len(self.batches)

    def __getitem__(self, i: int) -> list[dict]:
        return [s.to_dict() for s in self.batches[i]]


def make_batches(samples: list[Sample], batch_size: int, seed: int = 42, shuffle: bool = True,
                 mega: int = 16) -> list[list[Sample]]:
    """길이가 비슷한 샘플끼리 묶습니다. (학습은 섞은 뒤 큰 묶음 안에서만 길이순 - 무작위성 유지)"""
    items = list(samples)
    batch_size = max(1, int(batch_size))
    if shuffle:
        random.Random(seed).shuffle(items)
        chunk = batch_size * mega
        ordered: list[Sample] = []
        for i in range(0, len(items), chunk):
            ordered += sorted(items[i:i + chunk], key=lambda s: s.length, reverse=True)
        items = ordered
    else:
        items = sorted(items, key=lambda s: s.length, reverse=True)
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def make_collator(pad_id: int):
    import torch

    def collate(batch: list) -> dict:
        rows = [row for item in batch for row in (item if isinstance(item, list) else [item])]
        longest = max(len(r["input_ids"]) for r in rows)
        ids, labels, mask = [], [], []
        for r in rows:
            pad = longest - len(r["input_ids"])
            ids.append(r["input_ids"] + [pad_id] * pad)
            labels.append(r["labels"] + [IGNORE] * pad)
            mask.append([1] * len(r["input_ids"]) + [0] * pad)
        return {"input_ids": torch.tensor(ids), "labels": torch.tensor(labels), "attention_mask": torch.tensor(mask)}

    return collate


def answer_positions(label_rows: list[list[int]]) -> list[int]:
    """로짓을 계산할 위치 (오름차순). 위치 t 의 로짓이 t+1 번째 토큰을 예측합니다."""
    keep: set[int] = set()
    for row in label_rows:
        pos = [i for i, x in enumerate(row) if x != IGNORE]
        if pos:
            keep.update(range(pos[0] - 1, pos[-1]))
    if not keep:
        raise ValueError("정답 토큰이 없는 샘플입니다.")
    return sorted(keep)


def answer_losses(model, inputs):
    """샘플별 정답 구간 cross-entropy 평균 (B,) 과 정답 위치 로짓."""
    import torch
    import torch.nn.functional as F

    input_ids, labels = inputs["input_ids"], inputs["labels"]
    positions = answer_positions(labels.tolist())
    idx = torch.tensor(positions, device=input_ids.device)
    try:
        out = model(input_ids=input_ids, attention_mask=inputs.get("attention_mask"), use_cache=False, logits_to_keep=idx)
        logits = out.logits
        if logits.shape[1] != len(positions):
            raise TypeError("logits_to_keep 에 위치 목록을 지원하지 않는 모델")
    except (TypeError, IndexError, ValueError):
        keep = input_ids.shape[1] - positions[0]
        out = model(input_ids=input_ids, attention_mask=inputs.get("attention_mask"), use_cache=False, logits_to_keep=keep)
        logits = out.logits[:, -keep:, :][:, idx - positions[0], :]
    pred = logits.float()
    target = labels[:, idx + 1]
    ce = F.cross_entropy(pred.reshape(-1, pred.size(-1)), target.reshape(-1), ignore_index=IGNORE,
                         reduction="none").view(target.shape)
    valid = (target != IGNORE).float()
    return (ce * valid).sum(dim=1) / valid.sum(dim=1).clamp(min=1.0), pred


def _trainer_class():
    from transformers import Trainer

    class AnswerOnlyTrainer(Trainer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.model_accepts_loss_kwargs = False   # gradient accumulation 나눗셈은 Trainer 가 하도록

        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            per_sample, pred = answer_losses(model, inputs)
            loss = per_sample.mean()
            return (loss, {"logits": pred}) if return_outputs else loss

    return AnswerOnlyTrainer


def build_trainer(model, tokenizer, train_samples: list[Sample], val_samples: list[Sample], cfg: TrainConfig,
                  checkpoint_dir: str | Path, extra_callbacks: list | None = None):
    """extra_callbacks: 추가 TrainerCallback (예: plots.LiveLossPlot - 학습 중 loss 그래프 실시간 갱신)."""
    import torch
    from transformers import EarlyStoppingCallback, TrainingArguments

    train_batches = make_batches(train_samples, cfg.batch_size, seed=cfg.seed, shuffle=True)
    val_batches = make_batches(val_samples, cfg.batch_size, shuffle=False)
    steps_per_epoch = max(1, math.ceil(len(train_batches) / cfg.grad_accum))
    total_steps = cfg.max_steps if cfg.max_steps > 0 else math.ceil(steps_per_epoch * cfg.epochs)
    eval_steps = max(1, min(total_steps, steps_per_epoch) // max(cfg.evals_per_epoch, 1))
    bf16 = supports_bf16()
    kwargs = dict(
        output_dir=str(checkpoint_dir),
        per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=cfg.grad_accum,
        num_train_epochs=cfg.epochs, max_steps=cfg.max_steps,
        learning_rate=cfg.learning_rate, lr_scheduler_type=cfg.scheduler,
        weight_decay=cfg.weight_decay, max_grad_norm=cfg.max_grad_norm,
        logging_steps=cfg.logging_steps,
        eval_strategy="steps", eval_steps=eval_steps,
        save_strategy="steps", save_steps=eval_steps, save_total_limit=cfg.save_total_limit,
        load_best_model_at_end=True, metric_for_best_model="eval_loss", greater_is_better=False,
        bf16=bf16, fp16=not bf16 and torch.cuda.is_available(),
        optim="paged_adamw_8bit",
        gradient_checkpointing=False,     # attach_lora 에서 켬
        report_to="none", remove_unused_columns=False, prediction_loss_only=True,
        dataloader_num_workers=0, label_names=["labels"], seed=cfg.seed,
    )
    try:
        args = TrainingArguments(warmup_ratio=cfg.warmup_ratio, **kwargs)
    except TypeError:
        args = TrainingArguments(warmup_steps=cfg.warmup_ratio, **kwargs)   # 최신 transformers 는 비율을 warmup_steps 로 받음
    callbacks = []
    if cfg.early_stopping_patience > 0:
        callbacks.append(EarlyStoppingCallback(early_stopping_patience=cfg.early_stopping_patience))
    callbacks.extend(extra_callbacks or [])
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    trainer = _trainer_class()(
        model=model, args=args,
        train_dataset=SampleDataset(train_batches), eval_dataset=SampleDataset(val_batches),
        data_collator=make_collator(pad_id), callbacks=callbacks or None,
    )
    plan = {"train_batches": len(train_batches), "steps_per_epoch": steps_per_epoch, "total_steps": total_steps,
            "eval_steps": eval_steps, "effective_batch": cfg.batch_size * cfg.grad_accum, "bf16": bf16}
    return trainer, plan


def finish_training(trainer):
    """학습이 끝난 모델을 평가·저장용으로 정리 (autocast 래퍼 해제)."""
    model = trainer.model
    try:
        from accelerate.utils import extract_model_from_parallel
        model = extract_model_from_parallel(model, keep_fp32_wrapper=False)
    except Exception:
        pass
    model.eval()
    return model


def loss_history(trainer) -> dict[str, list]:
    """그래프용 (step, loss) 목록."""
    train, val = [], []
    for row in trainer.state.log_history:
        if "loss" in row:
            train.append((row["step"], row["loss"]))
        if "eval_loss" in row:
            val.append((row["step"], row["eval_loss"]))
    return {"train": train, "val": val}


# =============================================================================
# 저장
# =============================================================================
def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def save_run(model, tokenizer, run_dir: str | Path, info: dict) -> Path:
    """어댑터 + 토크나이저 + run_info.json 저장."""
    from app import prompts

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(run_dir))
    tokenizer.save_pretrained(str(run_dir))
    base = {"created_at": now_kst(), "base_model": common.BASE_MODEL, "prompt_version": prompts.PROMPT_VERSION,
            "enable_thinking": common.ENABLE_THINKING}
    base.update(info)
    (run_dir / "run_info.json").write_text(json.dumps(base, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return run_dir
