"""
Ollama 용 내보내기 - 어댑터 병합 -> GGUF 변환 -> Modelfile 작성.

  1. merge_adapter() : 베이스 모델을 bf16(원본 정밀도)으로 다시 올려 LoRA 를 합칩니다.
                       (4bit 로 올린 모델에 합치면 품질이 떨어지므로 학습 때와 다르게 bf16 으로 올림)
  2. to_gguf()       : llama.cpp 의 convert_hf_to_gguf.py 로 GGUF 한 파일을 만듭니다.
                       outtype 기본 q8_0 - 거의 손실 없이 크기를 bf16 의 절반으로. (llama.cpp 빌드 불필요)
                       더 작게 하려면 bf16 으로 만든 뒤 PC 에서 ollama create --quantize q4_K_M
  3. write_modelfile(): Ollama 기본 gemma4:e4b 와 같은 렌더러·파서·파라미터를 쓰는 Modelfile.
                       (RENDERER/PARSER gemma4 가 없으면 도구 호출을 해석하지 못함)

PC 에서 등록 (Modelfile 과 .gguf 를 같은 폴더에 두고)
    ollama create complaint-gemma4 -f Modelfile
    # bf16 GGUF 로 만들었다면:  ollama create complaint-gemma4 -f Modelfile --quantize q4_K_M

llama-server 용 내보내기 (export_llama_server) - 병합하지 않습니다.
  1. 베이스 GGUF  : 학습에 쓴 베이스(google/gemma-4-E4B-it)를 그대로 GGUF 로. 한 번 만들면 다음 학습부터는 다시 만들 필요 없음
  2. 어댑터 GGUF  : llama.cpp 의 convert_lora_to_gguf.py 로 LoRA 만 변환 (수십~수백 MB, 1~2분)
  PC 에서: scripts/run_llama_server.ps1 -Adapter <어댑터 gguf>  ->  dcm-back/.env 의 LLM_BACKEND=llama_server
"""

from __future__ import annotations

import gc
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import common

LLAMA_CPP_REPO = "https://github.com/ggml-org/llama.cpp"

# `ollama show gemma4:e4b --modelfile` 의 설정 (FROM·DRAFT·LICENSE 제외)
MODELFILE_TEMPLATE = """# {name} - complaint-ai 파인튜닝 모델 (베이스: {base})
# 만든 시각: {created_at} / 어댑터: {adapter}
FROM ./{gguf}
TEMPLATE {{{{ .Prompt }}}}
RENDERER gemma4
PARSER gemma4
PARAMETER temperature 1
PARAMETER top_k 64
PARAMETER top_p 0.95
"""


def disable_old_torchao() -> None:
    """
    Colab 에 기본으로 깔린 torchao 가 낡으면(0.16 미만) peft 가 bf16 모델에 LoRA 를 올릴 때
    'Found an incompatible version of torchao' ImportError 를 냅니다. (4bit 학습 때는 이 검사를 거치지 않음)
    이 프로젝트는 torchao 를 쓰지 않으므로, 낡은 torchao 가 있으면 peft 가 없는 것으로 보게 합니다.
    """
    from importlib import metadata

    try:
        version = metadata.version("torchao")
    except metadata.PackageNotFoundError:
        return
    from packaging.version import Version

    if Version(version) >= Version("0.16.0"):
        return
    for name, mod in list(sys.modules.items()):
        if name.startswith("peft") and hasattr(mod, "is_torchao_available"):
            mod.is_torchao_available = lambda: False
    print(f"참고: 낡은 torchao {version} 를 peft 에서 쓰지 않도록 했습니다. (병합에는 필요 없음)")


