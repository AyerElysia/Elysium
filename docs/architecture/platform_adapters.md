# 平台适配器（Platform Adapters）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/napcat_adapter/`（22 文件，5134 行）+ `plugins/feishu_adapter/`（6 文件，2166 行）+ `plugins/neko_surface/`（7 文件，1723 行）。
> 本文是平台适配器层的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

平台适配器是数字生命与外部世界沟通的**感官通道**：将各平台的消息协议统一为内部事件流，并将内部决策翻译为平台原生消息发出。当前支持 QQ（NapCat/OneBot 11）、飞书、N.E.K.O 展示面三个平台。

---

## 1. 适配器总览

| 适配器 | 平台 | 协议 | 代码量 | 特点 |
|--------|------|------|--------|------|
| NapcatAdapter | QQ | OneBot 11 (WebSocket) | 5134 行 | 60+ API、群聊/私聊、文件/图片/语音 |
| FeishuAdapter | 飞书 | REST + 事件订阅 | 2166 行 | 消息/群组/文件/通讯录 20 个操作 |
| NekoSurfaceAdapter | N.E.K.O | 自定义 WebSocket | 1723 行 | 展示面协议、多客户端、背压控制 |

---

## 2. NapCat 适配器（QQ）

### 2.1 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                  NapcatAdapterPlugin（插件入口）                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  NapcatAdapter（核心适配器，继承 BaseAdapter）             │    │
│  │                                                         │    │
│  │  client/ — API 客户端层                                  │    │
│  │    ├── base.py      — WebSocket 连接管理                 │    │
│  │    ├── message.py   — 消息收发（文本/图片/语音/文件）     │    │
│  │    ├── group.py     — 群管理（成员/禁言/公告）           │    │
│  │    ├── account.py   — 账号信息                           │    │
│  │    └── file.py      — 文件上传下载                       │    │
│  │                                                         │    │
│  │  events/ — 事件解析层                                    │    │
│  │    ├── router.py    — 事件路由分发                       │    │
│  │    ├── message.py   — 消息事件（私聊/群聊/群通知）       │    │
│  │    ├── notice.py    — 通知事件（入群/退群/禁言）         │    │
│  │    ├── request.py   — 请求事件（好友/入群申请）          │    │
│  │    └── meta.py      — 元事件（心跳/生命周期）            │    │
│  │                                                         │    │
│  │  outgoing/ — 出站层                                     │    │
│  │    ├── sender.py    — 统一消息发送                       │    │
│  │    └── commands.py  — 平台操作命令                       │    │
│  │                                                         │    │
│  │  utils/ — 工具层                                        │    │
│  │    ├── cache.py     — 消息/用户缓存                      │    │
│  │    ├── media.py     — 媒体文件处理                       │    │
│  │    └── constants.py — 常量定义                           │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 核心能力

- **消息收发**：文本、图片、语音、文件、表情包、回复、撤回
- **群管理**：成员列表、禁言、踢人（安全拦截）、公告、精华
- **好友操作**：好友列表、删除（安全拦截）、点赞
- **文件操作**：上传/下载群文件、图片 OCR
- **事件订阅**：消息、通知、请求、元事件全覆盖

### 2.3 安全机制

拦截不可逆操作：踢人、解散群、删好友等需要额外确认。

### 2.4 配置节

| 配置节 | 说明 |
|--------|------|
| `plugin` | 启用/自启 |
| `bot` | 机器人 QQ 号、昵称 |
| `napcat_server` | WebSocket 地址、端口、token |
| `features` | 功能开关（60+ 个 API 的细粒度控制） |
| `events` | 事件过滤规则 |
| `request_handling` | 好友/入群申请自动处理策略 |

---

## 3. 飞书适配器

### 3.1 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                  FeishuAdapterPlugin（插件入口）                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  FeishuAdapter（继承 BaseAdapter）                                │
│    ├── 消息收发（文本/富文本/图片/文件/卡片）                      │
│    ├── 群组管理（创建/成员/公告）                                 │
│    ├── 文件操作（上传/下载）                                      │
│    └── 通讯录查询                                                │
│                                                                 │
│  FeishuActionExecutor — 统一操作执行器（20 个操作）               │
│  FeishuRouter — HTTP 路由（事件回调 + 本地消息注入）              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 核心能力

