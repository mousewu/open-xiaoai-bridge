#!/usr/bin/env python3
"""
OpenXiaoAI API 基础工具（audio-player skill 共用）
"""

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid


def get_base_url():
    """获取 OpenXiaoAI 服务地址"""
    return os.environ.get("OPENXIAOAI_BASE_URL", "http://127.0.0.1:9092").rstrip("/")


def api_request(path, method="GET", data=None, timeout=30):
    """发送 JSON API 请求"""
    req = urllib.request.Request(
        f"{get_base_url()}{path}",
        headers={"Content-Type": "application/json"},
        method=method,
    )
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}") from e


def interrupt():
    """打断音箱当前播放"""
    return api_request("/api/interrupt", method="POST")


def upload_and_play(file_path, blocking=False, timeout=None):
    """上传音频文件到 /api/play/file 播放

    Args:
        file_path: 本地音频文件路径（mp3/flac/wav/ogg）
        blocking: True 时等待播放完成才返回
        timeout: HTTP 超时秒数，默认阻塞 4 小时 / 非阻塞 120 秒
    """
    boundary = f"----audio-player-{uuid.uuid4().hex}"
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(file_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body += file_data
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"{get_base_url()}/api/play/file?blocking={'true' if blocking else 'false'}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    if timeout is None:
        timeout = 4 * 3600 if blocking else 120
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
