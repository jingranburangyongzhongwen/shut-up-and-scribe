#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 WhisperX / pyannote / transformers / torch.hub 的缓存统一到 skill 的 models/ 目录。

只认本 skill 的 models/（或 --models-dir）：已有则用，没有则下载到这里。
不读取、不迁移用户的 HF_HOME / TORCH_HOME / ~/.cache。
HuggingFace / torch 会在 import 时把环境变量固化成默认缓存；configure_model_cache
只在本进程写入这些变量，把第三方库钉到 models/。
"""

import os
import sys


def default_models_dir(script_dir=None):
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "models")


def hub_paths(models_dir):
    """skill 内的缓存布局。调用方应把这些路径传给库 API，而不是去读环境变量。"""
    models_dir = os.path.abspath(models_dir)
    hf_home = os.path.join(models_dir, "huggingface")
    hf_hub = os.path.join(hf_home, "hub")
    torch_home = os.path.join(models_dir, "torch")
    torch_hub = os.path.join(torch_home, "hub")
    return {
        "models_dir": models_dir,
        "hf_home": hf_home,
        "hf_hub": hf_hub,
        "torch_home": torch_home,
        "torch_hub": torch_hub,
        "torch_ckpt": os.path.join(torch_hub, "checkpoints"),
    }


def configure_model_cache(models_dir):
    """把本进程的第三方库默认缓存钉到 models_dir。"""
    paths = hub_paths(models_dir)
    os.makedirs(paths["hf_hub"], exist_ok=True)
    os.makedirs(paths["torch_ckpt"], exist_ok=True)

    # 本进程隔离针：第三方库没有 cache_dir 参数时才会落到这里。
    os.environ["HF_HOME"] = paths["hf_home"]
    os.environ["HF_HUB_CACHE"] = paths["hf_hub"]
    os.environ["HUGGINGFACE_HUB_CACHE"] = paths["hf_hub"]
    os.environ["TORCH_HOME"] = paths["torch_home"]

    hf_const = sys.modules.get("huggingface_hub.constants")
    if hf_const is not None:
        hf_const.HF_HOME = paths["hf_home"]
        hf_const.HF_HUB_CACHE = paths["hf_hub"]
        hf_const.HUGGINGFACE_HUB_CACHE = paths["hf_hub"]

    torch_mod = sys.modules.get("torch")
    if torch_mod is not None:
        torch_mod.hub.set_dir(paths["torch_hub"])

    print(f"模型目录: {paths['models_dir']}")
    print(f"  HuggingFace: {paths['hf_hub']}")
    print(f"  Torch hub:   {paths['torch_hub']}")
    return paths["models_dir"]


if __name__ == "__main__":
    configure_model_cache(default_models_dir())
