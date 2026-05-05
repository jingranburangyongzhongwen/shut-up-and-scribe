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
            segments.append({
                'speaker': speaker,
                'text': text,
                'time': time_line.strip()
            })

    return segments


def detect_speakers(segments):
    """
    检测说话人并分配通用标签：
    - 按出现顺序分配【说话人1】、【说话人2】等
    """
    speaker_ids = []

    # 收集所有说话人ID（按出现顺序）
    for seg in segments:
        sp = seg['speaker']
        if sp and sp not in speaker_ids:
            speaker_ids.append(sp)

    # 按出现顺序分配通用标签
    speaker_map = {}
    for idx, sp in enumerate(speaker_ids):
        speaker_map[sp] = f'说话人{idx + 1}'

    return speaker_map


def merge_segments(segments, speaker_map):
    """合并同一说话人的连续段落"""
    if not segments:
        return []

    merged = []
    current = None

    for seg in segments:
        sp = seg['speaker']
        role = speaker_map.get(sp, '未知')

        if current is None:
            current = {'role': role, 'text': seg['text']}
        elif current['role'] == role:
            # 同一说话人，合并
            current['text'] += ' ' + seg['text']
        else:
            merged.append(current)
            current = {'role': role, 'text': seg['text']}

    if current:
        merged.append(current)

    return merged


def clean_text(text):
    """去除 WhisperX 逐字空格输出格式，还原正常文本"""
    # WhisperX 对中英文都是逐字加空格："大 家 看" / "h e l l o"
    # 判断方式：中文字后紧跟空格，或连续多个单字母后跟空格
    if re.search(r'[一-鿿] ', text) or re.search(r'(?:^| )\S (?:\S ){2,}', text):
        text = text.replace(' ', '')
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
    convert_parser.add_argument('--names', help='自定义说话人名称，逗号分隔（如：小珺,江泽元）')
    convert_parser.add_argument('--source', default='', help='来源信息（URL 或文件路径）')
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
            print(f"错误：找不到 SRT 文件: {srt_file}")
            sys.exit(1)

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
            # 用户自定义名称
            name_list = [n.strip() for n in names.split(',')]
            speaker_ids = []
            for seg in segments:
                sp = seg['speaker']
                if sp and sp not in speaker_ids:
                    speaker_ids.append(sp)
            for i, sp in enumerate(speaker_ids):
                if i < len(name_list):
                    speaker_map[sp] = name_list[i]
                else:
                    speaker_map[sp] = f'说话人{i+1}'
            print(f"自定义说话人名称: {speaker_map}")
        else:
            # 自动检测
            print("自动检测说话人角色...")
            speaker_map = detect_speakers(segments)
            print(f"说话人映射: {speaker_map}")

        # 合并段落
        print("合并同说话人段落...")
        merged = merge_segments(segments, speaker_map)
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
