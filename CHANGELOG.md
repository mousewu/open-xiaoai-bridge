# Changelog

All notable changes to this project will be documented in this file.

## Fork 版本 - 2026-07-09

> 本版本基于 [coderzc/open-xiaoai-bridge](https://github.com/coderzc/open-xiaoai-bridge) v1.0.6 之后的 main 分支，以下为相对上游的全部修改。

### 新增：语音接口新版鉴权（X-Api-Key）

- **TTS**：Rust `DoubaoStreamClient` 支持 `X-Api-Key` 鉴权与自定义端点（`api_key` / `api_url` 参数已透传至全部 TTS pyfunction），兼容两个入口：
  - 火山豆包语音新版控制台（默认端点）
  - 方舟 Agent Plan `/plan/tts/unidirectional` 端点（语音调用走套餐 AFP 抵扣）
  - 旧版 `X-Api-App-Id` + `X-Api-Access-Key` 鉴权保持兼容，`api_key` 优先
- **ASR**：新增 SAUC 流式识别 provider（`asr.model = "sauc"`，`core/services/audio/asr/sauc.py`），对接豆包流式语音识别大模型 2.0 的 WebSocket 二进制协议（header + gzip 帧、正负序号包），整句模式：VAD 判定说完后全速推送音频取最终结果。同样支持 Agent Plan `/plan/sauc/bigmodel_async` 与豆包语音原生端点两个入口
- 配置示例见 `config.py` 的 `asr.sauc` 与 `tts.doubao.api_key` 注释

### 新增：audio-player skill（skills/audio-player/）

面向 OpenClaw Agent 的本地音频库语音点播工具：

- `search_local.py`：检索本地音频库（默认 `~/Music`），中英文子串 + 拼音匹配（容忍 ASR 同音字错误，pypinyin 可选），括号/分隔符归一化
- `play.py`：单曲非阻塞播放；目录/多文件自动进入后台歌单队列（顺序连播、`--next` 跳曲、`--stop` 停止、`--status` 查询）；基于 ffprobe 时长对比检测语音打断，被"小爱同学"打断后队列自动停止；连续失败 3 次自动中止
- `fetch_youtube.py`：本地没有时的 YouTube 兜底（yt-dlp），下载音轨转 mp3 存入 `~/Music/YouTube/`，文件名"标题 [视频ID].mp3"（标题供后续本地检索，ID 供去重复用）

### 修复：媒体播放与语音会话的通道冲突

- **播放被回复 TTS 抢占**：Agent 通过 `/api/play/file`、`/api/play/url` 开始播放后，稍后到达的回复 TTS 会因 playback token 抢占机制杀掉刚开始的播放。现在这两个端点会先自动终止进行中的 OpenClaw/OpenAI 连续对话（新增 `WakeupSessionManager.stop_external_conversations()`），播放即接管音频通道
- **停止播放后音频"复活"**：`SpeakerManager.stop_device_audio()` 原来只杀设备端 aplay，Rust 播放泵仍持有有效 token，会在下一个 chunk 发送时通过 `ensure_player_ready()` 重新拉起 aplay。现在先全局取消播放会话（`stop_tts_playback(None)`）再杀 aplay，覆盖"小爱同学"打断、`/api/interrupt`、Agent 主动停止三条路径
- **退出关键词区分意图**：连续对话中说"停止"会同时停掉正在播放的内容；"退出/再见"仅退出对话，保留后台播放

### 文档

- `AGENTS.md`：修正 PCM 通道中断方式说明（必须 token 取消 + 杀 aplay 两步）；登记 audio-player skill
- `config.py`：补充 SAUC ASR 与 TTS X-Api-Key 配置示例（占位符）

## v1.0.6 - 2026-04-05

### 重点更新

- 新增 WebSocket Bearer Token 鉴权支持，设置 `OPEN_XIAOAI_TOKEN` 环境变量后，客户端须在握手时携带 `Authorization: Bearer <token>` 请求头，否则连接将被拒绝（返回 401）。未设置该变量时保持原有无鉴权行为。
- 连接日志新增鉴权失败原因输出，方便快速定位认证问题。

### 修复与优化

- 修复 OpenClaw agent 事件未按 `run_id` 过滤的问题，避免多次唤醒后事件监听器持续累积导致的内存泄漏。

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.5...v1.0.6

## v1.0.5 - 2026-03-29

### 重点更新

- 新增小爱原生 ASR 模式 (`OPENCLAW_XIAOAI_NATIVE_ASR`)，可在 OpenClaw 连续对话中使用小爱自带的语音识别能力，降低对离线 ASR 模型的依赖。
- 新增可配置的音频输入增益 (config `audio.input_gain`)，支持调节麦克风输入音量以优化唤醒词识别灵敏度。
- 新增音频输入开关 (`AUDIO_INPUT_ENABLE`)，可在不需要音频输入时禁用以节省系统资源。
- 新增发送消息提示音，改善 OpenClaw 连续对话的交互体验。(#11 by @codertinat)

### 修复与优化

- 修复 OpenClaw 小爱原生 ASR 模式下的超时处理，确保桥接超时配置被正确遵循。
- 优化环境变量命名：`OPENCLAW_ENABLED` → `OPENCLAW_ENABLE`（保留向后兼容，新变量优先）。
- 优化 CMake 启动脚本，修复构建相关问题。(#11 by @codertinat)

### 文档更新

- 补充音频输入增益配置的 FAQ 说明。
- 优化 Docker FAQ 格式，统一文档风格。
- 更新 OpenClaw 连接说明文档。

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.4...v1.0.5

## v1.0.4 - 2026-03-26

### 重点更新

- 新增可配置的 ASR 后端，支持通过配置切换不同语音识别模型。
- 优化设备端音频播放链路，通过延迟启动播放降低 `aplay` underrun 问题。
- 优化长时间运行场景下的内部状态管理，减少潜在内存泄漏风险。

### 修复与优化

- 修复 `after_wakeup` 回调中未正确透传 `source` 参数的问题，改善小智/OpenClaw 会话退出后的收尾逻辑。
- 调整 XiaoZhi、XiaoAI、OpenClaw 以及原生音频相关实现，优化稳定性与部分边界行为。
- 补充和整理 Docker / README 相关说明，提升部署与使用时的可读性。

### 文档更新

- 补充并整理项目文档说明，优化 README 的来源说明、致谢与相关文案表达。
- 更新 LICENSE 中的版权声明，保留上游作者信息并补充当前项目维护者信息。
- 更新 Docker 使用说明，改善 Windows 用户的部署体验。(#8 by @JackieQiang)

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.3...v1.0.4

## v1.0.3 - 2026-03-25

### 重点更新

- 豆包 TTS 升级支持新的 2.0 音色，并补充配套的辅助脚本与接口文档，便于查询和验证可用音色。
- 新增 `scripts/clone_voice.py` 声音复刻脚本，支持提交音频样本并查询训练状态。
- 新增 `scripts/generate_tts.py` 音频生成脚本，可按指定 `speaker_id`、文本和情感参数导出音频文件。
- 新增播放服务端音频文件的能力，可通过 API 直接下发本地文件进行播放。
- 优化 OpenClaw TTS 打断与设备音频关闭流程，减少播放被打断后残留音频状态未清理的问题。

### 修复与优化

- 修复外部唤醒词触发时，小爱仍然回声式回复的问题，降低路由到第三方 AI 时的干扰。
- 修复用户喊出“小爱同学”打断后，小智唤醒会话没有完全恢复的问题，避免后续唤醒失效。
- 在 Doubao TTS API 返回成功前增加请求校验，避免无效请求被误判为成功。
- 优化 Doubao TTS 的错误处理与日志输出，减少重复报错，并在流式/后台播放失败时保留更完整的上下文。
- 调整 `docker-compose.yml`，移除 `network_mode: host`，改善默认 Docker Compose 部署的兼容性。
- 调整部分 XiaoZhi/OpenClaw 内部流程与日志细节，减少连续对话等待和排障成本。

### 文档更新

- 补充 Doubao TTS 接口、声音复刻和指定音色导出脚本的使用说明。

### Full Changelog

- https://github.com/coderzc/open-xiaoai-bridge/compare/v1.0.2...v1.0.3
