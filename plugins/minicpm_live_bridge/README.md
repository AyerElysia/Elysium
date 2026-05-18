# MiniCPM Live Bridge

这是 MiniCPM-o live 的外部服务器桥接层。

Neo-MoFox 不在主进程里下载、加载或运行 MiniCPM-o 模型。真正的全双工多模态推理由你后续接入的外部服务器负责；本插件只提供：

- `/minicpm-live/` 浏览器页面，用于采集麦克风和桌面屏幕。
- `/minicpm-live/api/config` 下发前端配置。
- `/minicpm-live/api/status` 检查外部服务器健康状态。
- `/minicpm-live/api/sessions` 创建本地 session，并可选转发到外部服务器。
- `/minicpm-live/api/events` 记录字幕、文本、打断和状态事件。
- `/minicpm-live/api/debug/log` 接收浏览器页面的调试状态并打印到 Neo 终端。
- `/minicpm-live/api/context` 暴露 live 可读的统一意识运行态、实时统一事件流和当前 live session 短期上下文。
- `/minicpm-live/api/turn` 在未配置外部 WebSocket 时，直接调用 `model_tasks.live` 跑一轮文本 + 屏幕截图的半双工 API。
- `/minicpm-live/api/unified/ws` 把 Neo 核心消息事件实时推给 live 前端，再转发给外部 live 服务器。
- `/minicpm-live/api/realtime/ws` Neo 自己的 realtime proxy WebSocket。开启代理模式后，前端只连 Neo；Neo 再把消息翻译后转给上游双工模型。

边界：live 的 realtime 模型可以维护自己的低延迟音频会话，但它不是新的 Neo 主意识。它只作为一个实时通道接入统一事件流：

- QQ/其他通道的收发消息会以 `unified.event` 推给 live。
- live 用户语音转写、live 模型文本回复、live 音频回复文本会回灌到 Neo 的统一事件流。
- 本地单轮 API 会在每轮调用前复用 `life_chatter` 的完整提示词构建链路：SOUL、MEMORY、聊天历史格式、`<new_messages>`、`<life_runtime_context>` 都由 `LifeChatter.build_live_bridge_prompt()` 同源生成。跨 QQ/直播/live 的 `<chat_history>` 优先从统一 `messages` 表读取，而不是只看当前进程已加载的内存 stream。live 只额外追加一个输出适配说明，把 `life_send_text` 语义映射成直接口播文本。
- live 默认只读 life_chatter 运行态 cursor，不推进全局 cursor，避免 live 消耗 QQ 主链路尚未读取的事件；如确实要让 live 读取后提交 cursor，可开启 `context.mark_life_context_seen`。
- live 屏幕摘要默认只写入 life_engine 事件流，不制造普通聊天未读消息。
- 默认不把 live 用户转写发布成 `ON_MESSAGE_RECEIVED`，因此不会强制唤醒核心 Chatter；需要时可开启 `session.dispatch_user_transcript_to_chatter`。

如果 `server.websocket_url` 留空且 `session.enable_local_api_turn = true`，页面会进入本地单轮 API 模式：发送文本时同时截取当前屏幕帧，调用 `config/model.toml` 的 `[model_tasks.live]`。这适合先测试非全双工的全模态 API，例如 `MiMo-V2-Omni`。

本地单轮 API 模式也支持语音回合：点击页面上的 `语音` 开始录音，再次点击发送。浏览器会把麦克风音频编码成 WAV，连同当前屏幕截图一起提交给 `/api/turn`；模型返回文本后，页面会用浏览器 TTS 朗读。

## 传输模式

`[server].transport_mode` 现在有两种：

- `browser_direct`：浏览器直接连接 `server.websocket_url`。这适合上游已经兼容 Neo live v0 协议的服务。
- `neo_proxy`：浏览器连接 Neo 的 `/api/realtime/ws`，Neo 再连接真正的上游 WebSocket。后续要接 MiniCPM、Gemini 风格 live、或者任何 provider-specific realtime 协议，都从这里做 adapter，不再修改前端页面。

`[server].protocol_adapter` 当前提供：

