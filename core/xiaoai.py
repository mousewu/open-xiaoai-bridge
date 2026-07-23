import asyncio
import threading
import time

import numpy as np
import open_xiaoai_server

from core.ref import get_speaker, set_xiaoai
from core.services.audio.stream import GlobalStream
from core.services.speaker import SpeakerManager
from core.wakeup_session import EventManager
from core.xiaoai_conversation import XiaoAIConversationController
from core.utils.base import json_decode
from core.utils.config import ConfigManager
from core.utils.logger import logger

ASCII_BANNER = """
▄▖      ▖▖▘    ▄▖▄▖
▌▌▛▌█▌▛▌▚▘▌▀▌▛▌▌▌▐ 
▙▌▙▌▙▖▌▌▌▌▌█▌▙▌▛▌▟▖
  ▌                
                                                                                                                
"""


class XiaoAI:
    speaker = SpeakerManager()
    async_loop: asyncio.AbstractEventLoop = None
    config_manager = ConfigManager.instance()
    conversation = XiaoAIConversationController()
    _input_gain_enabled = False
    _input_gain = 1.0
    _async_loop_ready = threading.Event()
    _external_wakeup_keywords: set[str] = set()
    _suppressed_dialog_ids: set[str] = set()
    _suppressed_dialog_last_attempt: dict[str, float] = {}
    _MAX_SUPPRESSED_DIALOGS = 100
    _SUPPRESS_RETRY_INTERVAL = 0.35

    @classmethod
    def refresh_runtime_config(cls, *_args):
        """从配置中心同步运行时参数。"""
        cls.conversation.apply_runtime_config(
            cls.config_manager.get_app_config("xiaoai", {})
        )
        gain = cls.config_manager.get_app_config("audio_input.gain", 1.0)
        try:
            gain = float(gain)
        except (TypeError, ValueError):
            gain = 1.0
        cls._input_gain = max(1.0, min(gain, 8.0))
        cls._input_gain_enabled = cls._input_gain > 1.0
        wakeup_keywords = cls.config_manager.get_app_config("wakeup.keywords", [])
        cls._external_wakeup_keywords = {
            cls._normalize_text(keyword)
            for keyword in wakeup_keywords
            if isinstance(keyword, str) and cls._normalize_text(keyword)
        }

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            return ""
        return "".join(text.strip().lower().split())

    @classmethod
    def _is_external_wakeup_text(cls, text: str) -> bool:
        normalized = cls._normalize_text(text)
        return bool(normalized) and normalized in cls._external_wakeup_keywords

    @classmethod
    async def _suppress_dialog(cls, dialog_id: str, reason: str):
        if not dialog_id:
            return

        # Prevent unbounded growth: if too many stale dialog_ids accumulated
        # (missed Dialog.Finish events), clear them all before adding new one
        if len(cls._suppressed_dialog_ids) >= cls._MAX_SUPPRESSED_DIALOGS:
            logger.debug(
                f"[XiaoAI] Clearing {len(cls._suppressed_dialog_ids)} stale suppressed dialog_ids"
            )
            cls._suppressed_dialog_ids.clear()
            cls._suppressed_dialog_last_attempt.clear()

        is_new_dialog = dialog_id not in cls._suppressed_dialog_ids
        cls._suppressed_dialog_ids.add(dialog_id)

        if is_new_dialog:
            logger.info(
                f"[XiaoAI] 🛑 停止小爱当前对话: {reason}"
            )

        now = time.monotonic()
        last_attempt = cls._suppressed_dialog_last_attempt.get(dialog_id, 0.0)
        if not is_new_dialog and (now - last_attempt) < cls._SUPPRESS_RETRY_INTERVAL:
            return

        cls._suppressed_dialog_last_attempt[dialog_id] = now
        try:
            await cls.speaker.run_shell(
                "killall tts_play.sh miplayer 2>/dev/null; mphelper pause"
            )
            if is_new_dialog:
                await cls.speaker.wake_up(awake=False)
        except Exception as exc:
            logger.debug(
                f"[XiaoAI] Failed to pause suppressed dialog {dialog_id}: {exc}"
            )

    # ---- 音频输入断流看门狗 ----
    # 已知故障模式两种：
    #   1. 服务/音箱重启后录音通道未恢复推流 → 重启录音通道可救
    #   2. 客户端僵死（TCP 未断但停止一切响应）→ 录音重启指令石沉大海，
    #      且僵尸连接占据单连接服务器的坑位，音箱重连被拒之门外
    #      → 需强制断开连接，让客户端重连（实测重连在 1s 内完成）
    _last_input_at: float = 0.0
    _watchdog_last_restart: float = 0.0
    _watchdog_failures: int = 0  # 连续无回执的救援次数
    WATCHDOG_CHECK_INTERVAL = 30  # 检查周期（秒）
    WATCHDOG_SILENCE_THRESHOLD = 120  # 判定断流的静默时长（秒），需大于对话 TTS 的最长关麦窗口
    WATCHDOG_RESTART_COOLDOWN = 300  # 两次自动重启的最小间隔（秒），避免音箱离线时刷屏
    WATCHDOG_RETRY_COOLDOWN = 60  # 已有失败记录时缩短重试间隔，加速僵尸判定
    WATCHDOG_RPC_TIMEOUT = 15  # 等待救援指令回执的超时（秒），Rust 侧 RPC 自身 10s 超时
    WATCHDOG_MAX_FAILURES = 2  # 连续无回执达到该次数 → 判定连接僵死，强制断开

    @classmethod
    def on_input_data(cls, data: bytes):
        cls._last_input_at = time.monotonic()
        audio_array = np.frombuffer(data, dtype=np.int16)
        if cls._input_gain_enabled and audio_array.size > 0:
            boosted = audio_array.astype(np.float32) * cls._input_gain
            audio_array = np.clip(boosted, -32768, 32767).astype(np.int16)
        GlobalStream.input(audio_array.tobytes())

    @classmethod
    def _audio_watchdog_tick(cls):
        now = time.monotonic()
        silence = now - cls._last_input_at
        if silence < cls.WATCHDOG_SILENCE_THRESHOLD:
            return

        # 连续对话进行中的静默是有意关麦（TTS 播放期间），不干预
        for controller in (
            EventManager._openclaw_controller,
            EventManager._openai_controller,
        ):
            if controller and controller.is_active():
                return

        cooldown = (
            cls.WATCHDOG_RETRY_COOLDOWN
            if cls._watchdog_failures > 0
            else cls.WATCHDOG_RESTART_COOLDOWN
        )
        if now - cls._watchdog_last_restart < cooldown:
            return
        cls._watchdog_last_restart = now

        logger.warning(
            f"[XiaoAI] 音频输入断流 {silence:.0f}s，自动重启录音通道"
        )
        future = asyncio.run_coroutine_threadsafe(
            open_xiaoai_server.start_recording(),
            cls.async_loop,
        )
        try:
            # 看门狗独立线程，阻塞等待回执是安全的
            future.result(timeout=cls.WATCHDOG_RPC_TIMEOUT)
            cls._watchdog_failures = 0
            logger.info("[XiaoAI] 录音通道重启指令已确认")
            return
        except Exception as exc:
            cls._watchdog_failures += 1
            logger.error(
                f"[XiaoAI] 录音通道重启无回执"
                f"({cls._watchdog_failures}/{cls.WATCHDOG_MAX_FAILURES}): {exc}"
            )

        if cls._watchdog_failures < cls.WATCHDOG_MAX_FAILURES:
            return

        # 连续无回执 → 连接僵死：强制断开释放坑位，客户端会自动重连
        cls._watchdog_failures = 0
        logger.warning("[XiaoAI] 连接疑似僵死，强制断开以触发客户端重连")
        disconnect_future = asyncio.run_coroutine_threadsafe(
            open_xiaoai_server.force_disconnect(),
            cls.async_loop,
        )
        try:
            disconnect_future.result(timeout=10)
            logger.info("[XiaoAI] 僵尸连接已断开，等待客户端重连")
        except Exception as exc:
            logger.error(f"[XiaoAI] 强制断开失败: {exc}")

    @classmethod
    def _start_audio_watchdog(cls):
        audio_input_enabled = (
            __import__("os").environ.get("AUDIO_INPUT_ENABLE", "1").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if not audio_input_enabled:
            logger.debug("[XiaoAI] 音频输入已禁用，跳过断流看门狗")
            return

        # 以启动时刻为基线：覆盖"启动后从未收到过音频"的场景
        # （即 2026-07-12 22:44 事故的实际形态）
        cls._last_input_at = time.monotonic()

        def _watchdog_loop():
            while True:
                time.sleep(cls.WATCHDOG_CHECK_INTERVAL)
                try:
                    cls._audio_watchdog_tick()
                except Exception as exc:
                    logger.debug(f"[XiaoAI] 看门狗异常: {exc}")

        threading.Thread(
            target=_watchdog_loop, daemon=True, name="audio-input-watchdog"
        ).start()
        logger.info(
            f"[XiaoAI] 音频断流看门狗已启动（静默阈值 {cls.WATCHDOG_SILENCE_THRESHOLD}s，"
            f"冷却 {cls.WATCHDOG_RESTART_COOLDOWN}s）"
        )

    @classmethod
    def on_output_data(cls, data: bytes):
        async def on_output_data_async(data: bytes):
            return await open_xiaoai_server.on_output_data(data)

        asyncio.run_coroutine_threadsafe(
            on_output_data_async(data),
            cls.async_loop,
        )

    @classmethod
    async def run_shell(cls, script: str, timeout: float = 10 * 1000):
        return await open_xiaoai_server.run_shell(script, timeout)

    @classmethod
    async def on_event(cls, event: str):
        event_json = json_decode(event) or {}
        if not isinstance(event_json, dict):
            logger.debug("[XiaoAI] 忽略非字典事件负载")
            return

        event_data = event_json.get("data", {})
        event_type = event_json.get("event")

        if not event_json.get("event"):
            return

        # 记录所有事件用于调试监听退出
        logger.debug(f"[XiaoAI] 📡 收到事件: {event_type} | 数据: {event_data}")

        if event_type == "instruction":
            if not isinstance(event_data, dict):
                logger.debug(
                    f"[XiaoAI] 忽略非字典 instruction 数据: {event_data}"
                )
                return

            raw_line = event_data.get("NewLine")
            if not raw_line:
                return

            line = json_decode(raw_line) if isinstance(raw_line, str) else raw_line
            if not isinstance(line, dict):
                logger.debug(f"[XiaoAI] 忽略无法解析的指令行: {raw_line}")
                return

            header = line.get("header", {}) if isinstance(line.get("header"), dict) else {}
            dialog_id = header.get("dialog_id", "")
            namespace = header.get("namespace")
            header_name = header.get("name")

            if dialog_id and dialog_id in cls._suppressed_dialog_ids:
                if namespace in {"Nlp", "SpeechSynthesizer", "AudioPlayer"}:
                    await cls._suppress_dialog(
                        dialog_id,
                        f"{namespace}.{header_name}",
                    )
                    return
                if namespace == "Dialog" and header_name == "Finish":
                    cls._suppressed_dialog_ids.discard(dialog_id)
                    cls._suppressed_dialog_last_attempt.pop(dialog_id, None)
                    logger.debug(
                        f"[XiaoAI] Cleared suppressed dialog: {dialog_id}"
                    )
                    return

            if (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "SpeechRecognizer"
            ):
                if header_name == "RecognizeResult":
                    payload = line.get("payload", {})
                    if not isinstance(payload, dict):
                        logger.debug(
                            f"[XiaoAI] 忽略非字典 RecognizeResult payload: {payload}"
                        )
                        return

                    results = payload.get("results") or []
                    first_result = results[0] if results else {}
                    if isinstance(first_result, dict):
                        text = first_result.get("text") or ""
                    elif isinstance(first_result, str):
                        text = first_result
                    else:
                        text = ""
                    is_final = payload.get("is_final")
                    is_vad_begin = payload.get("is_vad_begin")

                    if EventManager.consume_openclaw_xiaoai_asr_result(
                        dialog_id=dialog_id,
                        text=text,
                        is_final=is_final,
                        is_vad_begin=is_vad_begin,
                    ):
                        if dialog_id and text and is_final:
                            await cls._suppress_dialog(
                                dialog_id,
                                f"OpenClaw 接管原生 ASR",
                            )
                        return
                    
                    # 只有明确的 is_vad_begin=False 且没有文本时才触发唤醒
                    # 避免重复触发
                    if not text and is_vad_begin is False:
                        # xiaoai_asr 接管会程序化静默唤醒小爱，设备同样上报此事件；
                        # 若最近刚发起过自唤醒，视为回环事件忽略，
                        # 否则接管动作会触发 on_interrupt 杀掉自己刚开启的会话
                        speaker = get_speaker()
                        if speaker and speaker.was_self_wake_recent():
                            logger.debug(
                                "[XiaoAI] 忽略自唤醒回环事件（程序化唤醒 3s 窗口内）"
                            )
                            return
                        logger.wakeup("小爱同学", module="XiaoAI")
                        cls.conversation.reset_retries()
                        EventManager.on_interrupt()
                    elif text and is_final and cls._is_external_wakeup_text(text):
                        await cls._suppress_dialog(
                            dialog_id,
                            f"外部唤醒词接管: {text}",
                        )
                        await EventManager.wakeup(text, "kws")
                        return
                    elif text and is_final:
                        logger.info(f"[XiaoAI] 🔥 收到指令: {text}")
                        cls.conversation.reset_retries()
                        await cls.conversation.handle_text_command(
                            text,
                            get_speaker(),
                        )
                        await EventManager.wakeup(text, "xiaoai")
                    elif is_final and not text:
                        logger.debug("[XiaoAI] 🛑 小爱监听超时自动退出")
                        await cls.conversation.handle_listening_timeout(
                            get_speaker()
                        )
            elif (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "AudioPlayer"
            ):
                cls.conversation.handle_audio_player_instruction(header_name)
        elif event_type == "playing":
            if not isinstance(event_data, str):
                logger.debug(
                    f"[XiaoAI] 忽略非字符串 playing 数据: {event_data}"
                )
                return

            playing_status = event_data.lower()
            
            get_speaker().status = playing_status
            await cls.conversation.handle_playing_status(
                playing_status,
                get_speaker(),
            )
        
        else:
            # 记录未处理的事件类型，可能包含监听退出信息
            logger.debug(f"[XiaoAI] ❓ 未处理的事件类型: {event_type} | 完整数据: {event_json}")

    @classmethod
    def __init_background_event_loop(cls):
        def run_event_loop():
            cls.async_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(cls.async_loop)
            cls._async_loop_ready.set()
            cls.async_loop.run_forever()

        thread = threading.Thread(target=run_event_loop, daemon=True)
        thread.start()
        cls._async_loop_ready.wait(timeout=2)

    @classmethod
    def __on_event(cls, event: str):
        future = asyncio.run_coroutine_threadsafe(
            cls.on_event(event),
            cls.async_loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(
                    f"[XiaoAI] Event handler failed: {type(exc).__name__}: {exc}"
                )

        future.add_done_callback(_log_result)

    @classmethod
    async def init_xiaoai(cls):
        cls.refresh_runtime_config()
        cls.config_manager.add_reload_listener(cls.refresh_runtime_config)
        set_xiaoai(XiaoAI)
        GlobalStream.on_output_data = cls.on_output_data
        open_xiaoai_server.register_fn("on_input_data", cls.on_input_data)
        open_xiaoai_server.register_fn("on_event", cls.__on_event)
        cls.__init_background_event_loop()
        cls._start_audio_watchdog()
        logger.info("[XiaoAI] 启动小爱音箱服务...")
        print(ASCII_BANNER)
        await open_xiaoai_server.start_server()

    @classmethod
    def stop_conversation(cls):
        cls.conversation.stop()