def merge_adapter(adapter_dir: str | Path, out_dir: str | Path) -> Path:
    """bf16 베이스 + LoRA 병합 결과를 HF 형식으로 저장합니다. (GPU 메모리 대신 CPU 메모리를 써도 됨)"""
    import torch
    from peft import PeftModel

    disable_old_torchao()

    from trainer import load_base_model

    adapter_dir, out_dir = Path(adapter_dir), Path(out_dir)
    tokenizer, base = load_base_model(common.BASE_MODEL, four_bit=False)
    model = PeftModel.from_pretrained(base, str(adapter_dir))
    merged = model.merge_and_unload()
    out_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(str(out_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(out_dir))
    # 프로세서 설정(멀티모달)도 같이 저장해 두면 변환기가 모델 구조를 알아보기 쉽습니다.
    try:
        from transformers import AutoProcessor
        AutoProcessor.from_pretrained(common.BASE_MODEL).save_pretrained(str(out_dir))
    except Exception:
        pass
    del model, merged, base
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return out_dir


def ensure_llama_cpp(workdir: str | Path) -> Path:
    """llama.cpp 저장소(변환 스크립트)를 받고 변환에 필요한 파이썬 패키지를 설치합니다."""
    workdir = Path(workdir)
    repo = workdir / "llama.cpp"
    if not (repo / "convert_hf_to_gguf.py").exists():
        subprocess.run(["git", "clone", "--depth", "1", LLAMA_CPP_REPO, str(repo)], check=True)
    req = repo / "requirements" / "requirements-convert_hf_to_gguf.txt"
    if req.exists():
        # torch·transformers 는 이미 설치된 것을 그대로 씁니다. (다시 설치하면 학습 환경이 바뀜)
        lines = [l for l in req.read_text().splitlines() if l.strip() and not l.lstrip().startswith(("torch", "transformers", "-r", "--extra-index"))]
        if lines:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", *lines], check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gguf", "sentencepiece", "protobuf"], check=False)
    return repo


def check_merged(merged_dir: str | Path, min_gb: float = 10.0) -> float:
    """변환할 HF 모델 폴더(병합 결과 또는 베이스 원본)에 가중치(.safetensors)가 제대로 들어 있는지 확인합니다. (bf16 E4B 는 약 16GB)"""
    files = sorted(Path(merged_dir).glob("*.safetensors"))
    total = sum(f.stat().st_size for f in files) / 1e9
    print(f"변환할 모델 가중치: {len(files)}개 파일, {total:.1f}GB ({merged_dir})")
    if not files or total < min_gb:
        raise RuntimeError(f"모델 가중치가 비정상입니다 ({total:.1f}GB < {min_gb}GB). 병합(또는 베이스 다운로드) 단계를 다시 확인하세요.")
    return total


def verify_gguf(path: str | Path, min_gb: float = 3.0) -> dict:
    """GGUF 가 끝까지 쓰였는지 확인합니다. (텐서 없이 헤더·어휘만 있는 ~15MB 파일을 걸러냄)"""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise RuntimeError(f"GGUF 헤더가 아닙니다: {path}")
    from gguf import GGUFReader

    reader = GGUFReader(str(path))
    n = len(reader.tensors)
    need = max((t.data_offset + t.n_bytes for t in reader.tensors), default=0)   # 마지막 텐서가 끝나는 위치
    print(f"GGUF 검증: {size / 1e9:.2f}GB, 텐서 {n}개, 필요한 크기 {need / 1e9:.2f}GB")
    if n == 0 or size < need or size < min_gb * 1e9:
        raise RuntimeError(f"GGUF 가 깨졌습니다 (크기 {size / 1e6:.0f}MB, 텐서 {n}개, 필요 {need / 1e6:.0f}MB). "
                           "변환이 중간에 끊겼거나 저장 공간이 모자랐을 가능성이 큽니다.")
    return {"size_bytes": size, "tensors": n}


# 변환 결과 크기 / 병합 모델(bf16) 크기 - 진행률 표시용 대략값 (비전·오디오 인코더는 GGUF 에 안 들어감)
_SIZE_RATIO = {"q8_0": 0.55, "bf16": 0.95, "f16": 0.95}


def _watch_size(out_file: Path, expected: float, stop: threading.Event, every: float = 20.0) -> None:
    """변환 중 GGUF 파일이 커지는 것을 주기적으로 출력합니다. (파일은 텐서를 쓰기 시작할 때부터 생김)"""
    started = time.time()
    last_size, last_time = 0, started
    while not stop.wait(every):
        elapsed = time.time() - started
        size = out_file.stat().st_size if out_file.exists() else 0
        if size <= 0:
            print(f"[GGUF 변환] 텐서 준비 중... 경과 {elapsed / 60:.1f}분", flush=True)
            continue
        speed = (size - last_size) / max(time.time() - last_time, 1e-6)
        last_size, last_time = size, time.time()
        pct = min(99, int(size * 100 / expected))
        remain = f" | 남은 시간 약 {(expected - size) / speed / 60:.1f}분" if speed > 0 and size < expected else ""
        print(f"[GGUF 변환] 파일 쓰는 중 {size / 1e9:.1f}GB / 약 {expected / 1e9:.1f}GB (약 {pct}%) | 경과 {elapsed / 60:.1f}분{remain}", flush=True)


