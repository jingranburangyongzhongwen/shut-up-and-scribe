#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从视频标题 / 简介 / 标签 / 章节抽出 faster-whisper hotwords。

只认元数据结构，不针对某一期视频写词表。入列优先嘉宾名和标题/章节里的
拉丁专名（K/V Cache → KV Cache），章节里的中文长片段只填剩余名额。
截断按 token 预算而不是条数：faster-whisper 只会保留 prompt 前约 223 token。
全局按小写去重，被更长短语包含的单词不再单列。
"""

from __future__ import annotations

import html
import re

LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-_.]{1,}")
LATIN_BIGRAM_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+\-_.]{1,})\s+([A-Za-z][A-Za-z0-9+\-_.]{1,})\b"
)
QUOTED_CJK_RE = re.compile(r"[「“\"']([\u4e00-\u9fff]{2,8})[」”\"']")
PAREN_CJK_RE = re.compile(r"[（(]([\u4e00-\u9fff]{2,8})[）)]")
MIXED_TERM_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]{1,4}[\u4e00-\u9fff]{2,6}")
STRIP_QUOTES_RE = re.compile("[\u201c\u201d\u2018\u2019「」『』\"']")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
CJK_NAME_RE = re.compile(r"^([\u4e00-\u9fff]{2,4})")
GUEST_START_RE = re.compile(r"(采访)?嘉宾\s*[：:]")
GUEST_END_RE = re.compile(r"^(时间轴|章节|目录|订阅|关注|简介)\b")
SLASH_LATIN_RE = re.compile(r"\b([A-Za-z])/(?=[A-Za-z])")
CHAPTER_SPLIT_RE = re.compile(r"[：:｜|]")
CJK_LATIN_CONJ_RE = re.compile(r"(?<=[\u4e00-\u9fff])[与和及](?=[A-Za-z])")
DOMAINISH_RE = re.compile(
    r"(?i)(https?://|www\.|\.(com|net|org|io|tv|cc|cn|html|htm)$)"
)

LATIN_STOP = {
    "https", "http", "www", "com", "net", "org", "html", "amp", "quot",
    "the", "and", "for", "with", "from", "this", "that", "video", "watch",
    "member", "technical", "staff", "of", "to", "in", "on", "all", "you",
    "your", "our", "is", "are", "be", "at", "by", "or", "an", "as", "it",
    "see", "click", "here", "more", "about", "please", "link",
}
CJK_STOP = {
    "怎么", "我们", "这期", "视频", "以及", "还有", "一个", "什么", "可以",
}
TAG_STOP = {"科技", "生活", "娱乐", "知识", "搞笑", "游戏", "时尚", "美食", "音乐"}
SHORT_OK = {"ai", "rl", "kv", "ml", "pd", "ui"}
MAX_TERM_LEN = 32
# large-v3 的 decoder max_length=448，hotwords 超过 max_length//2-1（223）会被后端截掉。
MAX_HOTWORD_TOKENS = 200


def _collapse_slash_latin(text: str) -> str:
    return SLASH_LATIN_RE.sub(r"\1", text or "")


def _ok_latin(term: str) -> bool:
    if term.lower() in LATIN_STOP:
        return False
    if term.isascii() and term.isalpha() and len(term) <= 2:
        if term.lower() in SHORT_OK:
            return True
        # 允许 Xu / Li 这类姓
        return len(term) == 2 and term[0].isupper() and term[1].islower()
    return True


def _chapter_parts(title: str) -> list[str]:
    title = html.unescape(title or "")
    title = STRIP_QUOTES_RE.sub("", title)
    parts = []
    for chunk in CHAPTER_SPLIT_RE.split(title):
        chunk = chunk.strip()
        if not chunk:
            continue
        for piece in CJK_LATIN_CONJ_RE.split(chunk):
            piece = piece.strip()
            if not piece:
                continue
            if re.search(r"[\u4e00-\u9fff]\s*/\s*[\u4e00-\u9fff]", piece):
                parts.extend(p.strip() for p in piece.split("/") if p.strip())
            else:
                parts.append(_collapse_slash_latin(piece))
    return parts


def _guest_line_terms(line: str) -> list[str]:
    out = []
    m = CJK_NAME_RE.match(line)
    if m:
        out.append(m.group(1))
    out.extend(LATIN_RE.findall(line))
    return out


def _guest_terms(description: str) -> list[str]:
    out = []
    in_guests = False
    for raw in description.splitlines():
        line = raw.strip()
        start = GUEST_START_RE.search(line)
        if start:
            in_guests = True
            rest = line[start.end():].strip()
            if rest:
                out.extend(_guest_line_terms(rest))
            continue
        if not in_guests:
            continue
        if not line:
            continue
        if GUEST_END_RE.search(line) or line.startswith("http"):
            break
        out.extend(_guest_line_terms(line))
    return out


def _drop_subsumed(terms: list[str]) -> list[str]:
    """有「KV Cache」就不再单列 KV / Cache。"""
    token_sets = [t.lower().split() for t in terms]
    keep = []
    for i, term in enumerate(terms):
        toks = token_sets[i]
        if len(toks) != 1:
            keep.append(term)
            continue
        word = toks[0]
        subsumed = False
        for j, other in enumerate(token_sets):
            if i == j:
                continue
            if len(other) >= 2 and word in other:
                subsumed = True
                break
            other_s = terms[j].lower()
            if other_s != word and other_s.startswith(word) and term.isascii():
                subsumed = True
                break
        if not subsumed:
            keep.append(term)
    return keep


def _approx_tokens(term: str) -> int:
    """粗估 Whisper BPE：汉字 1 字 ≈ 1 token，拉丁按约 4 字符 1 token。"""
    n = sum(1 for ch in term if "\u4e00" <= ch <= "\u9fff")
    for tok in LATIN_RE.findall(term):
        n += max(1, (len(tok) + 3) // 4)
    return max(1, n)


def _fit_token_budget(terms: list[str], max_tokens: int) -> list[str]:
    out = []
    used = 0
    for term in terms:
        cost = _approx_tokens(term)
        if used + cost > max_tokens:
            break
        out.append(term)
        used += cost
    return out


def extract_hotwords(
    title: str = "",
    description: str = "",
    tags: list | None = None,
    chapters: list | None = None,
    max_terms: int | None = None,
    max_tokens: int = MAX_HOTWORD_TOKENS,
) -> str:
    """返回空格分隔的 hint 短语，供 faster-whisper `hotwords` 使用。

    顺序：嘉宾名 → 标题/章节专名（含引号中文）→ 标签 → 简介拉丁词 → 中文长片段。
    默认按约 200 token 截断（模型硬上限约 223），不按条数 40 截。
    """
    tags = tags or []
    chapters = chapters or []
    title = html.unescape(title or "")
    description = html.unescape(description or "")
    chapter_titles = []
    for ch in chapters:
        if isinstance(ch, dict):
            chapter_titles.append(str(ch.get("title") or ""))
        else:
            chapter_titles.append(str(ch or ""))

    guests: list[str] = []
    proper: list[str] = []
    tag_terms: list[str] = []
    desc_latin: list[str] = []
    cjk_fill: list[str] = []
    seen_l: set[str] = set()

    def add(term: str, bucket: list[str]):
        term = (term or "").strip().strip(".,;:|/\\|")
        term = re.sub(r"\s+", " ", term)
        if not term or len(term) < 2 or len(term) > MAX_TERM_LEN:
            return
        key = term.lower()
        if key in seen_l or key in LATIN_STOP or term in CJK_STOP:
            return
        if key.startswith(("http://", "https://", "www.")):
            return
        if DOMAINISH_RE.search(term):
            return
        if term.isascii() and not _ok_latin(term):
            return
        seen_l.add(key)
        bucket.append(term)

    def add_quotes_and_parens(text: str, bucket: list[str]):
        for m in QUOTED_CJK_RE.findall(text):
            add(m, bucket)
        for m in PAREN_CJK_RE.findall(text):
            add(m, bucket)

    def add_latin(text: str, bucket: list[str], bigrams: bool):
        text = _collapse_slash_latin(text)
        if bigrams:
            for a, b in LATIN_BIGRAM_RE.findall(text):
                if _ok_latin(a) and _ok_latin(b):
                    add(f"{a} {b}", bucket)
        for tok in LATIN_RE.findall(text):
            add(tok, bucket)

    def add_mixed(text: str, bucket: list[str]):
        for m in MIXED_TERM_RE.findall(text):
            add(m, bucket)

    def add_cjk_runs(text: str, bucket: list[str]):
        for run in CJK_RUN_RE.findall(text):
            run = re.sub(r"[的了呢吗]$", "", run)
            if not (4 <= len(run) <= 8):
                continue
            if any(ch in run for ch in "的了呢吗"):
                continue
            if any(run.startswith(s) for s in CJK_STOP):
                continue
            add(run, bucket)

    for g in _guest_terms(description):
        add(g, guests)

    add_quotes_and_parens(title, proper)
    add_latin(title, proper, bigrams=True)
    add_mixed(title, proper)

    for raw in chapter_titles:
        for part in _chapter_parts(raw):
            add_latin(part, proper, bigrams=True)
            add_mixed(part, proper)
            compact = re.sub(r"\s+", "", part)
            if part.isascii() and 2 <= len(compact) <= 24:
                add(part, proper)
            add_cjk_runs(part, cjk_fill)

    add_cjk_runs(title, cjk_fill)

    for tag in tags:
        tag = html.unescape(str(tag or "")).strip()
        compact = re.sub(r"\s+", "", tag)
        if tag in TAG_STOP or compact in TAG_STOP:
            continue
        if len(LATIN_RE.findall(tag)) <= 1 and 2 <= len(compact) <= 16:
            add(tag, tag_terms)
        add_latin(tag, tag_terms, bigrams=False)

    add_quotes_and_parens(description, proper)
    add_latin(description, desc_latin, bigrams=False)

    ordered = _drop_subsumed(guests + proper + tag_terms + desc_latin + cjk_fill)
    if max_terms is not None:
        ordered = ordered[:max_terms]
    return " ".join(_fit_token_budget(ordered, max_tokens))
