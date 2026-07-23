"""Doubao SAUC streaming ASR provider (大模型流式语音识别).

支持两套入口（协议完全相同，仅 URL 与鉴权不同）：
  - 方舟 Agent Plan:  wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async
      鉴权: X-Api-Key（专属 API Key），走套餐 AFP 抵扣
  - 火山豆包语音:      wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
      鉴权: 新版控制台 X-Api-Key，或旧版 X-Api-App-Key + X-Api-Access-Key

输入约定与其他 ASR provider 一致：raw PCM int16 mono bytes 进，识别文本出。
当前实现为"整句识别"：VAD 判定说完后，将完整录音快速分包推送并取最终结果。
协议为 WebSocket 二进制帧（header + gzip payload），见火山 SAUC API 文档。
"""

import gzip
import json
import struct
import time
import uuid
from typing import Any

from core.utils.logger import logger

# ---- 二进制协议常量 ----
PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001  # x4 bytes

MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_AUDIO_ONLY_REQUEST = 0b0010
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_SERVER_ERROR = 0b1111

FLAG_POS_SEQUENCE = 0b0001
FLAG_NEG_WITH_SEQUENCE = 0b0011

SERIALIZATION_JSON = 0b0001
COMPRESSION_GZIP = 0b0001


def _header(message_type: int, flags: int) -> bytes:
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP,
        0x00,
    ])


def _full_client_request(seq: int, payload: dict) -> bytes:
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    return (
        _header(MSG_FULL_CLIENT_REQUEST, FLAG_POS_SEQUENCE)
        + struct.pack(">i", seq)
        + struct.pack(">I", len(body))
        + body
    )


def _audio_only_request(seq: int, segment: bytes, is_last: bool) -> bytes:
    flags = FLAG_NEG_WITH_SEQUENCE if is_last else FLAG_POS_SEQUENCE
    if is_last:
        seq = -seq
    body = gzip.compress(segment)
    return (
        _header(MSG_AUDIO_ONLY_REQUEST, flags)
        + struct.pack(">i", seq)
        + struct.pack(">I", len(body))
        + body
    )


class _SaucResponse:
    def __init__(self):
        self.code = 0
        self.is_last_package = False
        self.payload_msg: dict | None = None


def _parse_response(msg: bytes) -> _SaucResponse:
    resp = _SaucResponse()
    header_size = msg[0] & 0x0F
    message_type = msg[1] >> 4
    flags = msg[1] & 0x0F
    compression = msg[2] & 0x0F

    payload = msg[header_size * 4:]
    if flags & 0x01:
        payload = payload[4:]  # sequence number
    if flags & 0x02:
        resp.is_last_package = True

    if message_type == MSG_FULL_SERVER_RESPONSE:
        payload = payload[4:]  # payload size
    elif message_type == MSG_SERVER_ERROR:
        resp.code = struct.unpack(">i", payload[:4])[0]
        payload = payload[8:]  # error code + size

    if not payload:
        return resp
    if compression == COMPRESSION_GZIP:
        payload = gzip.decompress(payload)
    try:
        resp.payload_msg = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        pass
    return resp


class _SaucASR:
    """SAUC 流式识别客户端（整句模式）。"""

    def _cfg(self, key: str, default: Any = None) -> Any:
        from core.utils.config import ConfigManager

        return ConfigManager.instance().get_app_config(f"asr.sauc.{key}", default)

    def _headers(self) -> dict[str, str]:
        request_id = str(uuid.uuid4())
        resource_id = str(self._cfg("resource_id", "volc.seedasr.sauc.duration")).strip()
        headers = {
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        api_key = str(self._cfg("api_key", "")).strip()
        if api_key:
            headers["X-Api-Key"] = api_key
            return headers
        # 旧版鉴权回退
        app_key = str(self._cfg("app_key", "")).strip()
        access_key = str(self._cfg("access_key", "")).strip()
        if not app_key or not access_key:
            raise ValueError("Missing asr.sauc.api_key (or app_key + access_key)")
        headers["X-Api-App-Key"] = app_key
        headers["X-Api-Access-Key"] = access_key
        return headers

    def _request_payload(self, sample_rate: int) -> dict:
        payload = {
            "user": {"uid": "open-xiaoai-bridge"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": bool(self._cfg("enable_itn", True)),
                "enable_punc": bool(self._cfg("enable_punc", True)),
                "enable_ddc": bool(self._cfg("enable_ddc", False)),
            },
        }
        # 热词直传：提高专有名词（节目名、人名等）识别率。
        # 双向流式接口限约 100 token，配置过多时截取前 20 个
        hotwords = self._cfg("hotwords", []) or []
        if hotwords:
            context = json.dumps(
                {"hotwords": [{"word": str(w)} for w in hotwords[:20]]},
                ensure_ascii=False,
            )
            payload["request"]["corpus"] = {"context": context}
        return payload

    def asr(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """识别一段完整 PCM 音频，返回文本（失败返回空字符串）。"""
        if not pcm_bytes:
            return ""

        from websockets.sync.client import connect

        url = str(self._cfg("url", "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async")).strip()
        timeout = float(self._cfg("timeout", 10))
        segment_ms = int(self._cfg("segment_ms", 200))
        segment_bytes = max(1, sample_rate * 2 * segment_ms // 1000)

        started = time.monotonic()
        try:
            ws = connect(
                url,
                additional_headers=self._headers(),
                open_timeout=timeout,
                close_timeout=2,
                max_size=10 * 1024 * 1024,
            )
        except Exception as e:
            logger.error(f"[SaucASR] Connect failed: {e}")
            return ""

        logid = ""
        try:
            logid = ws.response.headers.get("X-Tt-Logid", "")
        except Exception:
            pass

        try:
            seq = 1
            ws.send(_full_client_request(seq, self._request_payload(sample_rate)))

            # 快速推送整段音频（不按实时节奏等待）
            segments = [
                pcm_bytes[i:i + segment_bytes]
                for i in range(0, len(pcm_bytes), segment_bytes)
            ]
            for i, segment in enumerate(segments):
                seq += 1
                ws.send(_audio_only_request(seq, segment, is_last=(i == len(segments) - 1)))

            # 等待最终结果（is_last_package）
            text = ""
            deadline = started + timeout
            while time.monotonic() < deadline:
                raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                if isinstance(raw, str):
                    continue
                resp = _parse_response(raw)
                if resp.code != 0:
                    logger.error(
                        f"[SaucASR] Server error code={resp.code}, "
                        f"msg={resp.payload_msg}, logid={logid}"
                    )
                    return ""
                if resp.payload_msg:
                    result = resp.payload_msg.get("result") or {}
                    if isinstance(result, dict) and result.get("text"):
                        text = result["text"]
                if resp.is_last_package:
                    break

            elapsed_ms = (time.monotonic() - started) * 1000
            audio_ms = len(pcm_bytes) * 1000 // (sample_rate * 2)
            logger.info(
                f"[SaucASR] Recognized {audio_ms}ms audio in {elapsed_ms:.0f}ms: "
                f"{text!r} (logid={logid})"
            )
            return text.strip()
        except TimeoutError:
            logger.error(f"[SaucASR] Timed out waiting for result (logid={logid})")
            return ""
        except Exception as e:
            logger.error(f"[SaucASR] Recognition failed: {e} (logid={logid})")
            return ""
        finally:
            try:
                ws.close()
            except Exception:
                pass


SaucASR = _SaucASR()
