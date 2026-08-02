# 实时通话意识（Voice Live）

> 文档状态：权威架构文档，与代码同步至 2026-08-02。
> 代码位置：`plugins/voice_live/`。
> 验收证据：`docs/report/voice_live-commercial-rebuild-2026-08-02.md`。

## 1. 定位与不变量

Voice Live 是爱莉在实时语音场景中的独立意识实例，不是“语音输入 + 普通聊天 + TTS”的外壳。每次通话都拥有独立的 `ConsciousnessInstance`、场景、上下文、工具边界、事件账本和恢复检查点，同时通过 Life Engine 的 `WorldState` 与同一主体的其他场景保持连续性。

实现必须满足以下不变量：

- Provider 由配置显式选择；连接失败时直接报告失败，不得静默改用另一个模型或另一套认知架构。
- 浏览器永远拿不到上游 API Key；云端凭证只从服务端环境变量读取。
- 模型不能决定运行时身份。`scene_id`、`consciousness_instance_id` 和 `episode_id` 由可信运行时覆盖注入。
- 通话事件追加写入；默认不保存原始音频。
- 打断、断线、重复控制消息与资源清理必须幂等。
- OBS 只读观察会话，不能获得控制权。
- Elysium 进程只由用户手动启动；测试和插件不得自动拉起、重启或接管主进程生命周期。

## 2. 端到端架构

```text
浏览器麦克风（通常 48 kHz）
  -> AudioWorklet 采集、单声道重采样、20 ms PCM16 帧
  -> 同源短期一次性 ticket
  -> /voice-live/ws
  -> CallSession
       |- VoiceLiveConsciousnessManager
       |- ContextBridge
       |- VoiceToolBroker
       |- VoiceEpisodeStore
       `- 显式 Realtime Provider
            |- Qwen-Audio Realtime（当前云端生产路径）
            |- Qwen-Omni Realtime（获得模型权限后的升级路径）
            |- OpenAI Realtime（协议适配路径）
            `- MiniCPM-o 4.5（本地可控路径）
  <- 24 kHz PCM16 增量音频 + 转写 + 状态 + 指标
  <- 浏览器时钟调度播放、排队、清空与重连

/voice-live/observe
  -> 只读复制状态、转写和音频
  -> /voice-live/overlay
  -> OBS Browser Source
```

每个浏览器 WebSocket 对应一个 `CallSession`。`CallSession` 独占 Provider 和意识实例，负责连接、收发、打断、工具回写、持久化以及最终清理，不共享可变会话状态。

## 3. 会话与协议

### 3.1 生命周期

```text
CREATED -> CONNECTING -> ACTIVE -> STOPPING -> ENDED
                     \-> FAILED  -> STOPPING -> FAILED
```

启动顺序是：创建 episode，激活 `voice_live` 意识，生成系统上下文与工具 schema，记录配置尺寸，连接显式 Provider，收到上游确认后才向浏览器发送 `ready`。

失败路径会断开已经创建的 Provider、挂起意识实例、写入失败原因，并把会话置为 `FAILED`。停止操作可以重复调用，不会重复释放资源。

### 3.2 二进制音频协议

浏览器和 Elysium 之间使用 Voice Live protocol v1：二进制帧包含魔数、版本、方向、序列号、时间戳、采样率和 PCM16 payload。服务端拒绝旧序列号、非法方向、错误版本、声道或采样格式，必要时做单声道 PCM16 重采样。

- 上行：16 kHz、16-bit、mono PCM。
- 下行：24 kHz、16-bit、mono PCM。
- 浏览器采集帧：默认 20 ms。
- MiniCPM-o 上游聚合：默认 1000 ms，和官方全双工路径的输入节奏一致。

浏览器通过 Web Audio 时钟逐段预约播放，避免把每个小片段当成独立播放器造成爆音和间隙。收到 `playback.clear` 时清空尚未播放的队列。

### 3.3 打断

“现在听我说”和本地 barge-in 都先立即清空客户端播放，再通知 Provider。Qwen 适配器跟踪 `response.created` / `response.done`：

- 有活动响应时发送 `response.cancel`，并在已知音频 item 上发送 truncate；
- 没有活动响应时只切回倾听状态，不向上游发送非法 cancel；
- 重复空闲打断是幂等操作。

这避免 Qwen 在“当前没有活动响应”时因 `response.cancel` 返回 `invalid_value` 并关闭会话。

## 4. 实时通话意识

### 4.1 独立实例而非复制人格

