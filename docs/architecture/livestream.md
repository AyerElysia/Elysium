# AI 直播系统（Livestream）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/livestream/`（20 文件，2707 行）。
> 本文是 AI 直播插件的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

Livestream 是一个**商业级 AI 直播框架**：从平台弹幕采集、事件过滤、优先级调度、LLM 生成、TTS 合成到 Live2D 形象驱动，形成完整的"弹幕进 → 语音+形象出"管线。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                     LivestreamPlugin（插件入口）                       │
│  plugin.py — 注册 Service / Router / EventHandler                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌────────────┐    ┌──────────────────────────────────────────────┐ │
│  │  Platform  │    │              Pipeline（核心管线）              │ │
│  │  适配层    │───▶│  EventFilter → PriorityQueue → Scheduler    │ │
│  │            │    │       ↓                                      │ │
│  │  B站       │    │  LLMOrchestrator → 流式按句分割              │ │
│  │  (blivedm) │    │       ↓                                      │ │
│  │            │    │  ProactiveEngine（空闲主动行为）              │ │
│  └────────────┘    └──────────────┬───────────────────────────────┘ │
│                                   │                                 │
│  ┌────────────────────────────────▼───────────────────────────────┐ │
│  │                     Output（输出层）                             │ │
│  │  TTSQueue（按句合成 + FIFO 播放）                               │ │
│  │  AvatarController（Live2D 表情/口型驱动）                       │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Router（FastAPI WebSocket）+ static/（Web 前端：Live2D + 弹幕面板） │
├─────────────────────────────────────────────────────────────────────┤
│  consciousness.py — 意识实例管理（kind="livestream"）                 │
│  context_bridge.py — 与主意识上下文桥接                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 平台适配层（platform/）

| 文件 | 职责 |
|------|------|
| `base.py` | `BasePlatformAdapter` 抽象 + `PlatformEvent` 统一事件模型 |
| `bilibili.py` | B站适配器（blivedm WebSocket 直连） |
| `factory.py` | 工厂函数，按配置实例化适配器 |

### 2.1 支持的事件类型

- 弹幕消息（danmaku）
- 礼物消息（gift）
- 醒目留言 / Super Chat
- 进场消息（entry）
- 大航海（guard buy）
- 点赞（like）

### 2.2 连接方式

B站：`room_id` + 可选 `sessdata`，通过 blivedm 库建立 Web 端 WebSocket 长连接。

---

## 3. 核心管线（pipeline/）

### 3.1 EventFilter — 事件过滤器

过滤无意义弹幕（纯表情、重复、过短），合并高频事件，控制 LLM 调用频率。

### 3.2 PriorityEventQueue — 优先级队列

| 优先级 | 事件类型 |
|--------|----------|
| 最高 | Super Chat / 大航海 |
| 高 | 礼物 |
| 中 | 弹幕互动 |
| 低 | 进场欢迎 |

### 3.3 PipelineScheduler — 状态机调度器

```
idle ──▶ thinking ──▶ speaking ──▶ idle
              │                        ▲
              └── 高优先级打断 ────────┘
```

职责：
- 从优先级队列取任务
- 调用 LLM 编排器生成回复
- 将回复送入 TTS 队列
- 控制形象状态切换
- 空闲检测 → 触发 ProactiveEngine

### 3.4 LLMOrchestrator — LLM 编排器

- 通过 NexusAI 中转站（`localhost:3000/v1`）调用模型
- 滚动窗口管理互动历史（`max_context_turns × 2`）
- 流式输出 → 按句分割
- 输出约束：1-3 句，口语化，自然接话

### 3.5 ProactiveEngine — 主动行为引擎

空闲时自动触发：
- 随机话题闲聊
- 观众进场批量欢迎（聚合窗口）
- 定时话题切换
- 礼物即时感谢

---

## 4. 输出层（output/）

### 4.1 TTSQueue

- 按句接收 LLM 输出
- 调用本地 TTS 服务合成音频
- FIFO 播放队列 + 优先级插入
- 输出格式：PCM16/OGG → WebSocket 二进制帧
- 生成口型时间戳数据

### 4.2 AvatarController

- Live2D 模型表情映射
- 口型同步（viseme 驱动）
- 状态动画（idle / thinking / speaking）

---

## 5. 服务层

| 文件 | 职责 |
|------|------|
| `router.py` | FastAPI WebSocket 服务，推送音视频流到前端 |
| `consciousness.py` | 独立意识实例（kind="livestream"），隔离直播人格 |
| `context_bridge.py` | 与主意识（LifeChatter）的上下文桥接 |
| `event_handler.py` | 插件事件处理（启停、状态同步） |
| `config.py` | 全量配置定义（7 个配置节） |

---

## 6. 配置节一览

| 配置节 | 说明 |
|--------|------|
| `plugin` | 启用/自启 |
| `platform` | B站房间号、sessdata |
| `pipeline` | 响应频率、批量窗口、上下文轮数、LLM 超时 |
| `proactive` | 空闲超时、话题列表、欢迎开关 |
| `tts` | 语速、音色、分句符、TTS 端点 |
| `avatar` | Live2D 模型路径、表情映射 |
| `server` | 路由前缀、认证 token、心跳间隔 |

---

## 7. 文件索引

```
plugins/livestream/
├── __init__.py              # 包说明
├── plugin.py                # 插件入口
├── config.py                # 配置定义
├── consciousness.py         # 意识实例管理
├── context_bridge.py        # 主意识上下文桥接
├── event_handler.py         # 事件处理
├── router.py                # WebSocket 路由
├── platform/
│   ├── base.py              # 平台抽象 + 事件模型
│   ├── bilibili.py          # B站适配器
│   └── factory.py           # 工厂
├── pipeline/
│   ├── event_filter.py      # 事件过滤
│   ├── priority_queue.py    # 优先级队列
│   ├── scheduler.py         # 状态机调度器
│   ├── llm_orchestrator.py  # LLM 编排
│   └── proactive.py         # 主动行为引擎
└── output/
    ├── tts_queue.py         # TTS 播放队列
    └── avatar_controller.py # Live2D 形象控制
```
