#!/usr/bin/env python3
"""
在本地音频库中模糊检索音频文件。

匹配目标是文件相对路径（含目录名，目录通常是节目/歌手/季），支持：
  - 中英文子串匹配（不区分大小写，括号/分隔符归一化）
  - 拼音全拼与首字母匹配（安装 pypinyin 时生效，可容忍 ASR 同音字错误）

输出 JSON 数组（按匹配分降序），供 Agent 从中挑选。
"""

import argparse
import json
import os
import re
import sys

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg"}  # bridge Rust 解码器支持的格式

try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None

# 括号、标点统一视为分隔符（文件名常见【noise】后缀，内容保留但不粘连）
_SEPARATORS = re.compile(r"[\s_\-.,，'’!！?？&【】\[\]()（）]+")


def normalize(text):
    return _SEPARATORS.sub(" ", text.lower()).strip()


def to_pinyin(text):
    """返回 (全拼, 首字母)，无 pypinyin 时返回 (None, None)"""
    if lazy_pinyin is None:
        return None, None
    syllables = lazy_pinyin(text)
    full = "".join(syllables)
    initials = "".join(s[0] for s in syllables if s)
    return full, initials


def scan_library(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                yield os.path.join(dirpath, fn)


def build_query_tokens(query):
    """预计算每个查询词的原文和拼音形式"""
    tokens = []
    for token in normalize(query).split():
        full, initials = to_pinyin(token)
        tokens.append((token, full, initials))
    return tokens


def score_entry(name_norm, name_pinyin, name_initials, tokens):
    total = 0
    for token, t_full, t_init in tokens:
        if token in name_norm:
            total += 100
        elif t_full and name_pinyin and t_full in name_pinyin:
            total += 60  # 同音字：拼音全拼命中
        elif t_init and name_initials and len(t_init) >= 2 and t_init in name_initials:
            total += 30  # 拼音首字母命中
    return total


def main():
    parser = argparse.ArgumentParser(description="本地音频库检索")
    parser.add_argument("query", help="检索关键词，空格分隔多个词（如 'yakka dee tiger'）")
    parser.add_argument(
        "--library",
        default=os.environ.get("MUSIC_LIBRARY_DIR", "/Volumes/music"),
        help="音频库根目录（默认 $MUSIC_LIBRARY_DIR 或 /Volumes/music）",
    )
    parser.add_argument("--limit", type=int, default=10, help="最多返回条数（默认 10）")
    args = parser.parse_args()

    root = os.path.expanduser(args.library)
    if not os.path.isdir(root):
        print(json.dumps({"error": f"音频库目录不存在: {root}"}, ensure_ascii=False))
        sys.exit(1)

    tokens = build_query_tokens(args.query)
    if not tokens:
        print(json.dumps({"error": "检索关键词为空"}, ensure_ascii=False))
        sys.exit(1)

    results = []
    for path in scan_library(root):
        rel = os.path.relpath(path, root)
        name_norm = normalize(os.path.splitext(rel)[0].replace(os.sep, " "))
        name_pinyin, name_initials = to_pinyin(name_norm.replace(" ", ""))
        score = score_entry(name_norm, name_pinyin, name_initials, tokens)
        if score > 0:
            results.append({"path": path, "name": rel, "score": score})

    results.sort(key=lambda r: (-r["score"], r["name"]))
    print(json.dumps(results[: args.limit], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
