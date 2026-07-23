#!/usr/bin/env python3
"""
播放本地音频（单曲 / 歌单队列）到小爱音箱。

单曲默认非阻塞：调用后立即返回，音箱开始播放。
多个文件或目录进入歌单模式：后台进程顺序播放，播完自动下一首。

续播模式（--resume）：
  为长期循环计划设计。持久化记录"当前播到列表第几条 / 第几遍"，
  被"小爱同学"打断或 --stop 停止后，下次 --resume 从**当前这一条**接着放，
  跨时段、跨天累计进度。配合 --loops N 指定总循环遍数，跑满即停。
  进度文件：~/.audio-player/progress.json（--reset 清零重来）。

打断行为：
  - 播放中喊"小爱同学"会停止当前曲目，队列检测到后自动停止
    （检测依赖 ffprobe 探测时长；未安装 ffprobe 时无法识别打断，
    队列会继续放下一首，需用 --stop 手动停止）
  - --next 跳到下一首，--stop 停止全部
"""

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import interrupt, upload_and_play

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg"}

# 队列运行时文件放固定绝对路径，不依赖 TMPDIR。
# （关键：语音经 OpenClaw 网关启动的 worker 会有不同的 TMPDIR，
#  若用 tempfile.gettempdir() 会导致 PID/STATE 落在不同目录，
#  使统计网页读不到状态、且定时 --stop 找不到语音启动的 worker。）
RUNTIME_DIR = os.path.expanduser("~/.audio-player")
os.makedirs(RUNTIME_DIR, exist_ok=True)
PID_FILE = os.path.join(RUNTIME_DIR, "queue.pid")
STATE_FILE = os.path.join(RUNTIME_DIR, "queue.state")
SKIP_FILE = os.path.join(RUNTIME_DIR, "queue.skip")
LOG_FILE = os.path.join(RUNTIME_DIR, "queue.log")

# 续播进度需跨会话/重启存活，放在家目录而非临时目录
PROGRESS_FILE = os.path.expanduser("~/.audio-player/progress.json")
# 每集播放事件流水（供统计网页读取）
PLAYBACK_LOG = os.path.expanduser("~/.audio-player/playback_log.jsonl")


def parse_show_title(basename):
    """从文件名解析节目与集名。
    交替列表命名如 "001 [萌鸡] 萌鸡小队英文版 S01E01 看一看妈妈就知道.mp3"
    普通文件则 show=其他、title=去扩展名的文件名。
    """
    name = os.path.splitext(basename)[0]
    m = re.match(r"^\d+\s*\[(?P<tag>[^\]]+)\]\s*(?P<rest>.+)$", name)
    if m:
        show = m.group("tag")
        title = m.group("rest").strip()
        # 去掉"萌鸡小队英文版 "这种冗余前缀，尽量留 S01Exx 集名
        title = re.sub(r"^萌鸡小队英文版\s*", "", title)
        return show, title
    return "其他", name


def log_playback_event(path, loop_no, expected, elapsed, status, start_wall):
    """把一次播放追加为 JSONL 一行；任何异常都不得影响播放。"""
    try:
        show, title = parse_show_title(os.path.basename(path))
        if expected and status == "completed":
            played = round(min(elapsed, expected), 1)
        else:
            played = round(max(0.0, elapsed), 1)
        dt = datetime.datetime.fromtimestamp(start_wall)
        event = {
            "ts": round(start_wall, 3),
            "date": dt.strftime("%Y-%m-%d"),
            "start": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "show": show,
            "title": title,
            "file": os.path.basename(path),
            "expected_sec": round(expected, 1) if expected else None,
            "played_sec": played,
            "status": status,          # completed | interrupted | skipped | failed
            "loop": loop_no,
        }
        os.makedirs(os.path.dirname(PLAYBACK_LOG), exist_ok=True)
        with open(PLAYBACK_LOG, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"记录播放日志失败（忽略）: {e}", flush=True)


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


# ---------- 续播进度持久化 ----------

def load_progress_raw():
    try:
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def load_progress(key):
    """读取进度；key 为 None 时不校验归属，否则要求归属同一播放列表"""
    d = load_progress_raw()
    if d and (key is None or d.get("key") == key):
        return d
    return None


def save_progress(key, total, target, pos):
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"key": key, "total": total, "target_loops": target, "pos": pos},
                  f, ensure_ascii=False)
    os.replace(tmp, PROGRESS_FILE)  # 原子写，防止停机时写坏


