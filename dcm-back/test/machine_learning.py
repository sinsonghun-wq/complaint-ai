import os

import torch
from unsloth import FastLanguageModel
from datasets import load_dataset
from transformers import TrainingArguments
from trl import SFTTrainer
from test import UPLOAD_DIR

path = os.path.join(UPLOAD_DIR, "complaint_land.csv")

model_name = "Qwen/Qwen3-1.7B"
max_seq_length = 2056  
dtype = None  
load_in_4bit = True  

def training():
    # 1. 사전 학습된 모델 불러오기 (Unsloth 전용 설정 보완)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_name,  
        max_seq_length=max_seq_length,
        dtype=dtype,
        load_in_4bit=load_in_4bit,
    )

    # 2. LoRA 어댑터 설정 (PEFT)
    model = FastLanguageModel.get_peft_model(
        model,
        r=16,  
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_alpha=16,
        lora_dropout=0,  
        bias="none",  
        use_gradient_checkpointing="unsloth",  # VRAM 최적화 핵심 옵션
    )

    # 3. 프롬프트 템플릿 설정
    prompt_style = """Below is an instruction that describes a task. Write a response that appropriately completes the request.

    ### Instruction:
    {}

    ### Response:
    {}"""

    # 4. 데이터셋 준비
    dataset = load_dataset("csv", data_files=path, split="train[:1000]", encoding='cp949')


    def formatting_prompts_func(data):
        instructions = data["문의내용"]
        departments = data["답변부서"]
        outputs = data["답변내용"]
        texts = []
        for instruction, department, output in zip(instructions, departments, outputs):
            text = prompt_style.format(instruction, department + ": " + output) + tokenizer.eos_token
            texts.append(text)
        return {"text": texts}

    dataset = dataset.map(formatting_prompts_func, batched=True)

    args = TrainingArguments(
        per_device_train_batch_size=2,       
        gradient_accumulation_steps=4,       
        warmup_steps=5,
        max_steps=60,                        
        learning_rate=2e-4,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=1,
        optim="adamw_8bit",                  # 8비트 옵티마이저 적용 필수
        weight_decay=0.01,
        lr_scheduler_type="linear",
        seed=3407,
        output_dir="outputs",
    )

    # 5. 트레이너 설정 및 학습 시작
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=max_seq_length,
        dataset_num_proc=2,
        packing=False,  
        args=args  
    )

    # 학습 실행
    trainer.train()

    # 로컬에 저장하기
    model.save_pretrained_gguf(
    "complaint_model1_gguf", 
    tokenizer, 
    quantization_method="q4_k_m" # q4_k_m 또는 f16, q8_0 등 선택
    )


def getModel():
    # (불러올때)원본 모델 이름 대신 저장한 폴더 경로를 입력합니다.
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name="\\modelfiles\\complaint_model1_gguf", 
        max_seq_length=max_seq_length,
        dtype=dtype,
        load_in_4bit=load_in_4bit,
    )
    # [중요] 추론(텍스트 생성)을 더 빠르게 최적화하고 싶다면 아래 코드를 실행해 줍니다.
    FastLanguageModel.for_inference(model)
