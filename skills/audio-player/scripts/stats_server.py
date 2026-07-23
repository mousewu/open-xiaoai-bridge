#!/usr/bin/env python3
"""
英语听力统计网页（只读）。

读取 play.py 写的播放事件流水 ~/.audio-player/playback_log.jsonl
与续播进度 ~/.audio-player/progress.json，按天聚合：
  - 每天收听总时长（小时）
  - 每天听了哪几集（含节目、状态、时长）
  - 计划总进度（第几遍 / 百分比）

用法：
  python3 stats_server.py                 # 默认 0.0.0.0:9099（局域网可访问，手机也能看）
  STATS_PORT=9099 STATS_BIND=127.0.0.1 python3 stats_server.py

纯标准库，无第三方依赖。
"""

import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RUNTIME_DIR = os.path.expanduser("~/.audio-player")
PLAYBACK_LOG = os.path.join(RUNTIME_DIR, "playback_log.jsonl")
PROGRESS_FILE = os.path.join(RUNTIME_DIR, "progress.json")
# 与 play.py 一致：队列 worker 的实时状态文件（固定绝对路径，不依赖 TMPDIR）
PID_FILE = os.path.join(RUNTIME_DIR, "queue.pid")
STATE_FILE = os.path.join(RUNTIME_DIR, "queue.state")
# 页面上次设置的音量（设备读不回来，用它回显滑块）
VOLUME_FILE = os.path.join(RUNTIME_DIR, "volume.json")
# 桥接地址：音量通过 /api/xiaoai/ask 交给原生小爱执行
BRIDGE_URL = os.environ.get("OPENXIAOAI_BASE_URL", "http://127.0.0.1:9092").rstrip("/")
# 播放/停止复用自洽的包装脚本（内部设好 PATH/ffprobe/服务地址）
PLAN_SH = os.path.expanduser("~/.audio-player/plan.sh")

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def run_plan(action):
    """调用 plan.sh start|stop。start 会拉起后台 worker 后很快返回。"""
    if action not in ("start", "stop"):
        return False, "bad action"
    try:
        r = subprocess.run(
            ["/bin/bash", PLAN_SH, action],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def load_volume():
    try:
        with open(VOLUME_FILE) as f:
            return int(json.load(f).get("level"))
    except Exception:
        return None


def save_volume(level):
    try:
        with open(VOLUME_FILE, "w") as f:
            json.dump({"level": int(level)}, f)
    except Exception:
        pass


def set_device_volume(level):
    """把音量设置交给原生小爱（不打断当前媒体播放）。返回是否成功。"""
    level = max(0, min(100, int(level)))
    body = json.dumps({"text": f"音量调到{level}%", "silent": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}/api/xiaoai/ask", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ok = bool(data.get("success"))
        if ok:
            save_volume(level)
        return ok
    except Exception:
        return False


def parse_show_title(basename):
    """从文件名解析节目与集名（与 play.py 同逻辑）。"""
    name = os.path.splitext(basename)[0]
    m = re.match(r"^\d+\s*\[(?P<tag>[^\]]+)\]\s*(?P<rest>.+)$", name)
    if m:
        title = re.sub(r"^萌鸡小队英文版\s*", "", m.group("rest").strip())
        return m.group("tag"), title
    return "其他", name


def load_now_playing():
    """读取队列 worker 的实时状态：是否在播、哪一集、本集进度。"""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # 探测进程存活
    except (OSError, ValueError, FileNotFoundError):
        return {"playing": False}
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        return {"playing": True}  # 在播但状态暂不可读
    show, title = parse_show_title(st.get("current", ""))
    return {
        "playing": True,
        "show": show,
        "title": title,
        "index": st.get("index"),
        "total": st.get("total"),
        "loop": st.get("loop"),
        "target_loops": st.get("target_loops"),
        "started": st.get("started"),
        "expected_sec": st.get("expected_sec"),
    }


def load_events():
    events = []
    try:
        with open(PLAYBACK_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return events


def load_progress():
    try:
        with open(PROGRESS_FILE) as f:
            p = json.load(f)
        total = int(p.get("total", 1)) or 1
        target = int(p.get("target_loops", 1))
        pos = int(p.get("pos", 0))
        grand = total * target
        done = pos >= grand
        return {
            "loop": target if done else pos // total + 1,
            "target": target,
            "track": total if done else pos % total + 1,
            "tracks_per_loop": total,
            "overall_done": pos,
            "overall_total": grand,
            "percent": round(pos / grand * 100, 1) if grand else 0.0,
            "complete": done,
        }
    except Exception:
        return None


def build_data():
    events = load_events()
    by_day = {}
    total_played = 0.0
    distinct = set()
    for e in events:
        date = e.get("date")
        if not date:
            continue
        played = float(e.get("played_sec") or 0)
        total_played += played
        if e.get("status") in ("completed", "interrupted"):
            distinct.add((e.get("show"), e.get("title")))
        d = by_day.setdefault(date, {"date": date, "played_sec": 0.0, "events": []})
        d["played_sec"] += played
        d["events"].append({
            "show": e.get("show"),
            "title": e.get("title"),
            "status": e.get("status"),
            "played_sec": played,
            "start": e.get("start"),
            "loop": e.get("loop"),
        })

    days = []
    for date in sorted(by_day.keys(), reverse=True):
        d = by_day[date]
        try:
            wd = WEEKDAYS[datetime.strptime(date, "%Y-%m-%d").weekday()]
        except Exception:
            wd = ""
        d["weekday"] = wd
        d["played_sec"] = round(d["played_sec"], 1)
        d["events"].sort(key=lambda x: x.get("start") or "")
        days.append(d)

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "now": load_now_playing(),
        "overall": {
            "total_played_sec": round(total_played, 1),
            "total_events": len(events),
            "distinct_titles": len(distinct),
            "active_days": len(by_day),
            "progress": load_progress(),
        },
        "days": days,
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>英语听力统计</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --ink:#1c2430; --muted:#69727e; --line:#e6e9ee;
    --accent:#3b6ef5; --katuri:#f59e0b; --peppa:#ec4899; --other:#94a3b8;
    --ok:#16a34a; --interrupt:#d97706;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#12151a; --card:#1b2027; --ink:#e8ebf0; --muted:#9aa4b1; --line:#2a313b;
      --accent:#6b9bff; --katuri:#fbbf24; --peppa:#f472b6; --other:#8792a3; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:860px;margin:0 auto;padding:20px 16px 60px}
  h1{font-size:22px;margin:4px 0 2px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:18px}
  .now{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px;
    display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .now.on{border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
  .dot{width:10px;height:10px;border-radius:99px;flex:0 0 auto;background:var(--muted)}
  .now.on .dot{background:var(--ok);box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 60%,transparent);
    animation:pulse 1.6s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 55%,transparent)}
    70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
  .now .lbl{font-weight:600}
  .now .ep{display:flex;align-items:center;gap:8px;min-width:0;flex:1 1 auto}
  .now .ep .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .now .meta{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
  .now .prog{flex-basis:100%;height:5px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:2px}
  .now .prog>span{display:block;height:100%;background:var(--ok);border-radius:99px;transition:width .8s linear}
  .vol{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:22px;
    display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .vol .vlabel{font-weight:600;white-space:nowrap}
  .vol input[type=range]{flex:1 1 160px;accent-color:var(--accent);height:4px;cursor:pointer}
  .vol input[type=range]:disabled{opacity:.5}
  .vol .vval{font-variant-numeric:tabular-nums;font-weight:650;min-width:46px;text-align:right}
  .vol .vhint{color:var(--muted);font-size:12px;white-space:nowrap;flex-basis:100%}
  .ctrl{display:flex;gap:10px;margin:-8px 0 22px}
  .btn{flex:1;padding:12px 16px;border-radius:12px;border:1px solid var(--line);cursor:pointer;
    font:inherit;font-weight:600;font-size:15px;color:var(--ink);background:var(--card);
    display:inline-flex;align-items:center;justify-content:center;gap:8px;transition:filter .15s,opacity .15s}
  .btn:hover{filter:brightness(1.08)}
  .btn:active{filter:brightness(.94)}
  .btn:disabled{opacity:.5;cursor:default}
  .btn.play{background:var(--ok);border-color:transparent;color:#fff}
  .btn.stop{background:var(--card)}
  .btn .ico{font-size:12px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card .k{color:var(--muted);font-size:12px;margin-bottom:6px}
  .card .v{font-size:24px;font-weight:650;letter-spacing:.2px}
  .card .v small{font-size:13px;font-weight:500;color:var(--muted);margin-left:2px}
  .bar-outer{height:6px;background:var(--line);border-radius:99px;margin-top:10px;overflow:hidden}
  .bar-inner{height:100%;background:var(--accent);border-radius:99px}
  h2{font-size:15px;margin:22px 0 10px;color:var(--muted);font-weight:600}
  .day{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:10px;overflow:hidden}
  .day summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px;padding:13px 16px}
  .day summary::-webkit-details-marker{display:none}
  .day .date{font-weight:600;min-width:96px}
  .day .wd{color:var(--muted);font-size:12px}
  .day .hrs{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
  .day .cnt{color:var(--muted);font-size:12px;min-width:56px;text-align:right}
  .daybar{height:8px;background:linear-gradient(90deg,var(--accent),var(--accent));border-radius:99px;flex:0 0 auto}
  .daybar-track{flex:1 1 120px;height:8px;background:var(--line);border-radius:99px;overflow:hidden;max-width:220px}
  .eps{border-top:1px solid var(--line);padding:6px 10px 12px}
  .ep{display:flex;align-items:center;gap:10px;padding:7px 8px;border-radius:8px}
  .ep:hover{background:rgba(127,127,127,.06)}
  .tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;color:#fff;white-space:nowrap}
  .tag.萌鸡{background:var(--katuri)} .tag.佩奇{background:var(--peppa)} .tag.其他{background:var(--other)}
  .ep .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ep .m{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
  .st{font-size:11px;padding:1px 6px;border-radius:6px;border:1px solid var(--line)}
  .st.completed{color:var(--ok)} .st.interrupted{color:var(--interrupt)}
  .st.skipped,.st.failed{color:var(--muted)}
  .empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:40px 16px;text-align:center;color:var(--muted)}
  .foot{color:var(--muted);font-size:12px;margin-top:20px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <h1>英语听力统计</h1>
  <div class="sub" id="sub">加载中…</div>
  <div class="now" id="now"><span class="dot"></span><span class="lbl">…</span></div>
  <div class="ctrl">
    <button class="btn play" id="btnPlay"><span class="ico">▶</span> 开始播放</button>
    <button class="btn stop" id="btnStop"><span class="ico">■</span> 停止</button>
  </div>
  <div class="vol" id="vol">
    <span class="vlabel">🔊 音量</span>
    <input type="range" id="volrange" min="0" max="100" step="5" value="50" aria-label="音量">
    <span class="vval" id="volval">--</span>
    <span class="vhint" id="volhint"></span>
  </div>
  <div class="cards" id="cards"></div>
  <h2>每日明细</h2>
  <div id="days"></div>
  <div class="foot" id="foot"></div>
</div>
<script>
function fmtHrs(sec){
  if(sec < 60) return sec.toFixed(0)+' 秒';
  const m = sec/60;
  if(m < 60) return m.toFixed(0)+' 分钟';
  return (sec/3600).toFixed(1)+' 小时';
}
function fmtMin(sec){ const m=sec/60; return m>=1 ? m.toFixed(0)+' 分' : sec.toFixed(0)+' 秒'; }
const STATUS_CN={completed:'完整',interrupted:'打断',skipped:'跳过',failed:'失败'};

let nowState=null;
function fmtClock(sec){sec=Math.max(0,Math.floor(sec));const m=Math.floor(sec/60);return m+':'+String(sec%60).padStart(2,'0');}
function renderNow(){
  const el=document.getElementById('now'); const n=nowState;
  if(!n||!n.playing){
    el.className='now';
    el.innerHTML='<span class="dot"></span><span class="lbl">未在播放</span><span class="meta">计划空闲中</span>';
    return;
  }
  el.className='now on';
  let meta = (n.loop&&n.target_loops) ? `第 ${n.loop}/${n.target_loops} 遍 · 第 ${n.index}/${n.total} 集` : '';
  let prog='';
  if(n.started&&n.expected_sec){
    const es=Date.now()/1000-n.started;
    const pct=Math.min(100, es/n.expected_sec*100);
    prog=`<span class="meta">${fmtClock(es)} / ${fmtClock(n.expected_sec)}</span>`+
         `<div class="prog"><span style="width:${pct}%"></span></div>`;
  }
  el.innerHTML='<span class="dot"></span><span class="lbl">正在播放</span>'+
    `<span class="ep"><span class="tag ${n.show}">${esc(n.show)}</span><span class="t" title="${esc(n.title)}">${esc(n.title)}</span></span>`+
    (meta?`<span class="meta">${meta}</span>`:'')+prog;
}
async function pollNow(){ try{ const r=await fetch('/api/now'); nowState=await r.json(); renderNow(); }catch(e){} }

const vr=document.getElementById('volrange'), vv=document.getElementById('volval'), vh=document.getElementById('volhint');
async function loadVolume(){
  try{
    const r=await fetch('/api/volume'); const d=await r.json();
    if(d.level!=null){ vr.value=d.level; vv.textContent=d.level+'%'; vh.textContent='页面记录的上次设置值（设备当前音量读不回来）'; }
    else { vv.textContent=vr.value+'%'; vh.textContent='拖动滑块设置音箱音量'; }
  }catch(e){ vh.textContent='读取音量失败'; }
}
vr.addEventListener('input',()=>{ vv.textContent=vr.value+'%'; });
vr.addEventListener('change', async ()=>{
  const lvl=parseInt(vr.value,10);
  vh.textContent='设置中…'; vr.disabled=true;
  try{
    const r=await fetch('/api/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:lvl})});
    const d=await r.json();
    vh.textContent = d.ok ? `已设为 ${lvl}%` : '设置失败，请重试（检查桥接是否在线）';
  }catch(e){ vh.textContent='设置失败：'+e; }
  finally{ vr.disabled=false; }
});

const btnPlay=document.getElementById('btnPlay'), btnStop=document.getElementById('btnStop');
async function control(action){
  btnPlay.disabled=true; btnStop.disabled=true;
  try{
    await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
  }catch(e){}
  // 立即刷新，并在几秒内再刷新几次（worker 拉起/停止需要一点时间落状态）
  pollNow();
  [1500,3500,6000].forEach(t=>setTimeout(pollNow,t));
  setTimeout(()=>{ btnPlay.disabled=false; btnStop.disabled=false; }, 1200);
}
btnPlay.addEventListener('click',()=>control('start'));
btnStop.addEventListener('click',()=>control('stop'));

async function load(){
  const r = await fetch('/api/data'); const d = await r.json();
  const o = d.overall;
  nowState = d.now; renderNow();
  document.getElementById('sub').textContent =
    `累计 ${o.active_days} 天收听 · 数据更新于 ${d.generated}`;

  const cards=[];
  cards.push(card('累计收听', fmtHrs(o.total_played_sec)));
  cards.push(card('播放集次', o.total_events, '次'));
  cards.push(card('去重集数', o.distinct_titles, '集'));
  if(o.progress){
    const p=o.progress;
    cards.push(card('计划进度', `第 ${p.loop}/${p.target} 遍`,
      '', `第 ${p.track}/${p.tracks_per_loop} 集 · ${p.percent}%`, p.percent));
  }
  document.getElementById('cards').innerHTML = cards.join('');

  const maxSec = Math.max(1, ...d.days.map(x=>x.played_sec));
  const box=document.getElementById('days');
  if(!d.days.length){ box.innerHTML='<div class="empty">还没有收听记录。<br>计划播放开始后，这里会按天出现统计。</div>'; }
  else box.innerHTML = d.days.map(day=>{
    const w = Math.round(day.played_sec/maxSec*100);
    const eps = day.events.map(e=>`
      <div class="ep">
        <span class="tag ${e.show}">${e.show}</span>
        <span class="t" title="${esc(e.title)}">${esc(e.title)}</span>
        <span class="st ${e.status}">${STATUS_CN[e.status]||e.status}</span>
        <span class="m">${fmtMin(e.played_sec)}</span>
      </div>`).join('');
    return `<details class="day">
      <summary>
        <span class="date">${day.date}</span>
        <span class="wd">${day.weekday}</span>
        <span class="daybar-track"><span class="daybar" style="width:${w}%"></span></span>
        <span class="hrs">${fmtHrs(day.played_sec)}</span>
        <span class="cnt">${day.events.length} 集</span>
      </summary>
      <div class="eps">${eps}</div>
    </details>`;
  }).join('');
  document.getElementById('foot').textContent='萌鸡小队 + 小猪佩奇 英语听力计划';
}
function card(k,v,unit='',hint='',pct=null){
  return `<div class="card"><div class="k">${k}</div>
    <div class="v">${v}${unit?`<small>${unit}</small>`:''}</div>
    ${hint?`<div class="k" style="margin:6px 0 0">${hint}</div>`:''}
    ${pct!=null?`<div class="bar-outer"><div class="bar-inner" style="width:${pct}%"></div></div>`:''}
    </div>`;
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
load().catch(e=>{document.getElementById('sub').textContent='加载失败：'+e;});
loadVolume();
setInterval(()=>load().catch(()=>{}), 60000);   // 每日明细/汇总 60s 刷新
setInterval(pollNow, 5000);                      // 实时状态 5s 拉取
setInterval(renderNow, 1000);                    // 本集进度条每秒平滑推进
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/data":
            self._send(200, json.dumps(build_data(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/now":
            self._send(200, json.dumps(load_now_playing(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/volume":
            self._send(200, json.dumps({"level": load_volume()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/volume":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                level = int(payload.get("level"))
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "bad level"}),
                           "application/json; charset=utf-8")
                return
            ok = set_device_volume(level)
            self._send(200 if ok else 502,
                       json.dumps({"ok": ok, "level": max(0, min(100, level))}),
                       "application/json; charset=utf-8")
        elif path == "/api/control":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                action = payload.get("action")
            except Exception:
                action = None
            if action not in ("start", "stop"):
                self._send(400, json.dumps({"ok": False, "error": "bad action"}),
                           "application/json; charset=utf-8")
                return
            ok, msg = run_plan(action)
            self._send(200 if ok else 502,
                       json.dumps({"ok": ok, "action": action, "message": msg}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass  # 静音访问日志


def main():
    bind = os.environ.get("STATS_BIND", "0.0.0.0")
    port = int(os.environ.get("STATS_PORT", "9099"))
    srv = ThreadingHTTPServer((bind, port), Handler)
    print(f"英语听力统计网页: http://{bind}:{port}/  (数据源: {PLAYBACK_LOG})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
