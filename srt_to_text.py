#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRT 转 整理文本格式
- 解析 WhisperX 输出的 SRT（含 [SPEAKER_XX] 标签）
- 支持自定义说话人名称（--names 参数）
- 合并同一说话人的连续段落
"""

import sys
import re
import os
import argparse
from datetime import datetime


def parse_srt(srt_file):
    """解析 SRT 文件，返回带说话人标签的字幕段落列表"""
    with open(srt_file, 'r', encoding='utf-8') as f:
        content = f.read()

    segments = []
    blocks = re.split(r'\n\s*\n', content.strip())

    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue

        # 找时间戳行
        time_line = None
        text_start = None
        for i, line in enumerate(lines):
            if '-->' in line:
                time_line = line
                text_start = i + 1
                break

        if time_line is None:
            continue

        # 提取文本（可能多行）和说话人
        # WhisperX 格式: [SPEAKER_XX]: 文本
        text_lines = []
        speaker = None
        for line in lines[text_start:]:
            m = re.match(r'^\[(SPEAKER_\d+)\]:\s*(.*)', line)
            if m:
                speaker = m.group(1)
                text_lines.append(m.group(2))
            else:
                text_lines.append(line)

        text = ' '.join(text_lines).strip()
        if text:
            start_ts, end_ts = [x.strip() for x in time_line.split('-->')]
            end_ts = end_ts.split()[0]
            segments.append({
                'speaker': speaker,
                'text': text,
                'time': time_line.strip(),
                'start': parse_timestamp(start_ts),
                'end': parse_timestamp(end_ts),
            })

    return segments


def parse_transcript_json(json_file):
    """读取引擎写出的 compact JSON（保留英文词间空格）。"""
    import json
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    segments = []
    for seg in data.get('segments') or []:
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        start = float(seg.get('start') or 0)
        end = float(seg.get('end') or start)
        segments.append({
            'speaker': seg.get('speaker'),
            'text': text,
            'start': start,
            'end': end,
            'time': f"{start:.3f} --> {end:.3f}",
        })
    return segments


def parse_timestamp(ts):
    """SRT 时间戳 → 秒。允许没有毫秒。"""
    ts = ts.strip().split()[0]
    h, m, rest = ts.split(':')
    if '.' in rest or ',' in rest:
        sec_s, frac = rest.replace(',', '.').split('.', 1)
        frac_val = int(frac) / (10 ** len(frac)) if frac else 0.0
    else:
        sec_s, frac_val = rest, 0.0
    return int(h) * 3600 + int(m) * 60 + int(sec_s) + frac_val


def speakers_by_appearance(segments):
    """按首次出场顺序收集说话人 ID。"""
    speaker_ids = []
    for seg in segments:
        sp = seg['speaker']
        if sp and sp not in speaker_ids:
            speaker_ids.append(sp)
    return speaker_ids


def detect_speakers(segments):
    """未指定 --names 时：按首次出场顺序分配【说话人1】、【说话人2】。"""
    return map_custom_names(segments, [])


def map_custom_names(segments, name_list):
    """按首次出场顺序对应名称。

    名单不够的说话人回退为【说话人N】（仍按出场顺序编号）。
    """
    speaker_map = {}
    for i, sp in enumerate(speakers_by_appearance(segments)):
        speaker_map[sp] = name_list[i] if i < len(name_list) else f'说话人{i + 1}'
    return speaker_map


# 同一说话人、停顿短、字数未超限才粘在一起；默认按「可读段落」而不是「整人一块」。
DEFAULT_MAX_PAUSE = 0.75
DEFAULT_MAX_CHARS = 180


def merge_segments(segments, speaker_map, max_pause=DEFAULT_MAX_PAUSE, max_chars=DEFAULT_MAX_CHARS):
    """合并同一说话人的连续段落。

    连续同说话人且间隙 <= max_pause、合并后不超过 max_chars 才粘连。
    说话人标签可以连续相同，避免主持人十分钟合成一块。
    传 max_pause=1e9, max_chars=10**9 可恢复旧的「按人粘」行为。
    """
    if not segments:
        return []

    merged = []
    current = None

    for seg in segments:
        sp = seg['speaker']
        role = speaker_map.get(sp, '未知')
        start = float(seg['start']) if 'start' in seg else 0.0
        end = float(seg['end']) if 'end' in seg else start
        text = seg['text']

        if current is None:
            current = {'role': role, 'text': text, 'start': start, 'end': end}
            continue

        gap = start - current['end']
        same = current['role'] == role
        would = len(current['text']) + 1 + len(text)
        if same and gap <= max_pause and would <= max_chars:
            current['text'] += ' ' + text
            current['end'] = end
        else:
            merged.append(current)
            current = {'role': role, 'text': text, 'start': start, 'end': end}

    if current:
        merged.append(current)

    return merged


def clean_text(text):
    """去掉中文词间空格，保留英文单词空格，并在中英边界补空格。"""
    text = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', text)
    text = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u3000-\u303f\uff00-\uffef])', '', text)
    text = re.sub(r'(?<=[\u3000-\u303f\uff00-\uffef])\s+(?=[\u4e00-\u9fff])', '', text)
    text = re.sub(r'([\u4e00-\u9fff])([A-Za-z0-9])', r'\1 \2', text)
    text = re.sub(r'([A-Za-z0-9])([\u4e00-\u9fff])', r'\1 \2', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def generate_header(speaker_count, source=''):
    """生成文件开头的标题部分"""
    lines = []
    if speaker_count <= 1:
        lines.append('==================== 语音转录文本 — 单人 ===================')
    else:
        lines.append(f'==================== 语音转录文本 — 共{speaker_count}位说话人 ===================')
    if source:
        lines.append(f'来源：{source}')
    lines.append(f'处理时间：{datetime.now().strftime("%Y-%m-%d")}')
    lines.append('')
    return '\n'.join(lines)


def generate_output(merged, source=''):
    """生成最终输出文本"""
    if not merged:
        return generate_header(0, source)

    # 统计说话人数量
    roles = set(item['role'] for item in merged)
    speaker_count = len(roles)

    parts = [generate_header(speaker_count, source)]

    first = True
    for item in merged:
        text = clean_text(item['text'])
        if not text:
            continue
        if not first:
            parts.append('')
        parts.append(f"【{item['role']}】{text}")
        first = False

    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description='SRT 转整理文本格式')
    subparsers = parser.add_subparsers(dest='command')

    # srt → 文本
    convert_parser = subparsers.add_parser('convert', help='SRT 转整理文本')
    convert_parser.add_argument('srt_file', help='输入 SRT 文件')
    convert_parser.add_argument('output_file', help='输出文本文件')
    convert_parser.add_argument(
        '--names',
        help='自定义说话人名称，逗号分隔，按首次出场顺序对应（如：主持人,嘉宾）',
    )
    convert_parser.add_argument('--source', default='', help='来源信息（URL 或文件路径）')
    convert_parser.add_argument('--max-pause', type=float, default=DEFAULT_MAX_PAUSE,
                                help='同说话人合并的最大停顿秒数（默认 0.75）')
    convert_parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS,
                                help='单段最大字符数，超出则另起一段（默认 180）')
    split_parser = subparsers.add_parser('split', help='将转录文本拆分为 chunk')
    split_parser.add_argument('input_file', help='输入转录文本文件')
    split_parser.add_argument('--max-chars', type=int, default=5000, help='每个 chunk 最大字符数（默认 5000）')

    args = parser.parse_args()

    if args.command is None:
        print("用法: srt_to_text.py convert <srt_file> <output_file> [--names ...]")
        print("      srt_to_text.py split <input_file> [--max-chars 5000]")
        sys.exit(1)

    if args.command == 'convert':
        srt_file = args.srt_file
        if not os.path.isfile(srt_file):
            print(f"错误：找不到输入文件: {srt_file}")
            sys.exit(1)

        if srt_file.lower().endswith('.json'):
            print(f"读取 JSON 文件: {srt_file}")
            segments = parse_transcript_json(srt_file)
        else:
            print(f"读取 SRT 文件: {srt_file}")
            segments = parse_srt(srt_file)
        print(f"共解析段落数: {len(segments)}")

        if not segments:
            print("错误：未能解析出任何字幕段落")
            sys.exit(1)

        # 确定说话人映射
        speaker_map = {}
        names = args.names
        source = args.source

        if names:
            name_list = [n.strip() for n in names.split(',') if n.strip()]
            speaker_map = map_custom_names(segments, name_list)
            print(f"自定义说话人名称（按出场顺序）: {speaker_map}")
        else:
            speaker_map = detect_speakers(segments)
            print(f"说话人映射（按出场顺序）: {speaker_map}")

        # 合并段落（短停顿才粘，避免整人一块）
        print("合并同说话人段落...")
        merged = merge_segments(
            segments, speaker_map,
            max_pause=args.max_pause, max_chars=args.max_chars,
        )
        print(f"合并后对话条数: {len(merged)}")

        # 生成输出
        output_file = args.output_file
        print(f"生成输出文件: {output_file}")
        output_text = generate_output(merged, source)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(output_text)

        print("完成！")
        # 预览前几行
        preview_lines = output_text.split('\n')[:8]
        print("\n预览:")
        for line in preview_lines:
            print(line)

    elif args.command == 'split':
        input_file = args.input_file
        max_chars = args.max_chars
        if not os.path.isfile(input_file):
            print(f"错误：找不到文件: {input_file}")
            sys.exit(1)

        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 按空行分段
        paragraphs = re.split(r'\n\s*\n', content)
        chunks = []
        current_chunk = []
        current_len = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if current_len + len(para) > max_chars and current_chunk:
                chunks.append('\n\n'.join(current_chunk))
                current_chunk = [para]
                current_len = len(para)
            else:
                current_chunk.append(para)
                current_len += len(para) + 2  # +2 for \n\n
        if current_chunk:
            chunks.append('\n\n'.join(current_chunk))

        base = os.path.splitext(os.path.basename(input_file))[0]
        out_dir = os.path.dirname(input_file) or '.'
        for i, chunk in enumerate(chunks):
            chunk_file = os.path.join(out_dir, f'{base}-chunk{i+1}.txt')
            with open(chunk_file, 'w', encoding='utf-8') as f:
                f.write(chunk)
            print(f"写入: {chunk_file} ({len(chunk)} 字符)")
        print(f"完成！共拆分为 {len(chunks)} 个 chunk")


if __name__ == '__main__':
    main()
