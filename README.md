# shut-up-and-scribe

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

一条命令，把 YouTube / B站 / 本地音视频变成**可直接阅读的对话文本** —— 自动下载、转录、识别说话人、整理输出。

![logo](./imgs/introduce-img.png)

---

## 快速上手

```
/shut-up-and-scribe https://youtu.be/ZDXIUO7CRzo
/shut-up-and-scribe https://www.bilibili.com/video/BVxxxx
/shut-up-and-scribe interview.mp4
```

## 环境要求与安装指引

### 1. 安装 ffmpeg
- **Windows**: 从 https://github.com/GyanD/codexffmpeg/releases?page=5 下载版本 7 的 shared，解压后将 `bin/` 目录加入系统 PATH
- **macOS**: `brew install ffmpeg`
- **Linux** (Debian/Ubuntu): `sudo apt update && sudo apt install ffmpeg`

但是目前还会有 torchcodec 的warning，暂未解决，不影响运行

### 2. 配置 HuggingFace Token（说话人分离必需）

1. **获取 Token**：
   *   注册/登录 [huggingface.co](https://huggingface.co)。
   *   进入 **Settings → Access Tokens**，创建一个具有 `read` 权限的 token。
2. **同意模型协议（必做）**：
   必须手动访问以下页面并点击 **"Accept Conditions"**，否则说话人分离无法下载模型：
   *   [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)（当前默认）
3. **设置环境变量 `HF_TOKEN`**：

#### **Windows (持久化)**
*   **方法 A：PowerShell（推荐）**
    ```powershell
    [System.Environment]::SetEnvironmentVariable('HF_TOKEN', 'hf_你的token', 'User')
    # 💡 注意：设置后需重启终端、IDE 或 VS Code 才能生效
    ```
*   **方法 B：命令提示符 (CMD)**
    ```cmd
    setx HF_TOKEN "hf_你的token"
    ```
*   **方法 C：图形界面 (GUI)**
    `右键此电脑` -> `属性` -> `高级系统设置` -> `环境变量` -> `用户变量` -> `新建`。

#### **Linux / macOS (持久化)**
将以下内容写入你的 Shell 配置文件（如 `~/.bashrc` 或 `~/.zshrc`）：
```bash
# 如果使用 zsh (macOS 默认)
echo 'export HF_TOKEN="hf_你的token"' >> ~/.zshrc && source ~/.zshrc

# 如果使用 bash
echo 'export HF_TOKEN="hf_你的token"' >> ~/.bashrc && source ~/.bashrc
```

#### **临时使用（仅当前窗口有效）**
| 环境 | 命令 |
| :--- | :--- |
| **Windows (PS)** | `$env:HF_TOKEN="hf_xxx"` |
| **Windows (CMD)** | `set HF_TOKEN=hf_xxx` |
| **Linux/macOS** | `export HF_TOKEN="hf_xxx"` |

### 3. 安装 Python
跑通版本使用 Anaconda3-2025.12-2-Windows-x86_64，并加入环境变量。

Windows可以手动在系统环境变量里加
```
D:\<anaconda dir>
D:\<anaconda dir>\Scripts
D:\<anaconda dir>\Library\bin
```

注意，由于库的各种依赖问题，强烈建议每个版本和我在这里写的保持一致，不然需要花较多时间处理问题

### 4. 安装 Python 包
说话人分离默认 **pyannote/speaker-diarization-community-1**，调用其 `exclusive_speaker_diarization`。这要求 **whisperx ≥ 3.8** 且 **pyannote.audio ≥ 4**；3.x 的 pyannote 没有该 API，会在转录完成后才崩。请钉死下面跑通版本，不要只写 `pip install whisperx`。

| 包 | 跑通版本 | 最低可用 |
|------|----------|----------|
| whisperx | 3.8.5 | 3.8.0（默认模型改为 community-1，`DiarizationPipeline` 支持 `token` / `cache_dir`） |
| pyannote.audio | 4.0.4 | 4.0.0（`DiarizeOutput.exclusive_speaker_diarization`） |

```bash
# 先安装 torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 cuda版本视自身情况而定
pip install yt-dlp "whisperx==3.8.5" "pyannote.audio==4.0.4"
# 如果是和我一样anaconda，需要额外执行下面命令，不然import transformers会崩溃
pip uninstall numpy
pip install "numpy<2.4"
# 测试transformers
$env:PYTHONFAULTHANDLER = "1"
python -c "import faulthandler; faulthandler.enable(); import transformers"
```
删除 <anaconda dir>/Library/bin/libiomp5md.dlll 避免和torch的omp冲突

启动时 `check_dependencies` 会检查上述版本和 exclusive API，不通过则直接退出，避免先跑完 ASR 再失败。

### 5. 模型存放位置

默认始终使用 **skill 目录下的 `models/`**（已加入 `.gitignore`）。只在这里查找，缺了也下载到这里；**不读取、不搬迁** 你的 `HF_HOME` / `HF_HUB_CACHE` / `TORCH_HOME` / `~/.cache`。首次转录会下载约 **5–8 GB** 权重（Whisper large-v3 约 3GB，其余为对齐和 pyannote community-1 说话人分离）。

门禁模型仍需要环境变量 `HF_TOKEN`（凭证，不是缓存路径）。若权重已经在别的目录，用 `--models-dir` 指过去，或自行拷进 `models/`。

```
models/
  huggingface/hub/   Whisper large-v3、pyannote 说话人分离、中文对齐、标点模型
  torch/hub/         WhisperX 英文对齐（torch.hub）
```

覆盖路径：`python main.py ... --models-dir D:\other\models`

## 可选参数

| 参数 | 作用 | 示例 |
|------|------|------|
| `--names "A,B"` | 自定义说话人名称（按首次出场顺序；片头垫话也会占一个名额） | `--names "主持人,嘉宾"` |
| `--language zh` | 指定音频语言（默认自动检测，不必填） | `--language zh` |
| `--punctuate` | 使用 transformers pipeline 恢复标点 | `xxx --punctuate` |
| `--proofread` | LLM 校对标点和错别字 | `xxx --proofread` |
| `--cleanup` | 删除 wav/srt/json 等临时文件，不删用户输入和成稿 | `xxx --cleanup` |
| `--force` | 忽略已有 SRT，强制重新转录 | 升级流水线后请加此项 |
| `--no-hotwords` | 关闭从标题/简介/标签/章节抽取的热词 | `xxx --no-hotwords` |
| `--hotwords "..."` | 覆盖自动热词（本地文件无标题时用这个） | `--hotwords "SGLang KV Cache"` |
| `--models-dir` | 模型缓存目录 | 默认 skill 目录下 `models/` |
| `--batch-size N` | Whisper 批大小（默认 8，约 8GB 显存） | `--batch-size 4` |
| `--no-exclusive-diarize` | 改用 overlapping 说话人分离 | 默认 exclusive |
| `--work-dir` | 工作目录（默认当前目录） | `--work-dir D:\out` |

运行时默认自动检测语言、自动估计说话人数，无需交互。

## 输出效果

输出到当前目录，文件名：`{视频ID}-转录文本.txt`

```
==================== 语音转录文本 — 共2位说话人 ===================
来源：https://youtu.be/abc
处理时间：2026-05-04

【说话人1】今天我们聊一聊 AI 的未来。
【说话人2】对，这个话题很有意思。
【说话人1】那我们先从大模型开始说起吧。
```

用 `--names "张总,李工"` 后：

```
【张总】Today let's talk about the future of AI.
【李工】Yes, I think this topic is really interesting.
```

## FAQ

| 问题 | 解答 |
|------|------|
| 转录很慢？ | 首次需下载约 5–8 GB 模型到 `models/`（其中 Whisper large-v3 约 3GB），长视频可能 10 分钟+ |
| 说话人识别不准？ | 已默认 pyannote/speaker-diarization-community-1，人数自动估计；效果不佳时可用 `--names` 指定名称 |
| 升级后文本还是旧的？ | 工作目录里已有同名 `.srt` 会跳过转录；加 `--force` 强制重转 |
| 找不到输出文件？ | 搜索 `*-转录文本.txt`，在工作目录下 |
| 提示缺少依赖？ | 按提示安装，Windows 安装后需重启 Claude Code |
| 说话人分离报 `exclusive_speaker_diarization` / `DiarizeOutput`？ | pyannote.audio 仍是 3.x，或 whisperx 低于 3.8。按上面表格安装 `whisperx==3.8.5` 和 `pyannote.audio==4.0.4` |

## 注意事项

- HF Token 是敏感信息，勿提交到公开仓库
- 临时文件默认保留；`--cleanup` 会删 wav/srt/json 等中间文件，不会删你指定的输入文件和 `*-转录文本.txt`
- 仅用于个人学习/研究，请遵守版权规定
