---
name: audio-player
description: Search and play audio from a local music library on the XiaoAI speaker via the OpenXiaoAI HTTP API, with playlist queue support and optional YouTube audio fallback. Use when the user wants to play music, podcasts, or kids' audio content on the speaker. Triggers on queries like "放歌", "放一首", "播放音乐", "点歌", "听歌", "播放播客", "放本地音乐", "播放专辑", "整季播放", "下一首", "停止播放", "audio-player".
---

# Audio Player

通过小爱音箱检索并播放本地音频库中的内容，支持模糊/拼音检索、目录整体连播（歌单队列）、跳曲/停止控制。本地库没有的内容可从 YouTube 下载音轨兜底。

## 前置配置

```bash
OPENXIAOAI_BASE_URL="http://192.168.x.x:9092"   # OpenXiaoAI 服务地址（默认 http://127.0.0.1:9092）
MUSIC_LIBRARY_DIR="~/Music"                      # 音频库根目录（默认 ~/Music）
AUDIO_PLAYER_CACHE="~/Music/YouTube"             # YouTube 下载目录（默认在音频库内，下载后可直接检索点播）
```

可选依赖（建议安装）：

```bash
brew install ffmpeg          # ffprobe 用于队列打断检测（强烈建议）
brew install yt-dlp          # YouTube 兜底下载
pip install pypinyin         # 拼音检索，容忍 ASR 同音字错误
```

## 标准工作流

用户说"放 XXX"时按以下顺序处理：

```bash
# 1. 检索本地库（输出 JSON 数组，按匹配分降序）
python3 scripts/search_local.py "yakka dee tiger"

# 2. 从结果中挑选并播放
python3 scripts/play.py "/path/to/file.mp3"              # 单曲，立即返回
python3 scripts/play.py "/path/to/S03 音頻/"             # 目录 → 整季连播（后台队列）
python3 scripts/play.py file1.mp3 file2.mp3 file3.mp3    # 多文件 → 歌单队列

# 3. 本地没有时，YouTube 兜底（需 yt-dlp）
python3 scripts/fetch_youtube.py search "关键词" --limit 5
python3 scripts/fetch_youtube.py download <视频ID>        # 输出 {"path": "..."}，再用 play.py 播放
# 下载文件存到 ~/Music/YouTube/"标题 [ID].mp3"，之后 search_local.py 可直接按标题搜到，
# 同一视频重复请求会复用已下载文件
```

## 播放控制

```bash
python3 scripts/play.py --next      # 跳到队列下一首
python3 scripts/play.py --stop      # 停止播放（含后台队列）
python3 scripts/play.py --status    # 查看队列状态（JSON：当前曲目/进度）
```

## Agent 决策指引

- **⚠️ 开始播放会结束语音会话**：调用 `play.py` 成功后，bridge 会自动终止当前的
  语音连续对话（防止你的文字回复被 TTS 播报时抢占音频通道、杀掉刚开始的播放）。
  因此：**你在播放开始之后的文字回复用户听不到**。如果想给用户语音确认
  （如"好的，即将播放 Dragon"），必须在调用 `play.py` **之前**用 xiaoai-tts skill
  播报（加 `--blocking` 等播完），然后再启动播放。播放开始后保持回复简短即可，
  不要依赖它传达信息。用户想继续对话需重新唤醒。

- **挑选结果靠语义判断**：ASR 识别的关键词可能有同音字错误（"晴天"→"情天"）。
  `search_local.py` 的拼音匹配（装了 pypinyin 时）能兜住大部分，但最终从候选列表里
  选哪一条应结合用户原话的语义判断。结果为空时，可尝试拆词、换写法重搜。
- **单曲还是整季**："放 Tiger 那集"→ 选单个文件；"把第三季放一遍"→ 直接把目录传给
  `play.py`，自动展开为按文件名排序的队列。
- **本地优先**：先查本地库，没有再问用户是否从 YouTube 找（下载转码需 10~60 秒，
  下载前建议先用 xiaoai-tts 或 `/api/play/text` 播报一句"正在下载"）。
- **播放开始即成功**：单曲模式非阻塞，命令返回即代表音箱已开始播放，无需等待。

## 行为与限制

- **格式**：仅支持 mp3 / flac / wav / ogg（bridge 的 Rust 解码器不支持 m4a/aac，
  此类文件需先 `ffmpeg -i in.m4a out.mp3` 转码）。
- **语音打断**：播放中喊"小爱同学"会立即停止当前曲目；后台队列检测到打断后自动停止，
  不会抢着播下一首。此检测依赖 ffprobe（比较实际播放时长与音频时长），未安装时队列
  无法识别打断，会继续放下一首，只能用 `--stop` 停止。
- **新播放抢占旧播放**：任何新的 play 调用会自动停掉正在进行的播放和队列（bridge 的
  playback token 机制 + 脚本主动清理队列进程）。
- **播放接管语音会话**：`/api/play/file` 和 `/api/play/url` 会自动终止进行中的
  OpenClaw/OpenAI 语音连续对话，播放期间喊"小爱同学"可打断播放。
- **队列是本机后台进程**：状态文件在系统临时目录（`audio_player_queue.*`），队列日志
  在 `audio_player_queue.log`，排查连播问题时查看该日志。
