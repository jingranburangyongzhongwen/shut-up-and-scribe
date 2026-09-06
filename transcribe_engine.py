#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WhisperX Python API 封装：自动语种、community-1 分离。batch 按约 8GB 显存固定为 8。"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import time
from typing import Optional


DEFAULT_BATCH_SIZE = 8  # 按约 8GB 显存占用，暂不按卡片容量加 batch


def _audio_duration_s(path: str) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                path,
            ],
            text=True,
        )
        return float(out.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        print(f"警告: 无法读取音频时长: {e}")
        return 0.0


def annotation_to_df(annotation):
    import pandas as pd
    df = pd.DataFrame(
        annotation.itertracks(yield_label=True),
        columns=["segment", "label", "speaker"],
    )
    if df.empty:
        df["start"] = []
        df["end"] = []
        return df
    df["start"] = df["segment"].apply(lambda x: x.start)
    df["end"] = df["segment"].apply(lambda x: x.end)
    return df


def diarize_both(
    audio,
    hf_token: str = "",
    device: Optional[str] = None,
    cache_dir: Optional[str] = None,
    model_name: str = "pyannote/speaker-diarization-community-1",
):
    """一次分离，同时返回 overlapping 与 exclusive 两个 DataFrame。"""
    import torch
    from whisperx.audio import SAMPLE_RATE
    from whisperx.diarize import DiarizationPipeline

    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    token = hf_token or None
    pipeline = DiarizationPipeline(
        model_name=model_name,
        token=token,
        device=device,
        cache_dir=cache_dir,
    )
    if hasattr(audio, "ndim"):
        waveform = torch.from_numpy(audio[None, :])
        audio_data = {"waveform": waveform, "sample_rate": SAMPLE_RATE}
    else:
        audio_data = audio
    output = pipeline.model(
        audio_data, min_speakers=None, max_speakers=None,
    )
    overlapping = annotation_to_df(output.speaker_diarization)
    exclusive = annotation_to_df(output.exclusive_speaker_diarization)
    del pipeline
    return overlapping, exclusive


def transcribe_audio(
    audio_file: str,
    work_dir: str,
    hf_token: str = "",
    language: str = "",
    batch_size: Optional[int] = None,
    diarize_model: str = "pyannote/speaker-diarization-community-1",
    chunk_size: int = 30,
    force: bool = False,
    hotwords: str = "",
    exclusive_diarize: bool = True,
    precomputed_diarize_df=None,
    models_dir: str = "",
) -> dict:
    """转录 + 对齐 + 说话人分离。语言和人数均自动，不要求调用方指定。"""
    os.makedirs(work_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(audio_file))[0]
    srt_file = os.path.join(work_dir, f"{base}.srt")
    json_file = os.path.join(work_dir, f"{base}.json")
    stats_file = os.path.join(work_dir, f"{base}.stats.json")

    if os.path.isfile(srt_file) and not force:
        print(f"SRT 已存在，跳过转录: {srt_file}")
        stats = {}
        if os.path.isfile(stats_file):
            with open(stats_file, "r", encoding="utf-8") as f:
                stats = json.load(f)
        stats["srt_file"] = srt_file
        stats["json_file"] = json_file if os.path.isfile(json_file) else ""
        stats["skipped"] = True
        return stats

    from model_cache import configure_model_cache, default_models_dir, hub_paths

    models_dir = os.path.abspath(models_dir or default_models_dir())
    configure_model_cache(models_dir)
    paths = hub_paths(models_dir)

    import torch
    from whisperx.audio import load_audio
    from whisperx.alignment import DEFAULT_ALIGN_MODELS_TORCH, align, load_align_model
    from whisperx.asr import load_model
    from whisperx.diarize import assign_word_speakers
    from whisperx.utils import get_writer

    torch.hub.set_dir(paths["torch_hub"])

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "float32"
    if batch_size is None:
        batch_size = DEFAULT_BATCH_SIZE
    lang = language.strip() or None
    token = hf_token or os.environ.get("HF_TOKEN") or None

    duration_s = _audio_duration_s(audio_file)
    print(f"音频时长: {duration_s:.1f}s  device={device}  batch_size={batch_size}  diarize={diarize_model}")
    print("语言: 自动检测" if not lang else f"语言: {lang}")
    if hotwords:
        print(f"hotwords: {hotwords}")
    print(f"说话人分离: {'exclusive' if exclusive_diarize else 'overlapping'}")

    t0 = time.perf_counter()
    stage = {}

    audio = load_audio(audio_file)

    t = time.perf_counter()
    asr_options = {}
    if hotwords:
        asr_options["hotwords"] = hotwords
    model = load_model(
        "large-v3",
        device=device,
        compute_type=compute_type,
        language=lang,
        asr_options=asr_options or None,
        vad_options={"chunk_size": chunk_size},
        download_root=paths["hf_hub"],
        threads=4,
        use_auth_token=token,
    )
    try:
        result = model.transcribe(
            audio,
            batch_size=batch_size,
            chunk_size=chunk_size,
            print_progress=True,
            verbose=False,
        )
    except RuntimeError as e:
        if "out of memory" not in str(e).lower() or batch_size <= 4:
            raise
        print(f"显存不足（batch_size={batch_size}），降为 {max(4, batch_size // 2)} 重试")
        if device == "cuda":
            torch.cuda.empty_cache()
        batch_size = max(4, batch_size // 2)
        result = model.transcribe(
            audio,
            batch_size=batch_size,
            chunk_size=chunk_size,
            print_progress=True,
            verbose=False,
        )
    del model
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    stage["asr_s"] = round(time.perf_counter() - t, 2)

    detected = result.get("language") or lang or "zh"
    print(f"检测到语言: {detected}")

    t = time.perf_counter()
    align_dir = (
        paths["torch_ckpt"]
        if detected in DEFAULT_ALIGN_MODELS_TORCH
        else paths["hf_hub"]
    )
    align_model, align_metadata = load_align_model(detected, device, model_dir=align_dir)
    if align_model is not None and result.get("segments"):
        result = align(
            result["segments"],
            align_model,
            align_metadata,
            audio,
            device,
            print_progress=True,
        )
    del align_model
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    stage["align_s"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    if precomputed_diarize_df is not None:
        diarize_segments = precomputed_diarize_df
    else:
        overlapping_df, exclusive_df = diarize_both(
            audio,
            hf_token=token or "",
            device=device,
            cache_dir=paths["hf_hub"],
            model_name=diarize_model,
        )
        diarize_segments = exclusive_df if exclusive_diarize else overlapping_df
        if exclusive_diarize and (exclusive_df is None or exclusive_df.empty):
            print("exclusive 结果为空，回退 overlapping")
            diarize_segments = overlapping_df
    result = assign_word_speakers(diarize_segments, result)
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    stage["diarize_s"] = round(time.perf_counter() - t, 2)

    # WhisperX CLI 在未指定 --language 时会把 result.language 写成 "en"，
    # 导致中文 SRT 被按英文逐字加空格。这里写回真实检测结果。
    result["language"] = detected

    writer_args = {
        "highlight_words": False,
        "max_line_count": None,
        "max_line_width": None,
    }
    get_writer("srt", work_dir)(result, audio_file, writer_args)
    get_writer("json", work_dir)(result, audio_file, writer_args)

    compact = []
    speakers = []
    for seg in result.get("segments") or []:
        sp = seg.get("speaker")
        if sp and sp not in speakers:
            speakers.append(sp)
        compact.append({
            "start": seg.get("start"),
            "end": seg.get("end"),
            "speaker": sp,
            "text": (seg.get("text") or "").strip(),
        })
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(
            {"language": detected, "speakers": speakers, "segments": compact},
            f,
            ensure_ascii=False,
            indent=2,
        )

    wall = round(time.perf_counter() - t0, 2)
    stats = {
        "audio_file": audio_file,
        "srt_file": srt_file,
        "json_file": json_file,
        "audio_duration_s": round(duration_s, 2),
        "wall_s": wall,
        "rtfx": round(duration_s / wall, 2) if wall else None,
        "detected_language": detected,
        "speaker_count": len(speakers),
        "speakers": speakers,
        "segment_count": len(compact),
        "batch_size": batch_size,
        "chunk_size": chunk_size,
        "diarize_model": diarize_model,
        "exclusive_diarize": exclusive_diarize,
        "hotwords": hotwords,
        "asr_model": "large-v3",
        "device": device,
        "skipped": False,
        **stage,
    }
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(
        f"转录完成  语言={detected}  说话人={len(speakers)}  "
        f"墙钟={wall:.1f}s  RTFx={stats['rtfx']}"
    )
    return stats
