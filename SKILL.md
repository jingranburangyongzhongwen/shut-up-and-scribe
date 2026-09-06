---
name: shut-up-and-scribe
description: 从 YouTube/B站视频或本地音频文件，自动下载音频→WhisperX转录+说话人分离→自定义说话人名称→合并同说话人段落，生成最终整理文件
triggers:
  - pattern: /shut-up-and-scribe
  - pattern: 整理访谈
  - pattern: 处理字幕
---

# 访谈字幕自动整理 Skill（WhisperX 版）

## 使用方式
用户在消息中提供 **YouTube URL** 或 **本地文件路径**，并可附带参数：
- `/shut-up-and-scribe https://www.youtube.com/watch?v=XXXXXXXX`
- `/shut-up-and-scribe https://youtu.be/ZDXIUO7CRzo`
- `/shut-up-and-scribe https://www.youtube.com/shorts/XXXXXXXX`
- `/shut-up-and-scribe https://www.bilibili.com/video/BVxxxxxxxx`
- `/shut-up-and-scribe https://b23.tv/xxxx`
- `/shut-up-and-scribe luofili.txt`（兼容旧模式，直接处理本地字幕）
- `/shut-up-and-scribe video.mp4 --names "小珺,江泽元"`
- `/shut-up-and-scribe https://youtu.be/xxx --proofread`（启用 LLM 校对）
- `/shut-up-and-scribe https://youtu.be/xxx --cleanup`（清理临时文件）

## 支持参数
| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--names "A,B"` | 自定义说话人名称（逗号分隔，按首次出场顺序；片头垫话也会占一个名额） | 自动判定（按出场顺序标【说话人N】） |
| `--language zh` | 指定音频语言（zh/en/ja 等） | 自动检测，无需询问 |
| `--punctuate` | 使用 transformers pipeline 恢复标点（oliverguhr/fullstop-punctuation-multilang-large） | 关闭 |
| `--proofread` | 启用 LLM 校对（标点修正、错别字修正） | 关闭 |
| `--cleanup` | 清理 wav/srt/json 等临时文件（不删用户输入和成稿） | 关闭（默认保留） |
| `--models-dir` | 模型缓存目录（只在该路径查找/下载，不读 HF_HOME） | skill 目录下 `models/` |
| `--force` | 忽略已有 SRT，强制重转 | 关闭 |
| `--no-hotwords` | 关闭自动热词 | 关闭（默认从标题/简介/标签/章节抽取） |
| `--hotwords "..."` | 覆盖自动热词（本地文件无标题时用） | 不传（URL 走自动抽取） |

## 环境要求与安装指引
> **详细安装步骤和已验证的依赖版本（含踩坑记录，如 numpy<2.4、transformers 崩溃、torchcodec warning 等）见同目录 README.md。**
> **如果环境检查失败，请先读取 README.md 获取对应内容，与错误信息一同展示给用户。**

## 执行步骤

### 0. 环境依赖检查

脚本内置依赖检查（`main.py:check_dependencies`），缺失依赖时会直接报错并提示安装命令。无需额外执行 bash 检查脚本。

> **AI 行为指令**
> 若脚本因依赖缺失退出，请先读取 README.md（同目录下）获取详细的安装步骤和已踩坑记录，然后将 README.md 中的相关内容及错误信息完整展示给用户，不要再执行后续步骤。

### 1. 语言（默认自动检测，不要询问）

> **AI 行为指令**
> **不要**为了语言去问用户。按以下优先级：
>
> 1. 用户消息中已明确指定 `--language zh/en/ja` → 原样传给脚本
> 2. 用户消息明确要求指定语言（如「用中文转」）→ 加上对应 `--language`
> 3. 其它情况一律**不传** `--language`，由 WhisperX 自动检测
>
> 说话人数量同样**不要询问、不要传** `--min-speakers` / `--max-speakers`。需要人名时仅当用户已给出 `--names` 或明确点名时才传。
> 热词从标题/简介/标签/**章节**自动抽取，**不要询问**。用户明确说「不要热词」时才加 `--no-hotwords`。用户写了 `--hotwords "..."` 或明确给出要提示的专名列表时，才传 `--hotwords`（会覆盖自动抽取）。
> LLM 校对保持默认关闭；仅当用户写了 `--proofread` 或明确要求校对/润色时才开。

### 2. 解析输入与参数
从用户消息中提取：
- `INPUT`：YouTube URL 或本地文件路径（支持 `@文件名` 格式，去掉 `@` 前缀）
- `NAMES`：仅当用户写了 `--names "A,B"` 或明确给出说话人姓名时提取，否则留空
- `LANGUAGE`：仅当用户明确指定时提取，否则留空（自动检测）
- `PUNCTUATE`：满足以下任一条件则启用（默认关闭）：
  - 用户消息包含 `--punctuate` 参数
  - 用户消息包含「恢复标点/加标点/标点符号/标点」等关键词
- `PROOFREAD`：满足以下任一条件则启用（默认关闭）：
  - 用户消息包含 `--proofread` 参数
  - 用户消息包含「校对/润色/修正错别字」等关键词
- `CLEANUP`：从 `--cleanup` 提取，存在则表示清理临时文件（默认保留）
- `FORCE`：满足以下任一条件则启用（默认关闭）：
  - 用户消息包含 `--force`
  - 用户消息包含「重新转录/不要缓存/强制重转」等关键词
- `NO_HOTWORDS`：满足以下任一条件则启用（默认关闭，走自动热词）：
  - 用户消息包含 `--no-hotwords`
  - 用户明确说「不要热词/关闭热词」
- `HOTWORDS`：仅当用户写了 `--hotwords "..."` 或明确给出要提示的专名列表时提取，否则留空（URL 自动抽；与 `NO_HOTWORDS` 同时出现则不传）
- `MODELS_DIR`：仅当用户写了 `--models-dir` 时提取，否则留空（用 skill 下 `models/`）

---

### 3. 运行主脚本（一次 Bash 权限）

使用系统注入的 skill Base directory（路径中的 `\` 改为 `/`），直接执行：

```bash
python "<Base directory>/main.py" \
  "$INPUT" \
  ${NAMES:+--names "$NAMES"} \
  ${LANGUAGE:+--language "$LANGUAGE"} \
  ${PUNCTUATE:+--punctuate} \
  ${PROOFREAD:+--proofread} \
  ${CLEANUP:+--cleanup} \
  ${FORCE:+--force} \
  ${NO_HOTWORDS:+--no-hotwords} \
  ${HOTWORDS:+--hotwords "$HOTWORDS"} \
  ${MODELS_DIR:+--models-dir "$MODELS_DIR"}
