#!/usr/bin/env python3
"""
把 萌鸡小队 + 小猪佩奇 两套音频做响度归一化（EBU R128 两遍 loudnorm），
统一到同一目标响度，消除两部片子音量不一致。保留原文件，输出归一化副本，
最后把交替播放列表原子切换到归一化副本（路径/顺序/进度 key 不变）。

用法: python3 normalize_loudness.py
可只重建列表(已归一化完时): python3 normalize_loudness.py --rebuild-only
"""
import os
import re
import sys
import json
import time
import shutil
import subprocess

KATURI = "/Volumes/music/萌鸡小队英文版"
PEPPA = "/Volumes/music/小猪佩奇英语音频196集(mp3)/第一季音频"
OUT_BASE = "/Volumes/music/英语听力计划_normalized"
OUT_K = os.path.join(OUT_BASE, "萌鸡")
OUT_P = os.path.join(OUT_BASE, "佩奇")
INTERLEAVE = "/Volumes/music/英语听力计划_萌鸡+佩奇交替"

TARGET_I, TARGET_TP, TARGET_LRA = -16.0, -1.5, 11.0


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def measure(inp):
    """第一遍：分析输入响度，返回 loudnorm 的 measured_* 参数。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", inp,
           "-af", f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    err = r.stderr
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def normalize(inp, outp):
    m = measure(inp)
    if not m:
        log(f"    测量失败: {os.path.basename(inp)}")
        return False
    af = (f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:"
          f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
          f"offset={m['target_offset']}:linear=true:print_format=summary")
    tmp = outp + ".tmp.mp3"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", inp,
           "-af", af, "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        log(f"    编码失败: {os.path.basename(inp)} :: {r.stderr[-160:]}")
        if os.path.exists(tmp):
            os.unlink(tmp)
        return False
    os.replace(tmp, outp)
    return True


def process_dir(src, out, tag):
    os.makedirs(out, exist_ok=True)
    files = sorted([f for f in os.listdir(src) if f.lower().endswith(".mp3")])
    done = 0
    for i, fn in enumerate(files, 1):
        inp = os.path.join(src, fn)
        outp = os.path.join(out, fn)
        if os.path.exists(outp) and os.path.getsize(outp) > 0:
            log(f"  [{tag} {i}/{len(files)}] 跳过(已存在) {fn}")
            done += 1
            continue
        t0 = time.monotonic()
        ok = normalize(inp, outp)
        dt = time.monotonic() - t0
        log(f"  [{tag} {i}/{len(files)}] {'✓' if ok else '✗'} ({dt:.0f}s) {fn}")
        if ok:
            done += 1
    return done, len(files)


def katuri_key(fn):
    m = re.search(r"S01E(\d+)", fn)
    return int(m.group(1)) if m else 9999


def peppa_key(fn):
    m = re.match(r"\s*(\d+)", fn)
    return int(m.group(1)) if m else 9999


def rebuild_interleave():
    """把交替列表原子切换到归一化副本；链接名/顺序与原来完全一致。"""
    katuri = sorted([f for f in os.listdir(OUT_K) if f.lower().endswith(".mp3")], key=katuri_key)
    peppa = sorted([f for f in os.listdir(OUT_P) if f.lower().endswith(".mp3")], key=peppa_key)
    if len(katuri) != 52 or len(peppa) != 52:
        log(f"⚠️ 归一化文件数不对(萌鸡{len(katuri)}/佩奇{len(peppa)})，跳过列表重建")
        return False

    newdir = INTERLEAVE + ".new"
    if os.path.exists(newdir):
        shutil.rmtree(newdir)
    os.makedirs(newdir)
    seq = 0
    for i in range(max(len(katuri), len(peppa))):
        for src_dir, lst, tag in ((OUT_K, katuri, "萌鸡"), (OUT_P, peppa, "佩奇")):
            if i < len(lst):
                seq += 1
                os.symlink(os.path.join(src_dir, lst[i]),
                           os.path.join(newdir, f"{seq:03d} [{tag}] {lst[i]}"))
    # 原子切换，窗口极小
    old = INTERLEAVE + ".old"
    if os.path.exists(old):
        shutil.rmtree(old)
    if os.path.exists(INTERLEAVE):
        os.rename(INTERLEAVE, old)
    os.rename(newdir, INTERLEAVE)
    if os.path.exists(old):
        shutil.rmtree(old)
    log(f"✅ 交替列表已切换到归一化副本({seq} 条)")
    return True


def main():
    rebuild_only = "--rebuild-only" in sys.argv
    if not rebuild_only:
        log("=========== 响度归一化开始 (目标 -16 LUFS) ===========")
        log("=== 萌鸡小队 ===")
        dk, tk = process_dir(KATURI, OUT_K, "萌鸡")
        log("=== 小猪佩奇 ===")
        dp, tp = process_dir(PEPPA, OUT_P, "佩奇")
        log(f"归一化完成: 萌鸡 {dk}/{tk}, 佩奇 {dp}/{tp}")
        if dk != tk or dp != tp:
            log("⚠️ 有文件未成功，先不重建列表。修好后可 --rebuild-only")
            sys.exit(1)
    log("=== 重建交替播放列表 ===")
    rebuild_interleave()
    log("=========== 全部完成 ===========")


if __name__ == "__main__":
    main()
