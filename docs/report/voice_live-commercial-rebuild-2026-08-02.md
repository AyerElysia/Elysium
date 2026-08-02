# 爱莉实时通话意识：商业级重构、模型调研与全链路验收报告

> 日期：2026-08-02
> 范围：`plugins/voice_live/`、必要的 Life Engine 工具注册/清单、浏览器通话页、OBS 叠加层、本地与云端 Provider、测试与文档。
> 结论：核心架构和主要真实链路已经跑通，当前建议以 Qwen-Audio Realtime 作为可上线默认路径，以 MiniCPM-o 4.5 作为本地可控路径；Qwen3.5-Omni Realtime 等待当前百炼工作空间开通模型权限。最后一次空闲打断修复已通过协议测试，但由于 Elysium 必须由用户手工启动，修复后的浏览器点击复验需要用户下次手工启动实例后完成。
>
> 后续补充：Qwen 下行接入爱莉 Seed-VC 实时音色的实现、真实音频指标与质量边界见 `docs/report/voice-live-elysia-seedvc-integration-2026-08-02.md`。

## 1. 执行摘要

旧实现最大的问题不是“模型不够大”，而是产品链路没有形成可信闭环：文档和代码不一致、Provider 能力边界模糊、存在自动降级理念、意识实例与 Life Engine 的连接不够刚性、工具调用缺少可信运行时身份、浏览器播放与打断缺乏足够协议验证、OBS 只有概念没有真实捕获验证、测试深度偏单元级。

本次重构后的系统具备：

- 每次通话一个真实、独立、可挂起的 `ConsciousnessInstance`；
- 显式 Provider，失败时不静默更换模型；
- 云端 Qwen-Audio / Qwen-Omni、OpenAI Realtime、本地 MiniCPM-o 的统一 Provider 契约；
- 浏览器麦克风到 16 kHz PCM16 上行、24 kHz PCM16 流式下行的二进制协议；
- 支持浏览器静音、主动打断、播放队列清空、RTT 和流量指标；
- 工具 schema、工具结果二轮推理，以及由运行时覆盖注入的可信场景身份；
- 追加式 episode、checkpoint、最终转写、工具事件和可追溯失败原因；
- 一次性短期 ticket、同源/Origin 校验、并发和时长边界，API Key 不下发浏览器；
- 独立 OBS 透明叠加层和只读 observer WebSocket；
- 真实云端音频、真实本地模型、真实网关工具、真实浏览器麦克风和真实 OBS Browser Source 验证。

商业上线建议：

1. 当前默认使用 `qwen-audio-3.0-realtime-plus` + `smart_turn`；
2. 开通 Qwen3.5-Omni 权限后做 A/B，再决定是否升级为主路径；
3. MiniCPM-o 4.5 保持显式本地模式，不做云端故障时的隐式切换；
4. 下一阶段增加 WebRTC、声学回声消除、长期会话续接、多人房间与压力测试；
5. API 凭证已经在对话中暴露，应立即轮换。

## 2. 规范与约束

本次实现遵循仓库 `AGENTS.md`，重点落实：

- 没有以固定关键词、硬编码类别、数值阈值或规则表替爱莉做认知决定；
- 音频时长、连接超时、ticket TTL、并发数等阈值只服务于协议和资源安全；
- 不裁剪模型回复来伪造“短回答”；表达长度由实时模型和指令共同决定；
- 不把 Provider 失败包装成另一条“看起来可用”的认知路径；
- 意识实例通过明确 kind 和 manifest 注册，不依赖未知 kind 的兼容回退；
- 对话记忆和场景状态保留来源、episode 和时间线，不覆盖原始证据。

运维边界：Elysium 只能由用户手工启动。本次后期收到该约束后，没有对主进程执行 kill、TERM、重启、nohup 启动或自动拉起，也没有修改另一任务负责的生命周期文件。当前没有遗留的自动重启或健康检查辅助进程。

## 3. 社区与官方方案调研

### 3.1 Qwen-Audio Realtime

阿里云官方文档确认 Qwen-Audio 是 WebSocket 端到端实时语音模型，支持 `server_vad`、`smart_turn`、push-to-talk、Function Calling、上下文管理和高表现力语音。`smart_turn` 把声学与语义结合，无意义附和不应打断系统；输入为 16 kHz PCM，输出为流式音频与文本。

结论：在当前百炼工作空间实际可用，并且真实 Function Calling 跑通，是当前最稳妥的生产默认。

来源：[Qwen-Audio 实时语音对话](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-user-guides)

