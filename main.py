#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
访谈字幕自动整理主脚本
封装整个流程：下载/转换音频 → WhisperX转录 → SRT转整理文本 → 可选LLM校对
只需一次执行权限
"""

import sys
# 修复Windows控制台GBK编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import os
import re
import json
import time
import subprocess
import argparse
import shutil
from datetime import datetime

from model_cache import configure_model_cache, default_models_dir
from hotwords import extract_hotwords

def extract_video_id(url):
    """从 YouTube/B站 URL 提取视频 ID"""
    for pattern in [r'v=([^&]+)', r'youtu\.be/([^?&]+)', r'(?:shorts|embed)/([^?&]+)']:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    bv_match = re.search(r'(BV\w+)', url)
    if bv_match:
        return bv_match.group(1)
    return f"vid_{int(datetime.now().timestamp())}"


def run_cmd(cmd, check=True):
    """运行命令。cmd 必须为列表，避免 shell 注入。日志实时打印，不吞掉进度。"""
    print(f"-> 执行: {' '.join(str(x) for x in cmd)}", flush=True)
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"错误: 命令失败 (退出码 {result.returncode}): {' '.join(str(x) for x in cmd)}")
        if check:
            sys.exit(result.returncode)
    return result


def wav_is_16k_mono(path):
    """已有文件是否为 16 kHz 单声道。无法探测时返回 None。"""
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return False
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels",
                "-of", "csv=p=0",
                path,
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    line = (out or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[-1].replace(";", ",").split(",") if p.strip()]
    if len(parts) < 2:
        return None
    return parts[0] == "16000" and parts[1] == "1"


def ensure_16k_mono(src_path, dst_path):
    """统一成 WhisperX 需要的 16 kHz 单声道 WAV。旧缓存若不是 16k 单声道会重转。"""
    same = os.path.abspath(src_path) == os.path.abspath(dst_path)
    ok = wav_is_16k_mono(dst_path) if os.path.isfile(dst_path) else False
    if ok is True:
        if not same:
            print(f"16k WAV 已存在: {dst_path}")
        return dst_path
    if ok is None and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        print(f"无法探测采样率，沿用已有文件: {dst_path}")
        return dst_path
    if ok is False and os.path.isfile(dst_path):
        print(f"已有 WAV 不是 16k 单声道，重新转换: {dst_path}")

    if same:
        tmp_path = dst_path + ".16k.tmp.wav"
        cmd = ['ffmpeg', '-i', src_path, '-ar', '16000', '-ac', '1', '-y', tmp_path]
        run_cmd(cmd)
        os.replace(tmp_path, dst_path)
        return dst_path
    cmd = ['ffmpeg', '-i', src_path, '-ar', '16000', '-ac', '1', '-y', dst_path]
    run_cmd(cmd)
    return dst_path


def _unlink_quiet(path):
    if path and os.path.isfile(path):
        os.remove(path)
        print(f"已删除: {path}")


def remove_temp_files(paths, protected):
    """删除临时文件，跳过用户输入和成稿。"""
    protected = {os.path.abspath(p) for p in protected if p}
    seen = set()
    for path in paths:
        if not path:
            continue
        ap = os.path.abspath(path)
        if ap in seen or ap in protected or not os.path.isfile(ap):
            continue
        seen.add(ap)
        try:
            os.remove(ap)
            print(f"已删除: {ap}")
        except OSError as e:
            print(f"未能删除 {ap}: {e}")


def _ytdlp_cookie_args():
    """可选 cookies 文件或浏览器，不把内容打到日志。"""
    extra = []
    cookies = os.environ.get("YTDLP_COOKIES") or ""
    if cookies and os.path.isfile(cookies):
        extra.extend(["--cookies", cookies])
    browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER") or ""
    if browser:
        extra.extend(["--cookies-from-browser", browser])
    return extra


def fetch_video_meta(url, work_dir, video_id, retries=3):
    """拉取标题/简介/标签，供 hotwords 使用。失败不阻断主流程。"""
    meta_path = os.path.join(work_dir, f"{video_id}.info.json")
    if os.path.isfile(meta_path) and os.path.getsize(meta_path) > 0:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            # 旧缓存没有 chapters，需要重拉
            if "chapters" in cached:
                return cached
        except Exception:
            pass
    cmd = [
        "yt-dlp", "--dump-json", "--no-download",
        "--no-progress",
        * _ytdlp_cookie_args(),
        url,
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for attempt in range(1, retries + 1):
        print(f"-> 获取视频信息 ({attempt}/{retries})", flush=True)
        proc = subprocess.run(cmd, env=env, capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            try:
                data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "title": data.get("title") or "",
                            "description": data.get("description") or "",
                            "tags": data.get("tags") or [],
                            "chapters": [
                                {"title": c.get("title"), "start_time": c.get("start_time")}
                                for c in (data.get("chapters") or [])
                                if isinstance(c, dict)
                            ],
                            "id": data.get("id") or video_id,
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                return data
            except Exception as e:
                print(f"解析视频信息失败: {e}")
        else:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[-400:]
            print(f"获取视频信息失败 (code={proc.returncode}): {err}")
        if attempt < retries:
            time.sleep(min(8, 2 ** attempt))
    return {}


def download_audio(youtube_url, work_dir, video_id=None, retries=3):
    """下载音频（YouTube / B站），失败自动重试。返回 16k 单声道 WAV 路径。"""
    if not video_id:
        video_id = extract_video_id(youtube_url)

    audio_file = os.path.join(work_dir, f"{video_id}.wav")
    if os.path.isfile(audio_file) and os.path.getsize(audio_file) > 0:
        print(f"音频文件已存在，跳过下载: {audio_file}")
        return ensure_16k_mono(audio_file, audio_file)

    raw_file = os.path.join(work_dir, f"{video_id}.src.wav")
    cmd = [
        "yt-dlp", "-f", "bestaudio/best",
        "-x", "--audio-format", "wav",
        "--audio-quality", "0",
        "--retries", "5",
        "--fragment-retries", "5",
        * _ytdlp_cookie_args(),
        "-o", raw_file, youtube_url,
    ]

    last_code = 1
    for attempt in range(1, retries + 1):
        print(f"-> 下载音频 ({attempt}/{retries})", flush=True)
        result = run_cmd(cmd, check=False)
        last_code = result.returncode
        if last_code == 0:
            if os.path.isfile(raw_file) and os.path.getsize(raw_file) > 0:
                break
            candidates = [
                os.path.join(work_dir, name)
                for name in os.listdir(work_dir)
                if name.startswith(f"{video_id}") and name.lower().endswith(".wav")
                and os.path.abspath(os.path.join(work_dir, name)) != os.path.abspath(audio_file)
            ]
            if candidates:
                raw_file = max(candidates, key=os.path.getsize)
                if os.path.getsize(raw_file) > 0:
                    break
        elif os.path.isfile(raw_file):
            try:
                os.remove(raw_file)
                print(f"已删除不完整下载: {raw_file}")
            except OSError:
                pass
        if attempt < retries:
            wait = min(8, 2 ** attempt)
            print(f"下载未完成 (退出码 {last_code})，{wait}s 后重试")
            time.sleep(wait)
        else:
            print(f"下载未完成 (退出码 {last_code})")
    else:
        print(f"错误: 下载失败，已重试 {retries} 次")
        sys.exit(last_code or 1)

    if not os.path.isfile(raw_file):
        candidates = [
            os.path.join(work_dir, name)
            for name in os.listdir(work_dir)
            if name.startswith(f"{video_id}") and name.lower().endswith(".wav")
        ]
        if not candidates:
            print(f"错误: 下载后找不到音频: {raw_file}")
            sys.exit(1)
        raw_file = max(candidates, key=os.path.getsize)
    out = ensure_16k_mono(raw_file, audio_file)
    if os.path.abspath(raw_file) != os.path.abspath(out):
        try:
            _unlink_quiet(raw_file)
        except OSError as e:
            print(f"未能删除中间文件 {raw_file}: {e}")
    return out


def convert_audio(input_path, work_dir):
    """本地文件转 16k 单声道 WAV"""
    base = os.path.splitext(os.path.basename(input_path))[0]
    audio_file = os.path.join(work_dir, f"{base}.wav")
    return ensure_16k_mono(input_path, audio_file)


def transcribe_whisperx(audio_file, work_dir, hf_token='', language='', force=False, batch_size=None, hotwords='', exclusive_diarize=True, models_dir=''):
    """WhisperX 转录 + 说话人分离（语言和人数均自动）。"""
    from transcribe_engine import transcribe_audio
    return transcribe_audio(
        audio_file,
        work_dir,
        hf_token=hf_token,
        language=language,
        batch_size=batch_size,
        force=force,
        hotwords=hotwords,
        exclusive_diarize=exclusive_diarize,
        models_dir=models_dir,
    )


def srt_to_text(script_dir, srt_file, output_file, names='', source='', json_file=''):
    """SRT/JSON 转整理文本"""
    srt_script = os.path.join(script_dir, 'srt_to_text.py')
    if not os.path.isfile(srt_script):
        print(f"错误: 找不到 SRT 处理脚本 {srt_script}")
        sys.exit(1)

    src = json_file if json_file and os.path.isfile(json_file) else srt_file
    cmd = [sys.executable, srt_script, 'convert', src, output_file]
    if names:
        cmd.extend(['--names', names])
    if source:
        cmd.extend(['--source', source])
    run_cmd(cmd)


def llm_proofread(output_file):
    """LLM校对标记：输出一个特殊标记，让调用方（Claude Code）接管校对"""
    print(f"__PROOFREAD_REQUIRED__:{output_file}")


def punctuate_text(input_file, output_file, script_dir, batch_size=32, language='', models_dir=''):
    """通过 subprocess 调用独立脚本恢复标点（隔离 transformers 导入）"""
    punctuate_script = os.path.join(script_dir, 'punctuate.py')
    if not os.path.isfile(punctuate_script):
        print(f"错误: 找不到标点恢复脚本 {punctuate_script}")
        sys.exit(1)
    cmd = [sys.executable, punctuate_script, input_file, output_file,
           '--batch-size', str(batch_size)]
    if language:
        cmd.extend(['--language', language])
    if models_dir:
        cmd.extend(['--models-dir', models_dir])
    run_cmd(cmd)


# 与 README 跑通版本一致。community-1 的 exclusive 输出需要 whisperx>=3.8 且 pyannote.audio>=4。
PINNED_WHISPERX = "3.8.5"
PINNED_PYANNOTE_AUDIO = "4.0.4"
MIN_WHISPERX = (3, 8, 0)
MIN_PYANNOTE_AUDIO = (4, 0, 0)
_DIARIZE_INSTALL = (
    f'pip install "whisperx=={PINNED_WHISPERX}" "pyannote.audio=={PINNED_PYANNOTE_AUDIO}"'
)


def _version_tuple(ver):
    parts = []
    for chunk in (ver or "").replace("+", ".").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _pkg_version(dist_name):
    try:
        from importlib.metadata import version
        return version(dist_name)
    except Exception:
        return None


def _die_diarize_api(reason):
    print(f"错误: {reason}")
    print("说话人分离需要 pyannote community-1 的 exclusive API，")
    print("请按 README 安装跑通版本：")
    print(f"  {_DIARIZE_INSTALL}")
    sys.exit(1)


def _check_pkg_version(dist_name, min_tuple, pinned):
    ver = _pkg_version(dist_name)
    if not ver:
        _die_diarize_api(f"无法读取 {dist_name} 版本")
    if _version_tuple(ver) < min_tuple:
        _die_diarize_api(
            f"{dist_name} {ver} 过旧（需要 >={min_tuple[0]}.{min_tuple[1]}.{min_tuple[2]}，跑通版本 {pinned}）"
        )
    return ver


def _check_community1_api():
    """在加载大模型之前确认 exclusive_speaker_diarization 这条调用链存在。"""
    import inspect
    try:
        from whisperx.diarize import DiarizationPipeline
    except ImportError:
        _die_diarize_api("whisperx.diarize.DiarizationPipeline 不可导入")

    params = inspect.signature(DiarizationPipeline.__init__).parameters
    for name in ("token", "cache_dir"):
        if name not in params:
            _die_diarize_api(
                f"DiarizationPipeline 不支持 {name}=（当前 whisperx 过旧，默认模型还不是 community-1）"
            )

    try:
        from pyannote.audio.pipelines.speaker_diarization import DiarizeOutput
    except ImportError:
        _die_diarize_api(
            "找不到 pyannote DiarizeOutput（pyannote.audio 3.x 没有 exclusive 输出）"
        )

    fields = getattr(DiarizeOutput, "__dataclass_fields__", None) or getattr(
        DiarizeOutput, "__annotations__", {}
    )
    for name in ("speaker_diarization", "exclusive_speaker_diarization"):
        if name not in fields:
            _die_diarize_api(f"DiarizeOutput 缺少 {name}")

    # exclusive 走 DiarizationPipeline.model（与 whisperx 3.8 __call__ 内部相同）
    try:
        src = inspect.getsource(DiarizationPipeline.__call__)
    except (OSError, TypeError):
        src = ""
    if src and "self.model" not in src:
        _die_diarize_api(
            "DiarizationPipeline 不再通过 .model 调用 pyannote，exclusive 路径可能失效"
        )


def check_dependencies():
    """检查必要的依赖项，失败则直接退出"""
    print("-> 检查环境依赖...")
    # 1. 检查 ffmpeg / ffprobe（采样率探测和时长都要用）
    for bin_name in ("ffmpeg", "ffprobe"):
        if not shutil.which(bin_name):
            print(f"错误: 未找到 {bin_name}，请安装 ffmpeg 并添加到 PATH")
            print("下载地址: https://ffmpeg.org/download.html")
            sys.exit(1)
    # 2. 检查yt-dlp
    try:
        import yt_dlp
    except ImportError:
        print("错误: 未找到 yt-dlp，请运行: pip install yt-dlp")
        sys.exit(1)
    # 3. 检查 whisperx / pyannote.audio（community-1 exclusive API）
    try:
        import whisperx  # noqa: F401
    except ImportError:
        print("错误: 未找到 whisperx")
        print(f"请运行: {_DIARIZE_INSTALL}")
        sys.exit(1)
    wx_ver = _check_pkg_version("whisperx", MIN_WHISPERX, PINNED_WHISPERX)
    pa_ver = _check_pkg_version("pyannote.audio", MIN_PYANNOTE_AUDIO, PINNED_PYANNOTE_AUDIO)
    _check_community1_api()
    # 4. 检查HF_TOKEN（可选，仅警告）
    hf_token = os.environ.get('HF_TOKEN', '')
    if not hf_token:
        print("警告: 未设置 HF_TOKEN 环境变量，说话人分离可能失败")
        print("请访问 https://huggingface.co 注册并创建 token，设置环境变量:")
        print("  Windows: set HF_TOKEN=hf_你的token")
        print("  Linux/Mac: export HF_TOKEN='hf_你的token'")
    print(f"环境检查通过 ✓  whisperx {wx_ver}  pyannote.audio {pa_ver}\n")


def main():
    parser = argparse.ArgumentParser(description='访谈字幕自动整理')
    parser.add_argument('input', help='YouTube URL 或本地文件路径')
    parser.add_argument('--names', help='自定义说话人名称，逗号分隔，按首次出场顺序对应（如：主持人,嘉宾）')
    parser.add_argument('--proofread', action='store_true', help='启用LLM校对')
    parser.add_argument('--cleanup', action='store_true', help='清理临时文件（不删用户输入和成稿）')
    parser.add_argument('--language', default='', help='音频语言（如 zh/en/ja），留空自动检测')
    parser.add_argument('--punctuate', action='store_true', help='使用 transformers pipeline 恢复标点')
    parser.add_argument('--punctuate-batch-size', type=int, default=32, help='标点恢复批处理大小（默认32）')
    parser.add_argument('--work-dir', default='', help='工作目录（默认当前目录）')
    parser.add_argument('--models-dir', default='', help='模型缓存目录（默认 skill 目录下 models/）')
    parser.add_argument('--force', action='store_true', help='忽略已有 SRT，强制重新转录')
    parser.add_argument('--batch-size', type=int, default=0, help='Whisper 批大小（0=默认 8，约 8GB 显存）')
    parser.add_argument('--hotwords', default='', help='覆盖自动热词（默认从标题/简介/标签/章节抽取）')
    parser.add_argument('--no-hotwords', action='store_true', help='关闭热词提示')
    parser.add_argument('--no-exclusive-diarize', action='store_true', help='使用 overlapping 分离（默认 exclusive）')

    args = parser.parse_args()

    # 确定工作目录
    work_dir = args.work_dir if args.work_dir else os.getcwd()
    os.makedirs(work_dir, exist_ok=True)

    # 获取脚本目录（同目录）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    models_dir = args.models_dir if args.models_dir else default_models_dir(script_dir)
    configure_model_cache(models_dir)

    check_dependencies()

    # 获取HF_TOKEN
    hf_token = os.environ.get('HF_TOKEN', '')

    print("═══════════════════════════════════════")
    print("  访谈字幕自动整理")
    print("═══════════════════════════════════════")

    # 1. 解析输入
    youtube_pattern = re.compile(
        r'(https?://)?(www\.|m\.)?'
        r'(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)[^\s]+'
    )
    bilibili_pattern = re.compile(
        r'(https?://)?(www\.|m\.)?(bilibili\.com/video/|b23\.tv/)[^\s]+',
        re.IGNORECASE
    )
    is_youtube = bool(youtube_pattern.match(args.input))
    is_bilibili = bool(bilibili_pattern.match(args.input))
    ext = os.path.splitext(args.input)[1].lower()
    is_subtitle = (not is_youtube and not is_bilibili and ext in {'.srt', '.txt'})

    audio_file = None
    srt_file = None
    stats = {}
    hotwords = ''

    if is_subtitle:
        print(f"\n-> 步骤1: 直接处理字幕文件...")
        if not os.path.isfile(args.input):
            print(f"错误: 找不到文件: {args.input}")
            sys.exit(1)
        srt_file = args.input
    elif is_youtube or is_bilibili:
        video_id = extract_video_id(args.input)
        platform = 'YouTube' if is_youtube else 'B站'
        print(f"\n-> 步骤1: 下载{platform}音频...")
        audio_file = download_audio(args.input, work_dir, video_id)
        meta = fetch_video_meta(args.input, work_dir, video_id)
        if args.no_hotwords:
            hotwords = ''
        elif args.hotwords:
            hotwords = args.hotwords
        else:
            hotwords = extract_hotwords(
                title=meta.get('title') or '',
                description=meta.get('description') or '',
                tags=meta.get('tags') or [],
                chapters=meta.get('chapters') or [],
            )
            if hotwords:
                print(f"自动热词: {hotwords}")
    else:
        print(f"\n-> 步骤1: 转换音频...")
        if not os.path.isfile(args.input):
            print(f"错误: 找不到文件: {args.input}")
            sys.exit(1)
        audio_file = convert_audio(args.input, work_dir)
        if args.hotwords and not args.no_hotwords:
            hotwords = args.hotwords

    if not is_subtitle:
        print(f"\n-> 步骤2: WhisperX 转录（large-v3 + community-1，语言/人数自动）...")
        stats = transcribe_whisperx(
            audio_file, work_dir, hf_token,
            language=args.language,
            force=args.force,
            batch_size=(args.batch_size or None),
            hotwords=hotwords,
            exclusive_diarize=not args.no_exclusive_diarize,
            models_dir=models_dir,
        )
        srt_file = stats.get('srt_file') or audio_file.replace('.wav', '.srt')
    else:
        print(f"\n-> 步骤2: 跳过转录")

    print(f"\n-> 步骤3: 转换为整理文本...")
    base = os.path.splitext(os.path.basename(srt_file))[0]
    output_file = os.path.join(work_dir, f"{base}-转录文本.txt")
    srt_to_text(script_dir, srt_file, output_file,
                names=args.names or '', source=args.input,
                json_file=stats.get('json_file') or '')

    # 4. 标点恢复（可选）。--language 优先，否则用 ASR 检测结果；
    # zh 会把 , . 换成 ，。；en 保持半角（英文稿通常已有标点）。
    if args.punctuate:
        punct_lang = args.language or stats.get('detected_language') or ''
        print(f"\n-> 步骤4: 恢复标点...")
        if punct_lang:
            print(f"标点语言: {punct_lang}")
        punctuate_text(output_file, output_file, script_dir,
                       batch_size=args.punctuate_batch_size,
                       language=punct_lang,
                       models_dir=models_dir)
    else:
        print(f"\n-> 步骤4: 跳过标点恢复（如需启用请加 --punctuate）")

    # 5. LLM校对（可选）
    if args.proofread:
        print(f"\n-> 步骤5: LLM校对...")
        llm_proofread(output_file)
    else:
        print(f"\n-> 步骤5: 跳过LLM校对（如需启用请加 --proofread）")

    # 6. 清理临时文件（可选）
    print(f"\n-> 步骤6: 清理临时文件...")
    stem = os.path.splitext(audio_file)[0] if audio_file else (
        os.path.splitext(srt_file)[0] if srt_file else ""
    )
    temp_paths = []
    if stem:
        temp_paths.extend([
            stem + ".wav",
            stem + ".src.wav",
            stem + ".srt",
            stem + ".json",
            stem + ".stats.json",
            stem + ".info.json",
            stem + ".16k.tmp.wav",
            stem + ".wav.16k.tmp.wav",
        ])
    if audio_file:
        temp_paths.append(audio_file)
    if srt_file:
        temp_paths.append(srt_file)
    if stats.get("json_file"):
        temp_paths.append(stats["json_file"])
    try:
        if args.cleanup:
            remove_temp_files(temp_paths, protected=[args.input, output_file])
            print("清理完成")
        else:
            if audio_file:
                print(f"保留WAV文件: {audio_file}")
            if srt_file and os.path.abspath(srt_file) != os.path.abspath(args.input):
                print(f"保留SRT文件: {srt_file}")
            if stem and os.path.isfile(stem + ".json"):
                print(f"保留JSON文件: {stem}.json")
    except Exception as e:
        print(f"清理警告: {e}")

    # 报告
    print("\n═══════════════════════════════════════")
    print("  处理完成 ✓")
    print("═══════════════════════════════════════")
    print(f"  输出文件: {output_file}")
    if os.path.isfile(output_file):
        size = os.path.getsize(output_file)
        print(f"  文件大小: {size} 字节")
    if stats:
        if stats.get('detected_language'):
            print(f"  检测语言: {stats['detected_language']}")
        if stats.get('speaker_count') is not None:
            print(f"  说话人数: {stats['speaker_count']}")
        if stats.get('wall_s') is not None:
            print(f"  转录耗时: {stats['wall_s']}s  RTFx={stats.get('rtfx')}")
    print("═══════════════════════════════════════")


if __name__ == '__main__':
    main()