每次通话创建 `voice_live_{episode_id}` 实例和同名场景流。它们是爱莉在当前通话场景中的运行态，不是另一个人格。激活和挂起通过 Life Engine 的真实 registry 完成；生产配置默认要求 Life Engine 已运行，不能悄悄退化成无意识的语音机器人。

`ContextBridge` 把以下信息组成 Provider instructions：

- 爱莉的身份、表达方式与当前通话约束；
- 当前意识实例和场景；
- `WorldState` 中允许感知的关系、话题、身体与其他场景状态；
- 当前 episode 的历史与恢复上下文；
- 用户配置的附加指令。

最终用户和助手转写进入统一生命事件流。临时 delta 只用于实时显示，不冒充最终记忆。

### 4.2 工具边界

`voice_live` 当前仅暴露：

```text
action-report_state
tool-inner_query
tool-fetch_chat_history
```

工具清单是上下文预算和权限边界，不是关键词路由规则。`VoiceToolBroker` 从全局组件注册表解析真实工具 schema，执行前加载对应插件，并把运行时身份参数覆盖为当前会话的可信值。模型即使伪造 `scene_id` 也不能改写其他场景。

`action-report_state(kind="scene")` 会定位精确场景并更新、持久化 `WorldState`；场景不存在时显式失败，不做模糊 kind/platform 回退。

### 4.3 可恢复事件账本

每个 episode 写入：

```text
runtime/consciousness/{instance_id}/episodes/{episode_id}/events.jsonl
runtime/consciousness/{instance_id}/episodes/{episode_id}/checkpoint.json
```

事件包括连接、Provider 配置尺寸、意识激活、最终转写、工具开始/完成、错误、打断、指标和会话结束。检查点记录当前状态、Provider、结束原因和累计指标。原始音频默认不持久化，只有显式打开 `observability.persist_audio` 才允许保存。

## 5. Provider 策略

### 5.1 当前默认：Qwen-Audio Realtime

当前百炼工作空间已实测可访问 `qwen-audio-3.0-realtime-plus`。适配器使用 `smart_turn`，支持 16 kHz PCM 输入、24 kHz PCM 输出、流式转写、语音回复和 Function Calling。Qwen 的嵌套 function schema、函数名字符集和工具结果二轮推理均在适配层完成。

它是当前最适合上线的默认路径：云端延迟稳定、中文语音自然、工具调用可用，且无需在本机长期占用 GPU。

### 5.2 升级目标：Qwen3.5-Omni Realtime

Qwen3.5-Omni Realtime 提供语义打断、多语种、语音控制、Function Calling 以及 WebRTC 路径。当前工作空间对该模型返回权限拒绝，因此不能把它伪装成已交付能力。获得 entitlement 后，只需显式修改 Provider 的 `model_name` 并重新做协议与实机验收。

生产阶段建议增加 WebRTC transport：浏览器到供应商的 RTP 音频可利用浏览器回声消除、降噪和抖动处理，同时把短期凭证、工具调用和意识上下文继续留在 Elysium 控制面。WebSocket 路径仍保留为服务端、录制和诊断通道。

### 5.3 本地路径：MiniCPM-o 4.5

本地路径使用 `llama.cpp-omni` 的 `llama-omni-server` 和 MiniCPM-o 4.5 GGUF。它提供端到端语音理解与语音生成，并保留未来连续音视频输入的空间。当前 Windows CUDA 实机已经跑通完整音频输入与有效 WAV 输出。

本地模型冷启动和首轮延迟显著高于云端，因此当前定位是：隐私模式、断网模式、供应商灾备、后续本地优化基线，而不是默认线上路径。严禁在云端失败时静默切换到本地模型；切换必须是用户或运维的显式配置决策。

### 5.4 社区方案结论

- Moshi 的双音频流和低延迟设计值得借鉴，但裸客户端缺少完整回声消除，官方 Windows 支持也不是当前主路径；不作为本项目默认 Provider。
- MoshiRAG 证明“交互前台持续说听、检索后台异步完成并回注”是降低工具等待感的正确方向。后续可把耗时工具分成异步结果事件，但不能用固定关键词替爱莉决定何时检索。
- Freeze-Omni 的 chunk-level 状态预测说明打断判断应是模型语义能力，而不是只靠固定能量阈值。
- MiniCPM-o 4.5 的连续音视频全双工能力适合作为未来本地多模态通话底座。

参考：