### 3.2 Qwen3.5-Omni Realtime

官方能力包括更强的实时多模态智能、Function Calling、语义打断、WebSocket/WebRTC、多语种识别与生成、语速/音量/情绪控制和声音复刻。WebRTC 音频走 RTP，浏览器可以利用更成熟的实时媒体处理。

实测结果：当前工作空间连接该模型时返回 WebSocket 1007 权限拒绝。因此本报告把它列为升级目标，不把文档能力冒充本机已交付能力。

来源：[Qwen-Omni 实时模型](https://help.aliyun.com/zh/model-studio/realtime)

### 3.3 Gemini Live 的产品体验

Gemini Live 的关键体验不是某一个 UI，而是低延迟双向音视频、原生音频推理、打断、函数调用、长会话压缩/恢复和客户端实时媒体处理的组合。

对 Elysium 的启示：意识和工具控制面必须留在服务端，音频媒体面应逐步 WebRTC 化；会话恢复必须保留 episode 连续性，不能只重连一个空的新 socket。

来源：[Gemini Live API](https://ai.google.dev/gemini-api/docs/live-api)、[Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)

### 3.4 MiniCPM-o 4.5 与 llama.cpp-omni

MiniCPM-o 4.5 是约 9B 的端到端多模态模型，官方描述支持连续音视频输入、并行文本/语音输出、全双工和主动交互。`llama.cpp-omni` 提供 Windows CUDA 可用的 GGUF 推理服务、参考音频和浏览器 demo 路径。

本机实测说明它可以作为真正的本地语音模型，而不是 STT + 文本 LLM + TTS 拼接。但当前冷启动约 76.7 秒、首音频约 6 秒，尚不及云端路径，后续需要常驻、量化/上下文、prefill 和流式调度优化。

来源：[MiniCPM-o 4.5](https://huggingface.co/openbmb/MiniCPM-o-4_5)、[MiniCPM-o 4.5 GGUF](https://huggingface.co/openbmb/MiniCPM-o-4_5-gguf)、[llama.cpp-omni](https://github.com/tc-mb/llama.cpp-omni)

### 3.5 Moshi、MoshiRAG 与 Freeze-Omni

Moshi 用双音频流建模真正的 full duplex，官方报告 L4 上实践延迟可低至约 200 ms。它的 Rust 生产栈和浏览器客户端值得借鉴，但裸客户端回声消除、Windows 运行支持和业务工具链并不满足本项目当前交付条件。

MoshiRAG 的价值在架构：前台持续实时交互，检索后台异步发生，结果随后以流的形式回注；这比阻塞整段语音等待工具更自然。未来可把耗时工具结果变成异步 Life Event，但由模型/主体决定是否调用，不能加入关键词触发。

Freeze-Omni 通过 chunk-level 状态预测决定“继续听、开始说、用户是否打断”，说明商业级打断不能只靠固定音量阈值。

来源：[Moshi](https://github.com/kyutai-labs/moshi)、[MoshiRAG](https://github.com/kyutai-labs/moshi-rag)、[Freeze-Omni](https://github.com/VITA-MLLM/Freeze-Omni)

## 4. 旧实现审计结论

### 4.1 文档漂移

旧权威文档仍宣称存在 Moshi 和 VAD -> LLM -> TTS 自动降级管线，而当前目标要求端到端 Omni、独立意识和显式失败。旧文档已经重写，避免后续开发继续沿错误方向扩展。

### 4.2 静默降级违背身份连续性

同一次“爱莉实时通话”如果因连接失败悄悄切到另一种模型和语音结构，语气、记忆、工具能力和延迟都会改变，用户却不知道。这不是容错，而是不可审计的身份漂移。本次实现要求 Provider 显式选择、失败显式暴露。

### 4.3 意识和工具没有形成可信闭环

工具参数以前完全来自模型，`scene_id` 可被幻觉或提示注入污染；`action-report_state` 对 scene 状态没有完整的精确更新路径。现在工具 broker 根据 schema 识别运行时参数，并覆盖为当前实例值；状态工具只接受精确注册场景。

### 4.4 Qwen 协议差异未被吸收

Qwen 的工具 schema 使用嵌套 `function`，函数名字符限制与 Elysium 的 `action-report_state` 命名不一致，Qwen-Audio 和 Qwen-Omni 的会话字段、turn detection 和工具确认也不同。适配器现在做显式协议翻译、可逆工具名映射、session acknowledgement 和初始化错误即时上抛。

### 4.5 浏览器播放与打断缺少真实状态

仅发送 cancel 不等于完成打断；客户端还必须立即丢弃排队音频，上游只应在活动响应存在时取消。真实浏览器验收发现了“空闲时点击打断导致 Qwen `invalid_value`”的问题，本次增加 `_response_active` 状态并做幂等修复。

## 5. 实现结果

### 5.1 插件发现与依赖

新增 `plugins/voice_live/manifest.json`，明确插件名、router、event handler 和对 `life_engine` 的依赖。增加静态测试，保证插件能够被 loader 发现且组件集合精确。

### 5.2 Life Engine 绑定

新增 `life_binding.py` 作为运行时绑定边界，避免在 Life Engine 尚未完成加载时因模块导入顺序造成错误。生产默认 `require_life_engine=true`；测试才能显式关闭。

每个 episode：

1. 注册 `voice_live_{episode_id}` 意识实例；
2. 注册对应 scene/stream；
3. 构造跨场景 WorldState 上下文；
4. 把最终转写写入 Life Event；
5. 工具调用携带可信 instance/scene/episode；
6. 会话结束后挂起实例并保存 checkpoint。

### 5.3 工具最小化

`voice_live` manifest 从五个工具收缩为三个：

```text
action-report_state
tool-inner_query
tool-fetch_chat_history
```

真实网关测试曾观察到模型在通话中错误选择通用聊天发送类 action。移除不适合该场景的工具，既降低 schema 预算，也避免重复对外表达。工具最小化不是限制爱莉意志，而是让当前语音场景只看见语义清晰、可审计的行动能力。

### 5.4 Provider

- `QwenRealtimeProvider`：支持 Qwen-Audio/Qwen-Omni 的协议差异、逐段 session update、工具 schema 翻译、函数名映射、工具结果回写、配置确认指标、错误即时传播、活动响应跟踪和幂等打断。
- `OpenAIRealtimeProvider`：保留标准 Realtime 适配路径和音频重采样契约。
- `MiniCPMOmniProvider`：对接本地原生 WebSocket，禁用不适合长时间 GPU 阻塞的 aiohttp heartbeat，完善初始化失败传播、全双工输入聚合和独立 E2E 脚本。
- `disabled`：用于明确关闭；不会自动选择其他 Provider。

### 5.5 浏览器和 OBS

通话页实现麦克风授权、48 kHz 到 16 kHz 重采样、20 ms 上行帧、24 kHz 播放调度、静音、打断、重连、episode 恢复、转写和指标。

OBS 叠加层通过 `/voice-live/observe` 只读接收广播。真实 OBS 32.2.1 portable 实例已创建名为“爱莉实时通话叠加层”的 Browser Source，尺寸 1920x1080，URL 为 `http://127.0.0.1:18000/voice-live/overlay`，透明背景设置生效。Elysium 停止时画面透明是预期行为。

## 6. 安全审计

### 6.1 凭证

- 用户提供的 API Key 只通过测试进程环境注入；
- 配置文件只记录 `VOICE_LIVE_API_KEY` 这类环境变量名；
- 仓库扫描测试拒绝 `sk-` 和内联 `api_key="..."`；
- 报告、Git diff、前端和运行事件中不包含凭证；
- 因凭证已出现在对话记录中，仍必须轮换。

### 6.2 浏览器接入

- ticket 使用 HMAC-SHA256；
- 随机 nonce、短 TTL、一次消费；
- ticket 接口和 WebSocket 同时校验同源 Host 或 Origin 白名单；
- ticket 响应禁止缓存；
- 控制通道和观察通道都需要 ticket；
- 设置最大并发、硬时长和空闲超时。

### 6.3 工具身份

`scene_id`、`consciousness_instance_id`、`episode_id` 不信任模型参数。Broker 在执行前读取当前意识运行态并覆盖注入。实机事件账本证明最终工具参数包含精确的当前 `voice_live_*` scene ID。

## 7. 全链路实机验证

### 7.1 本地 MiniCPM-o 4.5

环境：Windows CUDA、`llama-omni-server.exe`、MiniCPM-o 4.5 GGUF。

结果：

- 冷初始化：76.7 s；
- 输入完成到首音频：6046.4 ms；
- 输出：3.84 s，RMS 2237；
- 转写：“你好，是的，这条实时语音通话链路已经成功接通了。你可以开始讲话了。”；
- PCM payload SHA-256：`18a8d1bd12d4bd4e1c5d90bfbf9567a091e244bc0fba52231ffcb7086696b59a`；
- 证据音频：`minicpm-speech-output.wav`。

判断：真实有效音频，不是空 WAV、静音、mock 或仅 HTTP 200。

### 7.2 Qwen 文本触发语音

结果：

- 首音频：1027 ms；
- 输出：2.16 s，RMS 4996；
- 证据音频：`qwen-text-output.wav`。

用途：验证 Provider 会话、文本输入和 24 kHz 音频下行最小闭环。

### 7.3 Qwen 真实语音输入

结果：

- 输出：6.72 s，RMS 4226；
- 证据音频：`qwen-speech-output.wav`。

用途：验证真实 PCM 音频上行、服务端 turn detection、语音理解和语音回复。

### 7.4 Qwen-Audio 完整 Elysium 网关

结果：

- episode：`942069f03738`；
- 网关、意识、工具 broker、Provider 和事件持久化链路成功；
- 暴露出通用聊天 action 不适合 voice 场景，随后收缩工具 manifest。

这是一次有价值的失败导向验收：不是只证明 socket 可通，而是用真实模型行为修正了工具权限设计。

### 7.5 Qwen-Audio 可信场景工具

episode：`568a3bf5a648`。

结果：

- 输出：3.381 s、162300 bytes、RMS 3422；
- PCM payload SHA-256：`ddfb9557d9d139315afcee53b4c6a0c100e2aed712d75ec7e4f1c8a14d9d5a88`；
- 首音频总延迟：2441.6 ms；
- 输入完成到首音频：1733.9 ms；
- 转写：“验证成功啦，场景状态已更新♪”；
- 证据音频：`qwen-audio-trusted-context-output.wav`；
- `provider.configuration`：instructions 9840 chars / 16836 bytes，3 tools / 3829 bytes；
- `tool.started`：`action-report_state` 参数含当前精确 scene ID；
- `tool.completed`：`success=true`；
- 最终意识状态：suspended。

事件证据：

```text
runtime/consciousness/voice_live_568a3bf5a648/
  episodes/568a3bf5a648/events.jsonl
```

### 7.6 真实浏览器

已在真实浏览器打开 `http://127.0.0.1:18000/voice-live/` 并完成：

- 初始待机界面；
- 用户手势授权真实麦克风；
- 连接 Qwen-Audio；
- 建立 episode `c229d500...`；
- RTT 约 1 ms；
- 上行字节持续增长；
- 麦克风静音/恢复按钮实际生效；
- “现在听我说”实际清空播放并触发打断。

该测试发现空闲打断缺陷：上游当时没有活动响应，旧逻辑仍发送 `response.cancel`，Qwen 返回 “Conversation has no active response” 并断开。修复后增加了协议测试：空闲打断不发送上游事件；活动响应打断发送 cancel + truncate。

受“Elysium 只能手工启动”的明确运维策略约束，主进程退出后没有自动重启，所以修复后的真实浏览器再次点击尚未执行。该项不是隐藏失败：它是唯一明确记录的手工复验项。

### 7.7 OBS

真实 OBS 32.2.1 portable 已打开，场景中存在：

```text
Source: 爱莉实时通话叠加层
Type: Browser Source
URL: http://127.0.0.1:18000/voice-live/overlay
Resolution: 1920 x 1080
Background: transparent
```

OBS UI 和保存后的 scene JSON 均已核对。未启动 Elysium 时叠加层透明，证明它不会以错误页污染直播画面；下一次手工启动并通话时 observer 会自动显示实时内容。

## 8. 自动化测试结果

### 8.1 Voice Live 全量

```text
41 passed
coverage: 81.61%
statements: 1762
missed: 324
```

命令：

```bash
.venv/bin/python -m pytest test/plugins/voice_live -q \
  --cov-reset --cov=plugins.voice_live --cov-report=term-missing --cov-fail-under=80
```

`--cov-reset` 必须保留，否则仓库已有 coverage 配置会把全仓历史数据并入当前阈值，造成“测试全部通过但阈值按错误分母失败”的假阴性。

### 8.2 Life Engine 相关契约

```text
20 passed
```

覆盖工具注册、manifest、可信 scene 状态和 agent orchestration 合同。

### 8.3 重点故障测试

- ticket 篡改、过期和重复消费；
- Origin/Host 拒绝；
- 非法二进制帧与序列号；
- 插件 manifest 和 Life Engine 依赖；
- 插件树无内联 API Key；
- Qwen schema、函数名映射和初始化错误；
- Qwen-Audio `smart_turn` 契约；
- 空闲/活动响应两种打断；
- MiniCPM 初始化错误立即失败；
- MiniCPM 全双工音频形状；
- 意识创建、挂起、恢复和事件存储；
- 工具上下文覆盖和工具结果回写；
- OBS 静态资源和路由安全。

测试仅有 websockets legacy API deprecation warning，不影响当前功能，但建议后续升级到新 asyncio API。

## 9. 模型与产品路线建议

### P0：当前可上线

- Qwen-Audio Realtime Plus 作为默认；
- `smart_turn`；
- 单会话；
- 三个最小可信工具；
- 浏览器通话页 + OBS overlay；
- 追加式文本/状态审计，原始音频默认关闭；
- 上线前轮换 API Key；
- 用户手工启动 Elysium 后完成一次空闲打断回归。

### P1：接近 Gemini Live / 豆包电话体验

- 增加浏览器 WebRTC transport、AEC、noise suppression、自动增益；
- 以短期供应商 token 或 Elysium WebRTC relay 隔离长期凭证；
- 加入会话 resumption handle、上下文压缩和 120 分钟边界前的无感续接；
- 加入设备切换、输出设备选择、网络质量和抖动缓冲指标；
- 用真实双人串音、扬声器回灌和背景噪声做语义打断基准；
- 把耗时查询设计成异步工具结果回注，保持前台有自然 backchannel。

### P2：爱莉直播与多人房间

- 通话控制面与直播媒体面分离；
- WebRTC SFU/room 服务处理多人音频，不让每个观众直接连接模型；
- 主播、爱莉、嘉宾和观众使用明确身份与权限；
- 弹幕/语音/游戏分别成为场景事件，经过 WorldState 汇总，不把所有原始流直接塞进一个 prompt；
- OBS overlay 增加 speaking 状态、字幕、工具状态和安全的运营信息；
- 建立审核、速率限制、敏感操作确认、断线降噪和应急静音。

### P3：本地最强路径

- MiniCPM-o 常驻模型与热会话池；
- 测量 Q4/Q8/F16、context size、GPU layer、prefill 和音频 chunk 的质量/延迟曲线；
- 接入连续低帧率视觉，让实时通话意识能看见游戏画面和摄像头；
- 研究 MoshiRAG 式异步知识回注；
- 使用盲听 MOS、打断成功率、端到端首音频、连续 30/120 分钟稳定性作为门槛，而不是只看模型榜单。

## 10. 交付状态与已知边界

已完成：

- 架构重构、意识、上下文、工具、持久化、安全、Provider、浏览器和 OBS；
- Qwen-Audio 云端语音与工具真实链路；
- MiniCPM-o 4.5 本地真实音频链路；
- 41 个 Voice Live 测试、81.61% 覆盖率；
- 20 个 Life Engine 相关测试；
- 权威架构文档、意识实例文档、Life Engine README 和本报告；
- 精确 staging、提交和推送（提交信息由交付步骤回填到 Git 历史）。

需要外部条件：

- Qwen3.5-Omni Realtime：百炼工作空间尚无模型权限；
- 空闲打断修复后的浏览器点击复验：需要用户手工启动 Elysium；
- 真正多人直播：需要选定 WebRTC SFU/房间基础设施和平台合规策略；
- 本地 MiniCPM 首音频优化：需要继续做长期模型常驻与 GPU 参数实验。

这些边界不会触发隐式模型切换、自动拉起 Elysium 或未授权的主进程操作。

## 11. 验收清单

- [x] Provider 显式选择，无静默降级
- [x] API Key 不进入仓库、浏览器、日志或报告
- [x] 一次性 ticket 与 Origin 校验
- [x] 独立 `ConsciousnessInstance`
- [x] WorldState 跨场景上下文
- [x] 最终转写进入 Life Event
- [x] 工具 schema 与 Qwen Function Calling
- [x] 可信 scene/instance/episode 注入
- [x] 工具成功后语音二轮回复
- [x] 追加式 episode 与 checkpoint
- [x] Qwen-Audio 云端真实音频
- [x] MiniCPM-o 本地真实音频
- [x] 真实浏览器麦克风、静音和旧逻辑打断复现
- [x] 空闲打断代码修复与协议级回归
- [ ] 修复后真实浏览器空闲打断复验（等待用户手工启动 Elysium）
- [x] OBS Browser Source 实际创建并核对
- [x] Voice Live 覆盖率超过 80%
- [x] 文档同步
