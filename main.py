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
import subprocess
import argparse
import shutil
from datetime import datetime

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
    """运行命令。cmd 必须为列表，避免 shell 注入。
    始终输出 stdout/stderr 以便调试，check=False 时不吞掉错误信息。"""
    print(f"-> 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        # 只打印最后几行输出，避免刷屏
        lines = result.stdout.strip().split('\n')
        if len(lines) > 5:
            print(f"  输出(节选): {' '.join(lines[-3:])}")
        else:
            print(f"  输出: {result.stdout[:500]}")
    if result.returncode != 0:
        print(f"错误: 命令失败 (退出码 {result.returncode}): {' '.join(cmd)}")
        if result.stderr:
            # 完整输出错误信息，不被截断
            print(f"stderr:\n{result.stderr}")
        if check:
            sys.exit(result.returncode)
    return result


def download_audio(youtube_url, work_dir, video_id=None):
    """下载音频（YouTube / B站），返回音频文件路径"""
    if not video_id:
        video_id = extract_video_id(youtube_url)

    audio_file = os.path.join(work_dir, f"{video_id}.wav")

    if os.path.isfile(audio_file):
        print(f"音频文件已存在，跳过下载: {audio_file}")
        return audio_file

    # 下载（不限制 codec，兼容 YouTube / B站）
    cmd = [
        'yt-dlp', '-f', 'bestaudio/best',
        '-x', '--audio-format', 'wav',
        '--audio-quality', '0', '-o', audio_file, youtube_url
    ]
    run_cmd(cmd)
    return audio_file


def convert_audio(input_path, work_dir):
    """本地文件转WAV，返回音频文件路径"""
    base = os.path.splitext(os.path.basename(input_path))[0]
    audio_file = os.path.join(work_dir, f"{base}.wav")

    if os.path.isfile(audio_file):
        print(f"音频文件已存在，跳过转换: {audio_file}")
        return audio_file

    cmd = ['ffmpeg', '-i', input_path, '-ar', '16000', '-ac', '1', audio_file, '-y']
    run_cmd(cmd)
    return audio_file


def transcribe_whisperx(audio_file, work_dir, hf_token='', language=''):
    """WhisperX转录，返回SRT文件路径"""
    srt_file = audio_file.replace('.wav', '.srt')

    if os.path.isfile(srt_file):
        print(f"SRT文件已存在，跳过WhisperX: {srt_file}")
        return srt_file

    cmd = ['whisperx', audio_file, '--model', 'large-v3', '--diarize']
    if language:
        cmd.extend(['--language', language])
    if hf_token:
        cmd.extend(['--hf_token', hf_token])
    cmd.extend([
        '--diarize_model', 'pyannote/speaker-diarization-3.1',
        '--chunk_size', '10',
        '--segment_resolution', 'sentence',
        '--speaker_embeddings',
        '--output_format', 'srt', '--output_dir', work_dir
    ])

    run_cmd(cmd)
    return srt_file


def srt_to_text(script_dir, srt_file, output_file, names='', source=''):
    """SRT转整理文本"""
    srt_script = os.path.join(script_dir, 'srt_to_text.py')
    if not os.path.isfile(srt_script):
        print(f"错误: 找不到 SRT 处理脚本 {srt_script}")
        sys.exit(1)

    cmd = [sys.executable, srt_script, 'convert', srt_file, output_file]
    if names:
        cmd.extend(['--names', names])
    if source:
        cmd.extend(['--source', source])
    run_cmd(cmd)


def llm_proofread(output_file):
    """LLM校对标记：输出一个特殊标记，让调用方（Claude Code）接管校对"""
    print(f"__PROOFREAD_REQUIRED__:{output_file}")


def punctuate_text(input_file, output_file, script_dir, batch_size=32, language=''):
    """通过 subprocess 调用独立脚本恢复标点（隔离 transformers 导入）"""
    punctuate_script = os.path.join(script_dir, 'punctuate.py')
    if not os.path.isfile(punctuate_script):
        print(f"错误: 找不到标点恢复脚本 {punctuate_script}")
        sys.exit(1)
    cmd = [sys.executable, punctuate_script, input_file, output_file,
           '--batch-size', str(batch_size)]
    if language:
        cmd.extend(['--language', language])
    run_cmd(cmd)


def check_dependencies():
    """检查必要的依赖项，失败则直接退出"""
    print("-> 检查环境依赖...")
    # 1. 检查ffmpeg
    if not shutil.which('ffmpeg'):
        print("错误: 未找到 ffmpeg，请安装并添加到 PATH")
        print("下载地址: https://ffmpeg.org/download.html")
        sys.exit(1)
    # 2. 检查yt-dlp
    try:
        import yt_dlp
    except ImportError:
        print("错误: 未找到 yt-dlp，请运行: pip install yt-dlp")
        sys.exit(1)
    # 3. 检查whisperx
    try:
        import whisperx
    except ImportError:
        print("错误: 未找到 whisperx，请运行: pip install whisperx")
        sys.exit(1)
    # 4. 检查HF_TOKEN（可选，仅警告）
    hf_token = os.environ.get('HF_TOKEN', '')
    if not hf_token:
        print("警告: 未设置 HF_TOKEN 环境变量，说话人分离可能失败")
        print("请访问 https://huggingface.co 注册并创建 token，设置环境变量:")
        print("  Windows: set HF_TOKEN=hf_你的token")
        print("  Linux/Mac: export HF_TOKEN='hf_你的token'")
    print("环境检查通过 ✓\n")


def main():
    check_dependencies()
    parser = argparse.ArgumentParser(description='访谈字幕自动整理')
    parser.add_argument('input', help='YouTube URL 或本地文件路径')
    parser.add_argument('--names', help='自定义说话人名称，逗号分隔（如：小珺,江泽元）')
    parser.add_argument('--proofread', action='store_true', help='启用LLM校对')
    parser.add_argument('--cleanup', action='store_true', help='清理临时音频和SRT文件')
    parser.add_argument('--language', default='', help='音频语言（如 zh/en/ja），留空自动检测')
    parser.add_argument('--punctuate', action='store_true', help='使用 transformers pipeline 恢复标点')
    parser.add_argument('--punctuate-batch-size', type=int, default=32, help='标点恢复批处理大小（默认32）')
    parser.add_argument('--work-dir', default='', help='工作目录（默认当前目录）')

    args = parser.parse_args()

    # 确定工作目录
    work_dir = args.work_dir if args.work_dir else os.getcwd()
    os.makedirs(work_dir, exist_ok=True)

    # 获取脚本目录（同目录）
    script_dir = os.path.dirname(os.path.abspath(__file__))

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

    # 提取视频ID用于文件名
    if is_youtube or is_bilibili:
        video_id = extract_video_id(args.input)

    # 2. 下载/转换音频
    if is_youtube or is_bilibili:
        platform = 'YouTube' if is_youtube else 'B站'
        print(f"\n-> 步骤1: 下载{platform}音频...")
        audio_file = download_audio(args.input, work_dir, video_id)
    else:
        print(f"\n-> 步骤1: 转换音频...")
        if not os.path.isfile(args.input):
            print(f"错误: 找不到文件: {args.input}")
            sys.exit(1)
        audio_file = convert_audio(args.input, work_dir)

    # 2. WhisperX转录
    print(f"\n-> 步骤2: WhisperX转录（large-v3 + pyannote/speaker-diarization-3.1）...")
    srt_file = transcribe_whisperx(audio_file, work_dir, hf_token, language=args.language)

    # 3. SRT转整理文本
    print(f"\n-> 步骤3: 转换为整理文本...")
    base = os.path.splitext(os.path.basename(srt_file))[0]
    output_file = os.path.join(work_dir, f"{base}-转录文本.txt")
    srt_to_text(script_dir, srt_file, output_file,
                names=args.names or '', source=args.input)

    # 4. 标点恢复（可选）
    if args.punctuate:
        print(f"\n-> 步骤4: 恢复标点...")
        punctuate_text(output_file, output_file, script_dir,
                       batch_size=args.punctuate_batch_size,
                       language=args.language)
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
    try:
        if args.cleanup:
            if os.path.exists(audio_file):
                os.remove(audio_file)
                print(f"已删除WAV文件: {audio_file}")
            if os.path.exists(srt_file):
                os.remove(srt_file)
                print(f"已删除SRT文件: {srt_file}")
            print("清理完成")
        else:
            print(f"保留WAV文件: {audio_file}")
            print(f"保留SRT文件: {srt_file}")
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
    print("═══════════════════════════════════════")


if __name__ == '__main__':
    main()