- 消息：发送/回复/撤回、富文本、交互卡片
- 群组：创建群、管理成员、群公告
- 文件：上传/下载云空间文件
- 通讯录：按姓名/ID 查询用户信息
- 事件：消息接收、群变更、成员变更

### 3.3 配置节

| 配置节 | 说明 |
|--------|------|
| `plugin` | 启用/自启 |
| `app` | App ID、App Secret、验证 Token |
| `connection` | 事件订阅地址、加密密钥 |
| `bot` | 机器人名称、头像 |
| `behavior` | 行为策略（自动回复、群聊规则） |
| `identity` | 身份映射（飞书用户 ↔ 内部用户） |

---

## 4. N.E.K.O 展示面适配器

### 4.1 定位

N.E.K.O 是自定义的**展示面协议**（presentation surface），用于连接前端 UI（Web/桌面/移动端），实现多模态交互展示。

### 4.2 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                  NekoSurfacePlugin（插件入口）                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  NekoSurfaceGateway（WebSocket 网关）                             │
│    ├── HMAC 认证握手                                             │
│    ├── 多客户端管理（max 8）                                      │
│    ├── BoundedEventQueue（背压控制）                              │
│    ├── EventDeduplicator（事件去重）                              │
│    └── 双向事件流（Server → Client / Client → Server）           │
│                                                                 │
│  SurfaceProtocol（版本化线路协议 elysia.surface.v1）              │
│    ├── 客户端事件：hello / user.text / user.audio / ack / state  │
│    └── 服务端事件：ready / assistant.text / assistant.voice /    │
│                    assistant.media / playback.* / state           │
│                                                                 │
│  NekoSurfaceAdapter（继承 BaseAdapter）— 消息收发翻译             │
│  NekoSurfaceService（继承 BaseService）— 生命周期管理             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 协议特性

- 版本化：`SCHEMA_VERSION = "elysia.surface.v1"`
- 多模态：文本、音频（≤8MB/60s）、图片（≤8MB）
- 背压：有界队列（默认 128），满时丢弃低优先级事件
- 去重：LRU 去重器（容量 4096，TTL 600s）
- 安全：HMAC token 认证 + 握手超时

### 4.4 配置

通过环境变量配置：token、queue_size、handshake_timeout、max_clients、dedupe 参数等。

---

## 5. 统一操作工具

项目使用统一的 `tool-platform_action` 工具支持多平台操作：

```python
# 通过 platform 参数区分
platform="qq"     → NapCat 适配器（60+ WebSocket API）
platform="feishu" → 飞书适配器（REST API，20 个操作）
```

安全机制：拦截踢人、解散群、删好友等不可逆操作。

---

## 6. 文件索引

```
plugins/napcat_adapter/
├── plugin.py                # 插件入口 + NapcatAdapter 核心
├── config.py                # 配置定义（6 节）
├── client/
│   ├── base.py              # WebSocket 连接管理
│   ├── message.py           # 消息 API
│   ├── group.py             # 群管理 API
│   ├── account.py           # 账号 API
│   └── file.py              # 文件 API
├── events/
│   ├── router.py            # 事件路由
│   ├── message.py           # 消息事件
│   ├── notice.py            # 通知事件
│   ├── request.py           # 请求事件
│   └── meta.py              # 元事件
├── outgoing/
│   ├── sender.py            # 统一发送
│   └── commands.py          # 平台命令
└── utils/
    ├── cache.py             # 缓存
    ├── media.py             # 媒体处理
    └── constants.py         # 常量

plugins/feishu_adapter/
├── plugin.py                # 插件入口
├── adapter.py               # FeishuAdapter 核心
├── actions.py               # 操作执行器
├── router.py                # HTTP 路由
└── config.py                # 配置定义（6 节）

plugins/neko_surface/
├── plugin.py                # 插件入口
├── adapter.py               # NekoSurfaceAdapter
├── service.py               # Gateway + Service
├── protocol.py              # 线路协议定义
├── router.py                # HTTP 路由
└── event_handler.py         # 事件镜像
```
