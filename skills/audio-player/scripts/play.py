#!/usr/bin/env python3
"""
播放本地音频（单曲 / 歌单队列）到小爱音箱。

单曲默认非阻塞：调用后立即返回，音箱开始播放。
多个文件或目录进入歌单模式：后台进程顺序播放，播完自动下一首。

打断行为：
  - 播放中喊"小爱同学"会停止当前曲目，队列检测到后自动停止
    （检测依赖 ffprobe 探测时长；未安装 ffprobe 时无法识别打断，
    队列会继续放下一首，需用 --stop 手动停止）
  - --next 跳到下一首，--stop 停止全部
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import interrupt, upload_and_play

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg"}

PID_FILE = os.path.join(tempfile.gettempdir(), "audio_player_queue.pid")
STATE_FILE = os.path.join(tempfile.gettempdir(), "audio_player_queue.state")
SKIP_FILE = os.path.join(tempfile.gettempdir(), "audio_player_queue.skip")
LOG_FILE = os.path.join(tempfile.gettempdir(), "audio_player_queue.log")


def probe_duration(path):
    """用 ffprobe 获取音频时长（秒），不可用时返回 None"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def expand_paths(paths):
    """展开目录为其中的音频文件（按路径排序），校验文件存在"""
    files = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            found = []
            for dirpath, _dirnames, filenames in os.walk(p):
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                        found.append(os.path.join(dirpath, fn))
            files.extend(sorted(found))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"❌ 文件不存在: {p}")
            sys.exit(1)
    return files


def read_queue_pid():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # 探测进程是否存活
        return pid
    except (OSError, ValueError):
        return None


def stop_queue():
    """停止后台队列进程（如有），返回是否曾在运行"""
    pid = read_queue_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for f in (PID_FILE, STATE_FILE, SKIP_FILE):
        if os.path.exists(f):
            os.unlink(f)
    return pid is not None


def cmd_stop():
    was_running = stop_queue()
    try:
        interrupt()
    except Exception as e:
        print(f"⚠️ 调用 /api/interrupt 失败: {e}")
    print("⏹️ 已停止播放" + ("（含后台队列）" if was_running else ""))


def cmd_next():
    if read_queue_pid():
        # 先落 skip 标记再打断，队列 worker 据此续播而非停止
        with open(SKIP_FILE, "w") as f:
            f.write("1")
        try:
            interrupt()
        except Exception as e:
            print(f"⚠️ 调用 /api/interrupt 失败: {e}")
        print("⏭️ 已跳到下一首")
    else:
        try:
            interrupt()
        except Exception as e:
            print(f"⚠️ 调用 /api/interrupt 失败: {e}")
        print("⏹️ 当前无队列，已停止播放")


def cmd_status():
    pid = read_queue_pid()
    if not pid:
        print(json.dumps({"queue": "idle"}, ensure_ascii=False))
        return
    state = {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        pass
    print(json.dumps({"queue": "playing", "pid": pid, **state}, ensure_ascii=False))


def run_queue_worker(files):
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    consecutive_failures = 0
    try:
        for index, path in enumerate(files):
            with open(STATE_FILE, "w") as f:
                json.dump(
                    {"current": os.path.basename(path), "index": index + 1, "total": len(files)},
                    f, ensure_ascii=False,
                )
            expected = probe_duration(path)
            started = time.monotonic()
            try:
                upload_and_play(path, blocking=True)
                consecutive_failures = 0
            except Exception as e:
                print(f"播放失败，跳过 {path}: {e}", flush=True)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("连续失败 3 次，服务可能不可用，停止队列", flush=True)
                    break
                continue
            elapsed = time.monotonic() - started

            if os.path.exists(SKIP_FILE):
                os.unlink(SKIP_FILE)  # 用户主动跳过，继续下一首
                continue
            # 明显早于预期时长结束 → 大概率被"小爱同学"打断，停止队列
            if expected and elapsed < expected - max(10.0, expected * 0.15):
                print(
                    f"检测到播放被打断（{elapsed:.0f}s / 预期 {expected:.0f}s），停止队列",
                    flush=True,
                )
                break
            time.sleep(0.5)  # 曲间停顿
    finally:
        for f in (PID_FILE, STATE_FILE, SKIP_FILE):
            if os.path.exists(f):
                os.unlink(f)


def main():
    parser = argparse.ArgumentParser(description="播放音频到小爱音箱")
    parser.add_argument("paths", nargs="*", help="音频文件或目录（多个/目录即为歌单）")
    parser.add_argument("--stop", action="store_true", help="停止当前播放和队列")
    parser.add_argument("--next", dest="next_track", action="store_true", help="跳到队列下一首")
    parser.add_argument("--status", action="store_true", help="查看队列状态（JSON）")
    parser.add_argument("--queue-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.stop:
        cmd_stop()
        return
    if args.next_track:
        cmd_next()
        return
    if args.status:
        cmd_status()
        return
    if not args.paths:
        parser.error("缺少音频文件/目录参数")

    files = expand_paths(args.paths)
    if not files:
        print("❌ 未找到可播放的音频文件")
        sys.exit(1)

    if args.queue_worker:
        run_queue_worker(files)
        return

    stop_queue()  # 新播放前停掉旧队列，避免两路播放争抢

    if len(files) > 1:
        log = open(LOG_FILE, "a")
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--queue-worker"] + files,
            stdout=log, stderr=log, start_new_session=True,
        )
        print(f"🎵 歌单已开始后台播放，共 {len(files)} 首（日志: {LOG_FILE}）")
    else:
        result = upload_and_play(files[0], blocking=False)
        if result.get("success"):
            print(f"🎵 开始播放: {os.path.basename(files[0])}")
        else:
            print(f"⚠️ 播放失败: {result}")
            sys.exit(1)


if __name__ == "__main__":
    main()
