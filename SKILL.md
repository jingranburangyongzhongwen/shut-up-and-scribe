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
| `--names "A,B"` | 自定义说话人名称（逗号分隔） | 自动判定 |
| `--language zh` | 指定音频语言（zh/en/ja 等），运行时会自动询问 | 自动检测 |
| `--punctuate` | 使用 transformers pipeline 恢复标点（oliverguhr/fullstop-punctuation-multilang-large） | 关闭 |
| `--proofread` | 启用 LLM 校对（标点修正、错别字修正） | 关闭 |
| `--cleanup` | 清理临时音频和SRT文件 | 关闭（默认保留） |

## 环境要求与安装指引
> **详细安装步骤和已验证的依赖版本（含踩坑记录，如 numpy<2.4、transformers 崩溃、torchcodec warning 等）见同目录 README.md。**
> **如果环境检查失败，请先读取 README.md 获取对应内容，与错误信息一同展示给用户。**

## 执行步骤

### 0. 环境依赖检查

脚本内置依赖检查（`main.py:check_dependencies`），缺失依赖时会直接报错并提示安装命令。无需额外执行 bash 检查脚本。

> **AI 行为指令**
> 若脚本因依赖缺失退出，请先读取 README.md（同目录下）获取详细的安装步骤和已踩坑记录，然后将 README.md 中的相关内容及错误信息完整展示给用户，不要再执行后续步骤。

### 1. 选择语言（必须先执行）

> **AI 行为指令**
> 按以下优先级确定语言，**不要**盲目询问：
>
> 1. 用户消息中已明确指定 `--language zh/en/ja` 等参数 → 直接使用
> 2. 用户消息中包含「中文/国语/普通话」→ 直接用 `zh`，跳过询问
> 3. 用户消息中包含「英文/英语/English」→ 直接用 `en`，跳过询问
> 4. 用户消息中包含「日文/日语/Japanese」→ 直接用 `ja`，跳过询问
> 5. 以上都不匹配时，使用 AskUserQuestion 询问

询问示例：
```
问题：请选择音频的主要语言
选项：
- A. 中文（默认，中文视频推荐）
- B. 英文（英文视频选择）
- C. 自动检测（中英混合视频选择，中文可能逐字加空格）
```

将用户选择映射为 `LANGUAGE` 参数：
- A. 中文 → `zh`
- B. 英文 → `en`
- C. 自动检测 → 留空

### 2. 解析输入与参数
从用户消息中提取：
- `INPUT`：YouTube URL 或本地文件路径（支持 `@文件名` 格式，去掉 `@` 前缀）
- `NAMES`：从 `--names "A,B"` 提取，未提供则留空（自动判定）
- `LANGUAGE`：从上一步选择结果获取
- `PUNCTUATE`：满足以下任一条件则启用（默认关闭）：
  - 用户消息包含 `--punctuate` 参数
  - 用户消息包含「恢复标点/加标点/标点符号/标点」等关键词
- `PROOFREAD`：满足以下任一条件则启用（默认关闭）：
  - 用户消息包含 `--proofread` 参数
  - 用户消息包含「校对/润色/修正错别字」等关键词
- `CLEANUP`：从 `--cleanup` 提取，存在则表示清理临时文件（默认关闭）

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
  ${CLEANUP:+--cleanup}
```

- **禁止**使用 `2>/dev/null`（会吞掉关键错误信息）
- **禁止**使用 `|| python3` 的 fallback 逻辑（应直接报错而非静默失败）
- 使用 `python` 命令，`main.py` 内部已处理 python/python3 兼容

脚本内部执行：
1. 下载/转换音频（YouTube → WAV 或本地文件转 WAV）
2. WhisperX 转录（large-v3 模型，自动检测语言与说话人）
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
