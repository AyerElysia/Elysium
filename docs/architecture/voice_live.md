# 全双工语音通话（Voice Live）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/voice_live/`（18 文件，3095 行）。
> 本文是全双工语音通话插件的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

Voice Live 是**全双工实时语音通话**插件：支持 OpenAI Realtime / Moshi 等原生全双工 Provider，同时内置完整的降级管线（VAD → LLM → TTS），确保在任何 Provider 不可用时仍能提供流畅的语音对话体验。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    VoiceLivePlugin（插件入口）                         │
│  plugin.py — 注册 Service / Router / EventHandler                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              CallSession（会话管理）                            │   │
│  │  状态机：idle → connecting → active → ended                   │   │
│  │                                                               │   │
│  │  路径选择：                                                    │   │
│  │  ┌─────────────────────┐  ┌────────────────────────────────┐ │   │
│  │  │  全双工 Provider     │  │  降级管线 (DegradedPipeline)    │ │   │
│  │  │                     │  │                                │ │   │
│  │  │  • OpenAI Realtime  │  │  ServerVAD（语音活动检测）      │ │   │
│  │  │  • Moshi/PersonaPlex│  │       ↓                        │ │   │
│  │  │                     │  │  DegradedLLMClient（流式文本）  │ │   │
│  │  │  真全双工：          │  │       ↓                        │ │   │
│  │  │  同时收发音频流      │  │  TTSStreamer（切句+流式合成）   │ │   │
│  │  └─────────────────────┘  └────────────────────────────────┘ │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Router（WebSocket）— 客户端音频流收发 + 控制信令                     │
│  consciousness.py — 意识实例（kind="voice_live"）                     │
│  context_bridge.py — 与主意识上下文桥接                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 会话管理（session.py）

### 2.1 状态机

```
IDLE ──▶ CONNECTING ──▶ ACTIVE_FULL_DUPLEX ──▶ ENDED
                   └──▶ ACTIVE_DEGRADED ──────▶ ENDED
```

### 2.2 路径选择逻辑

1. 检查全双工 Provider 是否已配置且可用
2. 可用 → `ACTIVE_FULL_DUPLEX`（原生全双工）
3. 不可用 → `ACTIVE_DEGRADED`（降级管线）

每个 WebSocket 连接对应一个 `CallSession`，管理完整的通话生命周期。

---

## 3. 全双工 Provider（providers/）

### 3.1 抽象基类

`BaseRealtimeProvider` 定义统一接口：
- 连接/断开生命周期
- 音频流收发（`AudioDelta`）
- 打断（barge-in）支持
- 状态回调（`ProviderState`）
- 转写回调（`TranscriptEvent`）

### 3.2 Provider 状态

```
IDLE → CONNECTING → LISTENING ⇄ SPEAKING → CLOSED
                         │                      ▲
                         └── ERROR ─────────────┘
```

### 3.3 已实现的 Provider

| Provider | 协议 | 特点 |
|----------|------|------|
| `OpenAIRealtimeProvider` | WebSocket (OpenAI Realtime API) | 服务端 VAD、流式音频、打断 |
| `MoshiProvider` | 二进制 WebSocket (24kHz PCM16) | 真全双工、同时建模双方音频流 |

### 3.4 工厂

`providers/factory.py` — `create_provider()` 按配置实例化对应 Provider。

---

## 4. 降级管线（degraded/）

当全双工 Provider 不可用时，自动切换到降级管线：

```
用户音频 → ServerVAD（端点检测）→ 累积音频
    → DegradedLLMClient（MiMo 音频理解 + 流式文本）
    → 切句 → TTSStreamer（流式合成）→ 音频输出
```

### 4.1 ServerVAD

- 服务端语音活动检测
- 可配置静音阈值、最短语音时长
- 检测到语音结束 → 触发 LLM 请求

### 4.2 DegradedLLMClient

- 调用 MiMo 音频理解模型
- 流式文本输出
- 支持上下文注入（system prompt）

### 4.3 TTSStreamer

- 按句切分 LLM 输出
- 流式调用 TTS 服务
- 支持打断：用户开始说话时立即取消当前播放

### 4.4 管线状态

```
IDLE → LISTENING → THINKING → SPEAKING → IDLE
                                    │
                                    └── 打断 → LISTENING
```

---

## 5. 辅助模块

| 文件 | 职责 |
|------|------|
| `router.py` | WebSocket 路由，处理客户端音频帧和控制信令 |
| `consciousness.py` | 独立意识实例（kind="voice_live"），隔离通话人格 |
| `context_bridge.py` | 与主意识上下文桥接，注入 system prompt |
| `event_handler.py` | 插件事件处理（来电、挂断、状态同步） |
| `config.py` | 全量配置（8 个配置节） |

---

## 6. 配置节一览

| 配置节 | 说明 |
|--------|------|
| `plugin` | 启用/自启 |
| `server` | WebSocket 端口、路由前缀、认证 |
| `full_duplex` | Provider 选择、上游 URL、API Key、模型 |
| `degraded` | 降级管线开关、LLM 端点、TTS 端点 |
| `vad` | 静音阈值、最短语音、端点超时 |
| `session` | 最大时长、空闲超时、自动挂断 |
| `audio` | 采样率、位深、声道、编码格式 |

---

## 7. 文件索引

```
plugins/voice_live/
├── __init__.py              # 包说明
├── plugin.py                # 插件入口
├── config.py                # 配置定义（8 节）
├── session.py               # CallSession 会话管理
├── consciousness.py         # 意识实例管理
├── context_bridge.py        # 主意识上下文桥接
├── event_handler.py         # 事件处理
├── router.py                # WebSocket 路由
├── providers/
│   ├── base.py              # Provider 抽象基类
│   ├── factory.py           # 工厂函数
│   ├── openai_realtime.py   # OpenAI Realtime Provider
│   └── moshi.py             # Moshi/PersonaPlex Provider
└── degraded/
    ├── pipeline.py          # 降级管线编排器
    ├── server_vad.py        # 服务端 VAD
    ├── llm_client.py        # 降级 LLM 客户端
    └── tts_streamer.py      # 流式 TTS
```