def to_gguf(merged_dir: str | Path, out_file: str | Path, llama_cpp: str | Path, outtype: str = "q8_0") -> Path:
    """HF 모델 폴더 -> GGUF 한 파일. outtype: q8_0(기본) / bf16 / f16.  끝나면 파일이 온전한지 검증합니다."""
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.unlink(missing_ok=True)          # 이전에 끊긴 불완전한 파일이 남아 있지 않게
    merged_gb = check_merged(merged_dir)
    cmd = [sys.executable, str(Path(llama_cpp) / "convert_hf_to_gguf.py"), str(merged_dir),
           "--outfile", str(out_file), "--outtype", outtype]
    print(" ".join(cmd))
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_size, args=(out_file, merged_gb * 1e9 * _SIZE_RATIO.get(outtype, 0.55), stop), daemon=True)
    watcher.start()
    try:
        subprocess.run(cmd, check=True)
    finally:
        stop.set()
        watcher.join(timeout=2)
    verify_gguf(out_file)
    return out_file


def copy_verified(src: str | Path, dst_dir: str | Path) -> Path:
    """로컬 파일을 (드라이브 등) 목적지로 복사하고 크기가 같은지 확인합니다. 공간이 모자라면 미리 멈춥니다."""
    import os

    src, dst_dir = Path(src), Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dst_dir).free
    if free < src.stat().st_size * 1.05:
        raise RuntimeError(f"저장 공간 부족: 필요 {src.stat().st_size / 1e9:.1f}GB, 남음 {free / 1e9:.1f}GB ({dst_dir}). "
                           "드라이브 용량(구글 계정 15GB 공유)을 비우거나 q8_0 대신 더 작은 형식을 쓰세요.")
    dst = dst_dir / src.name
    shutil.copyfile(src, dst)
    os.sync()
    if dst.stat().st_size != src.stat().st_size:
        raise RuntimeError(f"복사 후 크기가 다릅니다: {src.stat().st_size} -> {dst.stat().st_size}")
    return dst


def write_modelfile(out_dir: str | Path, gguf_name: str, model_name: str, adapter: str, created_at: str) -> Path:
    path = Path(out_dir) / "Modelfile"
    path.write_text(MODELFILE_TEMPLATE.format(name=model_name, base=common.OLLAMA_BASE_MODEL, gguf=gguf_name,
                                              adapter=adapter, created_at=created_at), encoding="utf-8")
    return path