```

- **禁止**使用 `2>/dev/null`（会吞掉关键错误信息）
- **禁止**使用 `|| python3` 的 fallback 逻辑（应直接报错而非静默失败）
- 使用 `python` 命令，`main.py` 内部已处理 python/python3 兼容

脚本内部执行：
1. 下载/转换音频（YouTube → WAV 或本地文件转 WAV）
2. WhisperX 转录（large-v3 + pyannote/speaker-diarization-community-1；语言和人数均自动）
3. SRT → 整理文本（合并同说话人段落，分配【说话人X】标签）
4. 清理临时文件并报告结果

输出文件将生成在**当前工作目录**，文件名格式：`{视频ID}-转录文本.txt` 或 `{文件名}-转录文本.txt`

---

### 4. LLM 校对（仅在 `--proofread` 时执行）

检查脚本输出中是否包含 `__PROOFREAD_REQUIRED__:` 行（格式：`__PROOFREAD_REQUIRED__:/path/to/file.txt`）。如果存在：

1. 提取文件路径
2. 用 **Agent 工具** 启动子 agent 执行校对，prompt 如下：

```
你是专业文字校对助手。请读取 {output_file}，完成：
1. 智能标点：判断主要语言（中文/英文），补充/修正标点符号
2. 修正错别字：修正同音错别字、拼音错误，保持原意
3. 保留标签：严格保留所有【说话人X】标签
4. 不添加原文没有的信息
5. 用 Write 工具将校对后的内容写回 {output_file}
请开始校对。
```

3. 子 agent 完成后，向用户报告校对完成

---

## 输出格式示例

**自动检测，2 人**
```
==================== 语音转录文本 — 共2位说话人 ===================
来源：https://youtu.be/abc
处理时间：2026-05-04

【说话人1】今天我们聊一聊 AI 的未来。
【说话人2】对，这个话题很有意思。
【说话人1】那我们先从大模型开始说起吧。
```

**指定名字**
```
【张总】Today let's talk about the future of AI.
【李工】Yes, I think this topic is really interesting.
```

**单人演讲**
```
==================== 语音转录文本 — 单人 ===================
今天我想和大家分享的主题是……
```
