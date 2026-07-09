#!/usr/bin/env python3
"""
YouTube 兜底：本地库没有时，搜索并下载音轨（转 mp3）。

依赖: yt-dlp + ffmpeg（brew install yt-dlp ffmpeg）
下载存放在 $AUDIO_PLAYER_CACHE（默认 ~/Music/YouTube，在本地音频库检索范围内，
下载过的内容之后可直接语音点播）。文件名为"视频标题 [视频ID].mp3"——
标题供 search_local.py 检索，ID 供去重：同一视频重复请求直接复用，不重新下载。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

CACHE_DIR = os.path.expanduser(
    os.environ.get("AUDIO_PLAYER_CACHE", "~/Music/YouTube")
)


def find_cached(vid: str) -> str | None:
    """按视频 ID 在下载目录中查找已存在的文件"""
    if not os.path.isdir(CACHE_DIR):
        return None
    marker = f"[{vid}]"
    for fn in os.listdir(CACHE_DIR):
        if marker in fn and fn.endswith(".mp3"):
            return os.path.join(CACHE_DIR, fn)
    return None


def require_yt_dlp():
    if shutil.which("yt-dlp") is None:
        print(json.dumps(
            {"error": "未安装 yt-dlp，请先执行: brew install yt-dlp ffmpeg"},
            ensure_ascii=False,
        ))
        sys.exit(1)


def cmd_search(query, limit):
    out = subprocess.run(
        [
            "yt-dlp", f"ytsearch{limit}:{query}", "--flat-playlist",
            "--print", "%(id)s\t%(title)s\t%(duration)s\t%(channel)s",
            "--no-warnings",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        print(json.dumps({"error": out.stderr.strip()[-500:]}, ensure_ascii=False))
        sys.exit(1)

    results = []
    for line in out.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            results.append({
                "id": parts[0],
                "title": parts[1],
                "duration_sec": parts[2],
                "channel": parts[3],
            })
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_download(target):
    os.makedirs(CACHE_DIR, exist_ok=True)

    if re.fullmatch(r"[\w\-]{11}", target):
        url = f"https://www.youtube.com/watch?v={target}"
        vid = target
    else:
        url = target
        m = re.search(r"(?:v=|youtu\.be/)([\w\-]{11})", target)
        vid = m.group(1) if m else None

    if vid:
        cached = find_cached(vid)
        if cached:
            print(json.dumps({"path": cached, "cached": True}, ensure_ascii=False))
            return

    out = subprocess.run(
        [
            "yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
            "-o", os.path.join(CACHE_DIR, "%(title)s [%(id)s].%(ext)s"),
            "--print", "after_move:filepath", "--no-simulate", "--no-warnings",
            url,
        ],
        capture_output=True, text=True, timeout=600,
    )
    if out.returncode != 0:
        print(json.dumps({"error": out.stderr.strip()[-500:]}, ensure_ascii=False))
        sys.exit(1)

    lines = out.stdout.strip().splitlines()
    path = lines[-1] if lines else None
    if not path or not os.path.isfile(path):
        print(json.dumps({"error": "下载完成但未找到输出文件"}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"path": path, "cached": False}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="YouTube 音轨搜索与下载")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="搜索视频，输出 JSON 列表")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("--limit", type=int, default=5, help="结果条数（默认 5）")

    p_download = sub.add_parser("download", help="下载音轨为 mp3，输出文件路径 JSON")
    p_download.add_argument("target", help="视频 ID 或完整 URL")

    args = parser.parse_args()
    require_yt_dlp()

    if args.command == "search":
        cmd_search(args.query, args.limit)
    elif args.command == "download":
        cmd_download(args.target)


if __name__ == "__main__":
    main()