def export_all(adapter_dir: str | Path, run_name: str, outtype: str = "q8_0", model_name: str = "complaint-gemma4",
               work_dir: str | Path | None = None, keep_merged: bool = False, to_drive: bool = True) -> dict:
    """
    노트북에서 한 번에: 병합 -> GGUF -> Modelfile.
      work_dir : 병합 모델(약 16GB)과 llama.cpp 를 둘 곳. Colab 은 드라이브 대신 로컬 디스크(/content/...) 권장
      결과(GGUF + Modelfile + export_info.json)는 modelfiles/<run_name>/ 에 모입니다.
      to_drive=False : 드라이브에 쓰지 않고 work_dir/out/ (Colab 로컬 디스크)에만 만듭니다. 드라이브 용량이 모자랄 때.
                       이후 google.colab.files.download 등으로 PC 에 직접 내려받으세요.
    """
    from trainer import now_kst

    work = Path(work_dir) if work_dir else common.EXPORT_DIR / run_name
    out_dir = common.EXPORT_DIR / run_name if to_drive else work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_dir = work / f"merged-{run_name}"
    started = time.time()

    def stage(n: int, text: str) -> None:
        print(f"\n[{n}/4] {text} (시작 후 {(time.time() - started) / 60:.1f}분)", flush=True)

    stage(1, "어댑터 병합 - bf16 베이스 로드 + LoRA 합치기 (5~15분)")
    merge_adapter(adapter_dir, merged_dir)
    stage(2, "llama.cpp 변환 스크립트 준비 (1~3분)")
    llama_cpp = ensure_llama_cpp(work)
    gguf_name = f"{model_name}-{run_name}-{outtype}.gguf"
    stage(3, f"GGUF 변환 ({outtype}, 5~15분) - 아래에 llama.cpp 가 텐서 단위 진행을 출력합니다")
    local_gguf = to_gguf(merged_dir, work / "gguf" / gguf_name, llama_cpp, outtype)   # 로컬 디스크에 만들어 검증
    stage(4, "드라이브로 복사 + Modelfile 작성")
    if to_drive:
        gguf = copy_verified(local_gguf, out_dir)
    else:
        gguf = Path(shutil.move(str(local_gguf), str(out_dir / gguf_name)))
    verify_gguf(gguf)
    modelfile = write_modelfile(out_dir, gguf_name, model_name, str(adapter_dir), now_kst())
    info = {"gguf": str(gguf), "gguf_size_gb": round(gguf.stat().st_size / 1e9, 2), "modelfile": str(modelfile),
            "outtype": outtype, "ollama_create": f"ollama create {model_name} -f Modelfile"
            + (" --quantize q4_K_M" if outtype in ("bf16", "f16") else "")}
    (out_dir / "export_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if not keep_merged:
        shutil.rmtree(merged_dir, ignore_errors=True)
        shutil.rmtree(work / "gguf", ignore_errors=True)   # 약 16GB 를 차지하므로 기본은 지움
    print(f"\n내보내기 완료 - 총 {(time.time() - started) / 60:.1f}분")
    return info



# =============================================================================
# llama-server 용 - 베이스 GGUF + 어댑터 GGUF (병합하지 않음)
# =============================================================================
LLAMA_SERVER_DIR_NAME = "llama-server"          # model-training/modelfiles/llama-server/
BASE_CONFIG_PATTERNS = ["*.json", "*.jinja", "*.model", "tokenizer*"]   # 어댑터 변환에 필요한 베이스 설정만 (가중치 제외)


def base_gguf_name(outtype: str = "q8_0") -> str:
    return f"{common.BASE_MODEL.split('/')[-1]}-{outtype}.gguf"       # gemma-4-E4B-it-q8_0.gguf


def download_base(dest: str | Path, config_only: bool = False) -> Path:
    """학습에 쓴 베이스 모델(HF)을 받습니다. config_only=True 면 설정·토크나이저 파일만 (어댑터 변환용, 수 MB)."""
    from huggingface_hub import snapshot_download

    dest = Path(dest)
    kwargs = {"allow_patterns": BASE_CONFIG_PATTERNS} if config_only else {"ignore_patterns": ["*.gguf", "original/*"]}
    snapshot_download(common.BASE_MODEL, local_dir=str(dest), **kwargs)
    return dest


def to_lora_gguf(adapter_dir: str | Path, base_dir: str | Path, out_file: str | Path, llama_cpp: str | Path,
                 outtype: str = "f16") -> Path:
    """PEFT 어댑터 폴더 -> LoRA GGUF 한 파일. base_dir 는 베이스 모델의 config.json 이 있는 폴더 (텐서 이름 대응에 씀)."""
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.unlink(missing_ok=True)
    adapter_dir = Path(adapter_dir)
    if not (adapter_dir / "adapter_config.json").exists():
        raise RuntimeError(f"어댑터 폴더가 아닙니다 (adapter_config.json 없음): {adapter_dir}")
    cmd = [sys.executable, str(Path(llama_cpp) / "convert_lora_to_gguf.py"), str(adapter_dir),
           "--base", str(base_dir), "--outfile", str(out_file), "--outtype", outtype]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    info = verify_gguf(out_file, min_gb=0.001)
    from gguf import GGUFReader

    try:   # 어댑터 GGUF 는 general.type = "adapter" (gguf 버전에 따라 읽는 방식이 달라 실패하면 건너뜀)
        field = GGUFReader(str(out_file)).fields.get("general.type")
        kind = bytes(field.parts[field.data[0]]).decode("utf-8", "replace") if field else None
    except Exception:
        kind = None
    if kind and kind != "adapter":
        raise RuntimeError(f"어댑터 GGUF 가 아닙니다 (general.type={kind}): {out_file}")
    print(f"어댑터 GGUF: {out_file.name} ({info['size_bytes'] / 1e6:.0f}MB, 텐서 {info['tensors']}개)")
    return out_file


def export_llama_server(adapter_dir: str | Path, run_name: str, make_base: bool = True, base_outtype: str = "q8_0",
                        lora_outtype: str = "f16", adapter_name: str = "complaint-lora",
                        work_dir: str | Path | None = None, to_drive: bool = True) -> dict:
    """
    노트북에서 한 번에: (베이스 GGUF) + 어댑터 GGUF.
      make_base  : True 면 베이스 GGUF 도 만듭니다 (처음 한 번, 15~30분). PC 에 이미 있으면 False - 어댑터만 1~2분
                   같은 베이스(common.BASE_MODEL)로 학습하는 동안은 베이스 GGUF 를 계속 같이 씁니다.
      결과는 to_drive 면 model-training/modelfiles/llama-server/, 아니면 work_dir/out/ 에 모입니다. (Colab 은 로컬 디스크 권장)
    """
    from trainer import now_kst

    work = Path(work_dir) if work_dir else common.EXPORT_DIR / LLAMA_SERVER_DIR_NAME / "_work"
    out_dir = common.EXPORT_DIR / LLAMA_SERVER_DIR_NAME if to_drive else work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    total = 4 if make_base else 3

    def stage(n: int, text: str) -> None:
        print(f"\n[{n}/{total}] {text} (시작 후 {(time.time() - started) / 60:.1f}분)", flush=True)

    stage(1, "llama.cpp 변환 스크립트 준비 (1~3분)")
    llama_cpp = ensure_llama_cpp(work)
    base_name = base_gguf_name(base_outtype)
    n = 2
    if make_base:
        stage(n, f"베이스 GGUF 만들기 ({base_outtype}) - 베이스 다운로드 + 변환 (15~30분)")
        base_hf = download_base(work / "base-hf")
        local_base = to_gguf(base_hf, work / "gguf" / base_name, llama_cpp, base_outtype)
        base_gguf = copy_verified(local_base, out_dir) if to_drive else Path(shutil.move(str(local_base), str(out_dir / base_name)))
        verify_gguf(base_gguf)
        n += 1
    else:
        base_gguf = None
        print(f"베이스 GGUF 는 만들지 않습니다. PC 에 이미 있는 {base_name} 을 그대로 쓰세요.")
    stage(n, f"어댑터 GGUF 변환 ({lora_outtype})")
    base_cfg = work / "base-hf" if (work / "base-hf" / "config.json").exists() else download_base(work / "base-config", config_only=True)
    lora_name = f"{adapter_name}-{run_name}-{lora_outtype}.gguf"
    lora_gguf = to_lora_gguf(adapter_dir, base_cfg, out_dir / lora_name, llama_cpp, lora_outtype)
    stage(n + 1, "실행 안내 저장")
    run_cmd = (f".\\scripts\\run_llama_server.ps1 -LlamaServer <llama-server.exe 경로> "
               f"-BaseModel .\\model-training\\modelfiles\\llama-server\\{base_name} -Adapter .\\model-training\\modelfiles\\llama-server\\{lora_name}")
    info = {"created_at": now_kst(), "run_name": run_name, "adapter_dir": str(adapter_dir),
            "base_model": common.BASE_MODEL, "base_gguf": base_name, "base_gguf_made": bool(base_gguf),
            "lora_gguf": lora_name, "lora_size_mb": round(lora_gguf.stat().st_size / 1e6, 1),
            "run_llama_server": run_cmd,
            "env": {"LLM_BACKEND": "llama_server", "LLAMA_SERVER_ADAPTER_NAME": f"{adapter_name}-{run_name}"}}
    (out_dir / f"llama_server_info_{run_name}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(work / "gguf", ignore_errors=True)
    shutil.rmtree(work / "base-hf", ignore_errors=True)   # 베이스 원본(약 16GB)은 GGUF 를 만든 뒤 필요 없음
    print(f"\n내보내기 완료 - 총 {(time.time() - started) / 60:.1f}분")
    return info
