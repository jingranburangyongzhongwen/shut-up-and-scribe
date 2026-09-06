#!/usr/bin/env python3
"""标点恢复独立脚本（通过 subprocess 调用，隔离 transformers 导入）"""

import sys
import re
import argparse


PUNCT_MAP_ZH = {",": "，", ".": "。", "?": "？", "!": "！", ":": "："}


def _use_zh_punct(lang):
    """Whisper 可能给出 zh / zh-cn / chinese；仅中文把半角换成全角。"""
    code = (lang or "").strip().lower().replace("_", "-")
    return code.startswith("zh") or code in {"chinese", "cmn", "yue"}


def reconstruct_from_entities(chunk, entities, lang=""):
    """根据 pipeline 输出重建带标点的文本"""
    result = ""
    last_end = 0
    punct_map = PUNCT_MAP_ZH if _use_zh_punct(lang) else {}
    for ent in entities:
        start = ent["start"]
        end = ent["end"]
        label = ent.get("entity_group", ent.get("entity", "0"))
        result += chunk[last_end:start]
        result += chunk[start:end]
        if label in ",.?!:-":
            result += punct_map.get(label, label)
        last_end = end
    result += chunk[last_end:]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="输入文件路径")
    parser.add_argument("output", help="输出文件路径")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--language", default="", help="音频语言（zh/en 等），中文时自动转换标点")
    parser.add_argument("--models-dir", default="", help="模型缓存目录（默认 skill 目录下 models/）")
    args = parser.parse_args()

    from model_cache import configure_model_cache, default_models_dir, hub_paths
    models_dir = args.models_dir if args.models_dir else default_models_dir()
    configure_model_cache(models_dir)
    paths = hub_paths(models_dir)

    with open(args.input, "r", encoding="utf-8") as f:
        text = f.read()

    from transformers import pipeline
    import torch

    device_id = 0 if torch.cuda.is_available() else -1
    print(f"-> 加载标点恢复模型（{'GPU' if device_id >= 0 else 'CPU'}）...")
    pipe = pipeline(
        "token-classification",
        model="oliverguhr/fullstop-punctuation-multilang-large",
        device=device_id,
        aggregation_strategy="simple",
        cache_dir=paths["hf_hub"],
    )
    print("-> 模型加载完成")

    lines = text.split("\n")
    result_lines = list(lines)
    tasks = []
    all_chunks = []

    for i, line in enumerate(lines):
        if re.match(r"^【.+】", line):
            match = re.match(r"^(【.+】)(.*)", line)
            if match:
                prefix = match.group(1)
                content = match.group(2)
                if len(content) > 100:
                    chunks = [content[j : j + 100] for j in range(0, len(content), 100)]
                else:
                    chunks = [content]
                chunk_start = len(all_chunks)
                all_chunks.extend(chunks)
                tasks.append((i, prefix, len(chunks), chunk_start))

    if not tasks:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        return

    print(f"-> 批量处理 {len(all_chunks)} 个文本块（batch_size={args.batch_size}）...")
    batch_results = pipe(all_chunks, batch_size=args.batch_size)

    for line_idx, prefix, chunk_count, chunk_start in tasks:
        punctuated = ""
        for j in range(chunk_count):
            chunk_text = all_chunks[chunk_start + j]
            entities = batch_results[chunk_start + j]
            punctuated += reconstruct_from_entities(chunk_text, entities, args.language)
        result_lines[line_idx] = prefix + punctuated

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(result_lines))

    print("-> 标点恢复完成")


if __name__ == "__main__":
    main()