def reset_progress():
    try:
        os.unlink(PROGRESS_FILE)
    except OSError:
        pass


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
    # 注意：--stop 不清进度，下次 --resume 从当前曲续播
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
    out = {}
    prog = load_progress_raw()
    if prog:
        total = int(prog.get("total", 1)) or 1
        target = int(prog.get("target_loops", 1))
        pos = int(prog.get("pos", 0))
        grand = total * target
        done = pos >= grand
        out["progress"] = {
            "loop": target if done else pos // total + 1,
            "target_loops": target,
            "track": total if done else pos % total + 1,
            "tracks_per_loop": total,
            "overall_done": pos,
            "overall_total": grand,
            "percent": round(pos / grand * 100, 1) if grand else 0.0,
            "complete": done,
        }
    if not pid:
        out["queue"] = "idle"
        print(json.dumps(out, ensure_ascii=False))
        return
    state = {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        pass
    out.update({"queue": "playing", "pid": pid, **state})
    print(json.dumps(out, ensure_ascii=False))


def run_queue_worker(files, resume=False, loops=None, key=None):
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    total = len(files)
    target = loops if loops else 1
    pos = 0
    if resume:
        prog = load_progress(key)
        if prog and int(prog.get("total", 0)) == total:
            pos = int(prog.get("pos", 0))
            if not loops:
                target = int(prog.get("target_loops", 1))
        save_progress(key, total, target, pos)
    grand = total * target

    consecutive_failures = 0
    try:
        while pos < grand:
            idx = pos % total
            loop_no = pos // total + 1
            path = files[idx]
            expected = probe_duration(path)
            start_wall = time.time()
            with open(STATE_FILE, "w") as f:
                json.dump(
                    {"current": os.path.basename(path), "index": idx + 1, "total": total,
                     "loop": loop_no, "target_loops": target,
                     "overall": pos + 1, "overall_total": grand,
                     "started": round(start_wall, 3),
                     "expected_sec": round(expected, 1) if expected else None},
                    f, ensure_ascii=False,
                )
            # 播放前先把"当前曲"落盘：若中途被打断/SIGTERM，进度停在当前曲，下次续播重放它
            if resume:
                save_progress(key, total, target, pos)

            started = time.monotonic()
            try:
                upload_and_play(path, blocking=True)
                consecutive_failures = 0
            except Exception as e:
                print(f"播放失败，跳过 {path}: {e}", flush=True)
                log_playback_event(path, loop_no, expected, 0.0, "failed", start_wall)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("连续失败 3 次，服务可能不可用，停止队列", flush=True)
                    break
                pos += 1
                if resume:
                    save_progress(key, total, target, pos)
                continue
            elapsed = time.monotonic() - started

            if os.path.exists(SKIP_FILE):
                os.unlink(SKIP_FILE)  # 用户主动跳过，继续下一首
                log_playback_event(path, loop_no, expected, elapsed, "skipped", start_wall)
                pos += 1
                if resume:
                    save_progress(key, total, target, pos)
                continue
            # 明显早于预期时长结束 → 大概率被"小爱同学"打断，停止队列
            # 不推进 pos：下次 --resume 会从当前这一集重新开始
            if expected and elapsed < expected - max(10.0, expected * 0.15):
                print(
                    f"检测到播放被打断（{elapsed:.0f}s / 预期 {expected:.0f}s），停止队列",
                    flush=True,
                )
                log_playback_event(path, loop_no, expected, elapsed, "interrupted", start_wall)
                break
            # 完整播完 → 推进到下一条
            log_playback_event(path, loop_no, expected, elapsed, "completed", start_wall)
            pos += 1
            if resume:
                save_progress(key, total, target, pos)
            time.sleep(0.5)  # 曲间停顿
        else:
            if resume:
                print("🎉 循环计划已全部完成", flush=True)
    finally:
        for f in (PID_FILE, STATE_FILE, SKIP_FILE):
            if os.path.exists(f):
                os.unlink(f)


def main():
    parser = argparse.ArgumentParser(description="播放音频到小爱音箱")
    parser.add_argument("paths", nargs="*", help="音频文件或目录（多个/目录即为歌单）")
    parser.add_argument("--stop", action="store_true", help="停止当前播放和队列（不清续播进度）")
    parser.add_argument("--next", dest="next_track", action="store_true", help="跳到队列下一首")
    parser.add_argument("--status", action="store_true", help="查看队列/续播状态（JSON）")
    parser.add_argument("--resume", action="store_true",
                        help="续播模式：从上次进度接着放，跨时段/打断累计")
    parser.add_argument("--loops", type=int, default=None,
                        help="续播模式下的总循环遍数（首次写入进度，之后可省略）")
    parser.add_argument("--reset", action="store_true", help="清零续播进度，重头开始")
    parser.add_argument("--progress-key", default=None, help=argparse.SUPPRESS)
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
    if args.reset:
        reset_progress()
        print("🔁 已重置续播进度")
        if not args.paths:
            return
    if not args.paths:
        parser.error("缺少音频文件/目录参数")

    files = expand_paths(args.paths)
    if not files:
        print("❌ 未找到可播放的音频文件")
        sys.exit(1)

    key = args.progress_key or os.path.abspath(os.path.expanduser(args.paths[0]))

    if args.queue_worker:
        run_queue_worker(files, resume=args.resume, loops=args.loops, key=key)
        return

    # 续播模式：进度已跑满则不再播放
    if args.resume:
        prog = load_progress(key)
        if prog and int(prog.get("total", 0)) == len(files):
            target = args.loops or int(prog.get("target_loops", 1))
            if int(prog.get("pos", 0)) >= len(files) * target:
                print("🎉 循环计划已全部完成，无需播放（--reset 可重头开始）")
                return

    stop_queue()  # 新播放前停掉旧队列，避免两路播放争抢

    if len(files) > 1 or args.resume:
        extra = []
        if args.resume:
            extra.append("--resume")
        if args.loops:
            extra += ["--loops", str(args.loops)]
        extra += ["--progress-key", key]
        log = open(LOG_FILE, "a")
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--queue-worker"] + extra + files,
            stdout=log, stderr=log, start_new_session=True,
        )
        tip = "（续播）" if args.resume else ""
        print(f"🎵 歌单已开始后台播放{tip}，共 {len(files)} 首（日志: {LOG_FILE}）")
    else:
        result = upload_and_play(files[0], blocking=False)
        if result.get("success"):
            print(f"🎵 开始播放: {os.path.basename(files[0])}")
        else:
            print(f"⚠️ 播放失败: {result}")
            sys.exit(1)


if __name__ == "__main__":
    main()