- [阿里云 Qwen-Audio 实时语音对话](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)
- [阿里云 Qwen-Omni 实时模型](https://help.aliyun.com/zh/model-studio/realtime)
- [Google Gemini Live API](https://ai.google.dev/gemini-api/docs/live-api)
- [MiniCPM-o 4.5](https://huggingface.co/openbmb/MiniCPM-o-4_5)
- [llama.cpp-omni](https://github.com/tc-mb/llama.cpp-omni)
- [Moshi](https://github.com/kyutai-labs/moshi)
- [MoshiRAG](https://github.com/kyutai-labs/moshi-rag)
- [Freeze-Omni](https://github.com/VITA-MLLM/Freeze-Omni)

## 6. 安全边界

浏览器先从 `POST /voice-live/ticket` 获取 HMAC 签名 ticket，再连接 `/voice-live/ws` 或 `/voice-live/observe`。ticket 有短 TTL、随机 nonce、签名校验且只能消费一次；接口同时校验同源 Host 或显式 Origin 白名单。

其他边界：

- API Key 配置只保存环境变量名，不保存凭证值。
- ticket secret 未配置时使用进程级随机值；生产多实例部署应显式配置共享 secret。
- 默认最多一个并发通话，并设置最大通话时长和空闲超时。
- 健康接口只输出 Provider 类型、模型名、会话摘要和观察者数量，不输出密钥或转写正文。
- OBS observer 使用同样的一次性 ticket 与 Origin 校验，只能接收广播。
- 插件树的测试会扫描并拒绝内联 `sk-` 和 `api_key="..."`。

用户曾在对话中发送过真实 API Key；它不在仓库和报告中。该凭证应在本轮结束后轮换。

## 7. 浏览器与 OBS

### 7.1 通话页

地址：`http://127.0.0.1:18000/voice-live/`

页面提供开始/结束、麦克风静音、主动打断、实时状态、上下行字节、RTT、转写和错误提示。浏览器断线时采用有界退避重连；恢复时携带 episode 标识，以延续持久化上下文而不是伪造一个新 episode。

麦克风必须由用户手势授权。部署到非 localhost 时，应使用 HTTPS，否则浏览器可能拒绝 `getUserMedia`。

### 7.2 OBS 透明叠加层

Browser Source 推荐配置：

```text
URL: http://127.0.0.1:18000/voice-live/overlay
Width: 1920
Height: 1080
Custom CSS: body { background-color: rgba(0, 0, 0, 0); overflow: hidden; }
Shutdown source when not visible: off
Refresh browser when scene becomes active: off
```

叠加层没有麦克风权限和控制按钮，仅显示连接状态、爱莉/用户转写及必要的视觉状态。没有通话时透明是正常行为。OBS 捕获游戏仍应使用 Game Capture；Voice Live overlay 作为独立 Browser Source 叠加，这样语音 UI 不依赖游戏窗口标题或渲染后端。

## 8. 配置示例

配置文件只写环境变量名：

```toml
[full_duplex]
provider_type = "qwen_realtime"
upstream_url = "wss://<workspace-host>/api-ws/v1/realtime"
api_key_env = "VOICE_LIVE_API_KEY"
model_name = "qwen-audio-3.0-realtime-plus"
voice = "longanqian"

[session]
require_life_engine = true
record_to_life = true
cross_scene_awareness = true

[observability]
persist_audio = false
```

运行前在启动 Elysium 的同一手工终端设置 `VOICE_LIVE_API_KEY`。不得把真实值写进 TOML、脚本、命令历史示例、前端代码或 Git。

本地模型启动脚本为 `plugins/voice_live/scripts/start_minicpm_omni.ps1`；它只启动本地 Omni 推理服务，不启动、不终止也不重启 Elysium。

## 9. 验收

单元、契约与安全测试：

```bash
.venv/bin/python -m pytest test/plugins/voice_live -q \
  --cov-reset --cov=plugins.voice_live --cov-report=term-missing --cov-fail-under=80

.venv/bin/python -m pytest \
  test/plugins/life_engine/test_agent_orchestration_contracts.py \
  test/plugins/life_engine/test_report_state_action.py \
  -q --no-cov
```

独立 Provider 音频验证使用 `scripts/e2e_realtime.py`；完整 Elysium 网关验证使用 `scripts/e2e_gateway.py`。验收不能只检查“连接成功”，还必须检查：输出音频时长、RMS、哈希、转写、首音频延迟、工具事件、可信场景 ID、意识最终挂起和事件账本。

Elysium 必须由用户手工启动。任何自动化验收需要实例重启时必须停止并请求用户确认，不能以“测试需要”为由自动拉起主进程。