- `passthrough`：前后都说同一套 Neo live v0 协议。
- `minicpm_realtime_v0`：MiniCPM-o Realtime API 适配。Neo 会把前端的 `session.start/context.snapshot/unified.event/screen.frame/audio.chunk/session.stop` 翻译成 MiniCPM 的 `session.update/input_audio_buffer.append/session.close`，并把 `response.output_audio.delta/response.listen/session.closed/error` 翻译回页面可消费的字幕、PCM 音频和状态事件。

MiniCPM-o 4.5 的官方 realtime 入口推荐配置：

```toml
[server]
base_url = "http://127.0.0.1:8010"
websocket_url = "ws://127.0.0.1:8010/v1/realtime?mode=video"
transport_mode = "neo_proxy"
protocol_adapter = "minicpm_realtime_v0"
```

`mode=video` 会把实时屏幕帧作为 `video_frames` 附在音频包上；如果只想纯语音，可以改成 `mode=audio`，adapter 会停止附带屏幕帧。

## 终端调试日志

默认会在 Neo 终端打印 live bridge 关键摘要：

- session 创建、统一事件 WebSocket 连接/断开。
- `/api/events` 收到的 live 事件。
- 浏览器页面回传的启动、采集、WebSocket、语音回合和错误日志。
- `/api/turn` 的输入摘要、音频/屏幕标记、模型回复摘要。
- `life_chatter` 同源 prompt 的长度、`<new_messages>`/`<life_runtime_context>` 预览。
- QQ/其他核心消息同步到 live 统一事件流的摘要。

可在插件配置 `[debug]` 中关闭或调节：`terminal_log_enabled`、`log_core_events`、`log_prompt_preview`、`log_client_events`、`stderr_mirror_enabled`、`preview_chars`。

## 外部服务器协议 v0

配置 `websocket_url` 后，live 页面始终发送同一套 Neo live v0 JSON 协议；如果 `transport_mode = browser_direct`，这些消息会由浏览器直接发给外部服务器；如果 `transport_mode = neo_proxy`，浏览器发给 Neo proxy，再由 Neo adapter 决定如何转给上游：

```json
{"type":"session.start","protocol":"neo-minicpm-live-v0","session_id":"...","stream_id":"live_voice_main","model_task_name":"live","capture":{}}
{"type":"context.snapshot","session_id":"...","timestamp":0,"context":{"life_runtime_context":"...","unified_events":[],"session_events":[]}}
{"type":"screen.frame","session_id":"...","timestamp":0,"width":1280,"height":720,"image":"data:image/jpeg;base64,..."}
{"type":"audio.chunk","session_id":"...","timestamp":0,"mime_type":"audio/webm;codecs=opus","data":"data:audio/webm;base64,..."}
{"type":"unified.event","session_id":"...","timestamp":0,"event":{"origin":"neo_core","source":"qq","event_type":"on_message_received","text":"..."}}
{"type":"text.input","session_id":"...","timestamp":0,"text":"..."}
{"type":"session.interrupt","session_id":"...","timestamp":0}
{"type":"session.stop","session_id":"...","timestamp":0}
```

在 `minicpm_realtime_v0` 下，浏览器会改用 16kHz mono float32 PCM 推送实时麦克风音频；旧的 `audio/webm` MediaRecorder 分片只用于 passthrough/其它外部服务。

外部服务器可以回传：

```json
{"type":"transcript","role":"assistant","text":"..."}
{"type":"partial","role":"assistant","text":"..."}
{"type":"final","role":"assistant","text":"..."}
{"type":"audio","text":"...","audio_url":"https://..."}
{"type":"audio","text":"...","mime_type":"audio/wav","audio_base64":"..."}
{"type":"audio","encoding":"pcm_float32","sample_rate":24000,"channels":1,"audio_base64":"..."}
{"type":"screen.summary","text":"当前屏幕里..."}
{"type":"error","message":"..."}
```

如果你直接使用官方 WebRTC Demo 前端，可以先只配置 `frontend_url`，从 Neo 页面跳转过去；等服务器接口稳定后再接 `websocket_url` 或 LiveKit token 适配。
