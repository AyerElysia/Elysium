# 阶段三：Elysium 应用后端接口导出开发步骤

> 文档状态：范围与决策已由汐汐确认，可按本文步骤实施
>
> 上位规划：[Elysium 离线优先共享后端重构计划](./Elysium离线优先共享后端重构计划.md)
>
> 本文是供后续 AI 直接执行的阶段三开发步骤，不是完成证明。文中的路由、schema、迁移和测试只有在代码落地并实际验证后才能标记为已完成。

## 1. 阶段定位

### 1.1 一句话定义

阶段三负责把 **Elysium（爱莉应用后端）已经拥有或应由其拥有的事件、命令、查询、状态和媒体能力，整理为统一、稳定、版本化、可鉴权、可恢复的接口**。独立应用被视为一个完整平台：独立应用前端依靠独立应用后端完整运行，独立应用后端可以在授权后调用阶段三导出的全部 Elysium 接口。

### 1.2 与阶段四的硬边界

阶段三不是独立应用后端开发。

| 边界            | 阶段三：Elysium 接口导出                             | 阶段四：独立应用后端                |
| ------------- | -------------------------------------------- | ------------------------- |
| 运行 owner      | Elysium 进程及其领域插件                             | 独立部署的应用服务                 |
| 数据 owner      | Life Event、Elysium 运行事实、插件领域状态、Elysium 可授权投影 | 应用用户、协作关系、应用会话、普通业务数据     |
| 主要职责          | 导出事件、命令、查询、媒体、健康和能力契约                        | 消费阶段三契约，提供应用业务 API 和应用数据库 |
| 是否直接依赖插件对象    | 网关内部可以通过明确领域 facade 调用                       | 禁止                        |
| 是否实现应用用户体系    | 否，只验证调用方身份与 Elysium 授权范围                     | 是                         |
| 是否实现普通业务 CRUD | 否                                            | 是                         |
| 是否改写爱莉主体语义    | 否                                            | 否                         |
|               |                                              |                           |
|               |                                              |                           |

以下内容不得混入阶段三：

- 独立应用的注册、登录、用户资料、好友或协作者关系；
- 独立应用自己的会话、页面、收藏、通知、偏好和普通 CRUD；
- 独立应用数据库 schema；
- 为前端展示便利而复制一套新的长期人格、记忆或世界状态；
- 绕过 Elysium 领域服务直接写插件 SQLite、JSON 或内存字典。

### 1.3 阶段三交付范围：用户层与管理层

本文不再按“全仓发现了什么”罗列候选接口，而按**是否能对应一个合理的前端页面或交互流程**决定是否纳入。只保留用户层或管理层可能使用的后端接口；没有可说明前端消费者、仅供内部模块互调、纯属未来泛化预留的能力不进入阶段三接口目录。

1. **两层共用基础接口**：鉴权、bootstrap、capability、Life Event、命令状态、媒体、实时订阅、错误、幂等和断线恢复。
2. **用户层接口**：面向日常使用者的聊天、图片／语音媒体、直播观看与互动、语音通话、狼人杀、Neko 展示面，以及被授权的个人历史和状态。
3. **管理层接口**：面向汐汐持有的全能管理员身份，提供运行总览、平台连接、聊天与群管理、直播控制、通话监督、桌游裁判、媒体资产、命令追踪、事件审计、记忆／世界／意识观察、计划与后台任务状态及 Surface 管理。

纳入规则：

- 能明确对应用户页面、管理页面、弹窗、实时面板或故障处理流程的接口，应写入本文；
- 高风险但管理前端可能需要的能力也应写入，并标为管理层；只有全能管理员或具有完整平台授权的独立应用后端可以调用，同时保留强审计、幂等和必要的二次确认；
- 前端只需要聚合结果时，不导出底层内部对象或任意 action 透传接口；
- 没有当前实现锚点但已有明确页面需求的，标为“需新增领域 facade／持久化”，不能伪造现成能力；
- STS2 泛化预留、任意终端／设备通道、任意 LLM 工具执行、原始 Mission 推理链等没有明确前端消费方式的内容，从阶段三交付范围排除。

### 1.4 前端页面与接口域总览

| 前端层级 | 可能页面                  | 需要的接口域                                 |
| ---- | --------------------- | -------------------------------------- |
| 用户层  | 会话列表、聊天窗口、媒体查看器       | chat、media、events、commands             |
| 用户层  | 直播间、互动区、字幕／舞台         | livestream、media、events                |
| 用户层  | 实时语音通话页               | voice-calls、transcripts、tickets        |
| 用户层  | 狼人杀大厅、房间、玩家私密操作、复盘    | tabletop、events                        |
| 用户层  | Neko 展示与交互页           | surfaces、tickets、events                |
| 管理层  | 系统总览、模块健康、同步与积压       | bootstrap、readiness、health、diagnostics |
| 管理层  | 平台／Adapter 连接和权限诊断    | integrations、capabilities、audit        |
| 管理层  | 消息、群组、成员、公告、申请管理      | chat administration                    |
| 管理层  | 直播控制台、OBS／舞台和场次历史     | livestream administration              |
| 管理层  | 通话监督、转写、错误与指标         | voice-call administration              |
| 管理层  | 桌游裁判台和房间恢复            | tabletop moderation                    |
| 管理层  | 媒体资产、失败任务、访问审计        | media administration                   |
| 管理层  | 意识窗口、Presence、世界与记忆观察 | consciousness、world、memory             |
| 管理层  | TODO、计划、自主执行和后台任务状态   | commitments、autonomy、jobs              |
| 管理层  | Surface 连接管理          | surfaces                               |
| 管理层  | 命令追踪、事件审计和安全审计        | commands、events、audit                  |

## 2. 工程不变量

后续 AI 在每个实施步骤中必须同时遵守以下不变量。

### 2.1 主体性与数据权威

- API 只导出已有事实、授权投影或明确外部命令，不替爱莉判断意义、价值、关系或行动选择。
- `event_type`、场景类型和观察内容使用开放技术字符串；不得用封闭枚举穷尽认知意义。
- 外部调用方不能直接创建、修改、删除或“纠正”爱莉的记忆、信念、自我叙事、内心独白和主体文件。
- 外部输入只能作为带 `actor`、`source`、`occurrence_id`、时间和授权范围的观察或信息事件进入。
- Life Event、Experience、记忆版本和领域事实事件只追加；可重建投影不得反向覆盖权威历史。
- Presence 是运行事实，不是爱莉的关系、情绪或信念。

### 2.2 可靠性与生命周期

- 命令受理和副作用成功必须分离。
- 同一幂等键重复提交同一内容返回同一命令；同一键不同内容返回冲突。
- 历史游标只能在完整交付后推进；历史缺口必须显式失败。
- Adapter 超时造成的投递结果不确定必须表示为 `delivery_unknown`，不得伪装成成功或失败，也不得盲目自动重发。
- SSE／WebSocket 断线不能影响权威事件写入；慢消费者不能阻塞 Elysium 主链。
- 每个订阅、后台任务、数据库连接和临时媒体对象必须有明确 owner、超时、取消和关闭路径。
- 不得为实施或验收自动启动、停止、重启 Elysium 或 NapCat；需要重启时先获得用户授权。

### 2.3 安全与隐私

- 默认拒绝；每个 query、event、command、media 操作都必须声明 scope。
- 私聊、语音、媒体、记忆原文、内心独白、工具参数和桌游隐藏身份不得默认公开。
- 前端不能提交任意本地路径或任意 URL 要求 Elysium 读取、下载或发送。
- 普通事件 JSON 只包含媒体 descriptor，不包含大型 base64、原始磁盘路径、凭据化 URL 或内联字节。
- 健康接口只输出定位所需技术状态，必须脱敏 URL query、token、cookie、Authorization、App Secret、用户原文和本地绝对路径。
- 现有飞书 `cors_origins=["*"]` 等局部策略不能复制到统一 API。

## 3. 当前实现基线与复用锚点

### 3.1 HTTP 与路由基座

- `src/core/transport/router/http_server.py`：现役 FastAPI 主服务器与子应用挂载能力。
- `src/core/components/base/router.py`：插件 Router 基类。
- `src/core/utils/security/`：现有安全工具，需要审计后决定复用或替换，不能仅因存在就视为满足阶段三鉴权。

统一接口建议挂载到 `/api/v1`，现有插件局部路由保留为兼容入口，阶段三不应一次性删除旧路由。

### 3.2 统一事件与离线同步

- `plugins/life_engine/service/event_bus.py`：追加式 Life Event 账本、稳定 sequence、occurrence、因果和关联字段、`RawEventGapError`。
- `src/kernel/sync/local_store.py`：阶段二本地同步状态和 Outbox 基座。
- `src/kernel/sync/mysql_ledger.py`：远程幂等账本与 consumer cursor 基座。
- `docs/architecture/Elysium离线同步内核.md`：阶段二实现合同。

阶段三应复用阶段二事件身份和同步状态，不再另造与 Life Event 平行的“前端事件真相”。

### 3.3 聊天与媒体

- `src/core/models/message.py`：`Message`、`MessageType`、`Message.to_dict()`。
- `src/core/models/media.py`：`MediaAttachment`、媒体 descriptor 和安全 source 规则。
- `src/core/transport/message_receive/receiver.py`：统一入站与 `ON_MESSAGE_RECEIVED`。
- `src/core/transport/message_send/message_sender.py`：发送前、Adapter 执行、历史持久化和 `ON_MESSAGE_DELIVERED`。
- `src/core/components/types.py`：`ON_MESSAGE_RECEIVED`、`ON_MESSAGE_SENT`、`ON_MESSAGE_DELIVERED`、notice 等事件名。
- `src/app/plugin_system/api/send_api.py`：内部文本、图片、表情、语音、视频、文件和自定义消息发送入口。
- `src/app/plugin_system/api/message_api.py`：内部历史查询。
- `src/app/plugin_system/api/media_api.py`：识别、保存媒体信息和媒体查询。
- `plugins/napcat_adapter/client/message.py`、`client/group.py`、`events/notice.py`：QQ／OneBot 扩展消息、戳戳、回应、已读、撤回、公告、群管理和通知。
- `plugins/feishu_adapter/actions.py`：飞书消息编辑／删除／置顶／回应、群组成员、图片文件能力。

### 3.4 直播

- `plugins/livestream/domain.py`：直播领域技术模型。
- `plugins/livestream/runtime.py`：直播 session、平台、导演、TTS、舞台和手动控制 owner。
- `plugins/livestream/ledger.py`：直播追加账本与 consumer cursor。
- `plugins/livestream/router.py`：现有 ticket、health、start、stop、interrupt、say 和舞台 WebSocket。
- `plugins/livestream/platform/base.py`、`bilibili.py`：`send_danmaku()` 与 B 站事件解析。

### 3.5 语音通话

- `plugins/voice_live/protocol.py`：版本化控制帧、状态和 PCM16 二进制帧。
- `plugins/voice_live/session.py`：单会话 owner、恢复、打断、音频、转写、工具和指标。
- `plugins/voice_live/router.py`：ticket、health、`/ws` 和 `/observe`。

### 3.6 桌游

- `plugins/werewolf_game/models.py`：狼人杀原始状态与隐藏信息。
- `plugins/werewolf_game/engine.py`：确定性规则和 player-view 约束。
- `plugins/werewolf_game/service.py`：当前群命令服务与内存游戏字典。

### 3.7 管理前端与展示前端边界

以下实现能够支撑明确的管理页、观察页或具身控制页，因此保留；它们不是因为“仓库里存在”而自动公开。

- `plugins/life_engine/service/consciousness.py`：意识窗口／Presence 管理页所需的 `ConsciousnessInstance`、状态、stream owner、lease 和 revision。
- `plugins/life_engine/service/presence_store.py`：Presence 健康、revision 冲突和事务 Outbox，供运行观察与故障诊断。
- `plugins/life_engine/service/world_projection.py`：世界观察页所需的来源、矛盾并存断言、change cursor、重建和健康。
- 已加载插件 manifest 与各领域 capability：作为“爱莉当前可用能力”展示目录的数据源；不能原样导出内部工具清单或提供任意执行。
- `plugins/life_engine/autonomy.py`：自主执行状态页所需的意向 occurrence、lease、结果和错误摘要。
- `plugins/life_engine/tools/todo_tools.py`：承诺／TODO 页面所需的可见性、进度和复发状态。
- `plugins/life_engine/tools/schedule_tools.py`：定时计划页面所需的计划登记与技术状态。
- `plugins/neko_surface/router.py`：Neko 展示页和管理页所需的 Surface 状态与认证 WebSocket。

未发现可对应当前前端页面、且没有稳定领域 owner 的 STS2 泛化入口、任意终端／设备通道等不列入阶段三接口目录。以后若产品明确新增页面，再通过独立变更补充，不能以本阶段“预留”为由提前暴露内部接口。

## 4. 目标接口架构

```text
FastAPI 主服务器
└── /api/v1
    ├── bootstrap / capabilities / readiness / health
    ├── events / subscriptions
    ├── commands
    ├── chat / media
    ├── livestream
    ├── voice-calls
    ├── tabletop
    ├── consciousness / world / memory（管理层）
    ├── commitments / autonomy / jobs（管理层）
    ├── surfaces（用户层＋管理层）
    ├── integrations / audit（管理层）
    └── diagnostics（管理层受限）
          ↓
    统一授权、schema、错误、幂等和审计
          ↓
    领域 Facade / Query Service / Command Dispatcher
          ↓
    现有 MessageSender、Life Event、插件 Runtime、Ledger、Registry
```

### 4.1 建议新增包

建议新增 `src/app/api/v1/`，而不是把公共契约散落到各插件 Router：

```text
src/app/api/v1/
├── router.py
├── dependencies.py
├── auth.py
├── errors.py
├── pagination.py
├── capabilities.py
├── events.py
├── commands.py
├── media.py
├── chat.py
├── livestream.py
├── voice_calls.py
├── tabletop.py
├── consciousness.py
├── world.py
├── memory.py
├── commitments.py
├── autonomy.py
├── jobs.py
├── surfaces.py
├── integrations.py
├── audit.py
└── schemas/
    ├── common.py
    ├── event.py
    ├── command.py
    ├── chat.py
    ├── media.py
    ├── livestream.py
    ├── voice_call.py
    ├── tabletop.py
    └── projection.py
```

具体文件可在实施时按现有代码风格合并，但职责不能重新耦合。

### 4.2 领域 Facade 规则

- Router 只负责认证、校验、调用、响应和协议升级。
- Facade 负责把稳定公共 schema 映射到当前内部对象。
- 领域执行必须走现有 owner；例如消息发送走 `MessageSender`，不能由 Router 直接调用平台 HTTP API。
- Adapter 特有动作通过明确 allowlist facade 暴露，禁止公共接口接收任意 `action_name` 并透传。
- 插件未加载、功能关闭和依赖故障分别返回 `disabled`、`unavailable`、`degraded`，不得都映射为 404 或空结果。

## 5. 通用协议契约

### 5.1 API 版本

- REST 前缀：`/api/v1`。
- 事件 envelope：`schema_version` 独立演进。
- WebSocket 子协议：每个实时领域保留协议名和版本，例如 `elysium.events.v1`、`elysium.voice-live.v1`。
- 向后不兼容变更必须进入 `/api/v2` 或新的子协议版本。
- OpenAPI 必须生成稳定 operation id；CI 检查 schema diff。

### 5.2 通用事件 envelope

```json
{
  "event_id": "evt_...",
  "sequence": 123,
  "origin_node_id": "node_...",
  "origin_sequence": 456,
  "occurred_at": "2026-08-03T17:00:00+08:00",
  "recorded_at": "2026-08-03T17:00:00.100000+08:00",
  "published_at": null,
  "actor": {"type": "platform_user", "id": "...", "display_name": "..."},
  "source": {"component": "feishu_adapter", "connection": "..."},
  "channel": "chat",
  "event_type": "chat.message.received",
  "schema_version": 1,
  "consciousness_instance_id": "chat_global",
  "stream_id": "...",
  "reply_target": {"type": "chat", "id": "..."},
  "correlation_id": "...",
  "causation_id": "...",
  "visibility": {"scope": "private", "audience": []},
  "payload_hash": "sha256:...",
  "payload": {}
}
```

要求：

- `event_type` 是开放命名空间字符串。
- 公共 `sequence` 必须有明确范围；若直接使用 Life Event sequence，文档写明为 ledger 全局位置。
- 对授权过滤后的消费者，cursor 表示已扫描的权威位置，不得因某事件不可见而让客户端误以为历史丢失。
- payload schema 按 `event_type + schema_version` 定义。
- payload 中的媒体使用 `media_id`、hash、MIME、size、width、height、duration 等 descriptor。

### 5.3 事件命名规范

统一使用 `<domain>.<entity>.<past-tense-fact>`，例如：

- `chat.message.received`
- `chat.message.delivery_confirmed`
- `chat.message.delivery_unknown`
- `chat.message.recalled`
- `chat.poke.received`
- `chat.announcement.published`
- `livestream.session.started`
- `livestream.platform.danmaku_received`
- `voice_call.transcript.updated`
- `tabletop.werewolf.vote_cast`
- `consciousness.instance.suspended`
- `world.observation.reported`

命令名使用动词：`chat.message.send`、`livestream.session.start`、`voice_call.interrupt`。

### 5.4 通用命令 envelope

```json
{
  "command_type": "chat.message.send",
  "schema_version": 1,
  "target": {},
  "payload": {},
  "correlation_id": "optional-client-correlation",
  "expected_revision": null
}
```

HTTP Header：

- 所有产生副作用的命令必须携带 `Idempotency-Key`。
- 所有请求应返回 `X-Request-ID`。
- 乐观并发更新携带 `If-Match` 或 body `expected_revision`，二者选定一种后全局统一。

命令状态：

- `accepted`：已耐久受理，尚未执行；
- `executing`：执行 owner 已领取；
- `succeeded`：领域定义的成功事实已耐久记录；
- `failed`：确认失败且不会自行变为成功；
- `delivery_unknown`：外部系统可能已执行，但没有可靠确认；
- `rejected`：鉴权、能力、schema、状态前置条件不满足；
- `cancelled`：在允许取消的领域中已停止；
- `expired`：命令未执行且超过明确有效期。

### 5.5 命令持久化

阶段三需新增耐久命令账本，不能只依赖 `send_api.py` 的 `bool` 返回值。至少保存：

```text
command_id
idempotency_key
request_hash
command_type
schema_version
actor_id
scope_snapshot
target_json
payload_json（敏感字段按策略加密或引用）
status
created_at / accepted_at / started_at / finished_at
result_event_id
error_code / safe_error_detail
correlation_id / causation_id
attempt_count
```

约束：

- `(actor_id, idempotency_key)` 唯一。
- 同键同 hash 返回既有 `command_id` 和当前状态。
- 同键不同 hash 返回 `409 idempotency_conflict`。
- 命令状态迁移与结果事件应通过事务 Outbox 或等价机制关联。
- 不为 `delivery_unknown` 自动创建新命令。

### 5.6 通用错误响应

```json
{
  "error": {
    "code": "history_gap",
    "message": "可安全展示的说明",
    "request_id": "req_...",
    "retryable": false,
    "details": {},
    "recovery": {"action": "restart_from_cursor", "cursor": "..."}
  }
}
```

基础错误码至少包括：

- `unauthenticated`、`forbidden`、`scope_required`；
- `validation_failed`、`unsupported_media_type`、`payload_too_large`；
- `capability_disabled`、`component_unavailable`、`component_degraded`；
- `resource_not_found`、`state_conflict`、`revision_conflict`；
- `idempotency_conflict`、`command_not_cancellable`；
- `history_gap`、`cursor_invalid`、`cursor_expired`；
- `rate_limited`、`upstream_timeout`、`delivery_unknown`；
- `media_not_ready`、`media_access_denied`、`media_integrity_failed`；
- `private_view_required`、`actor_not_in_game`。

## 6. 两层共用：身份、基础与发现接口

### 6.1 调用身份与前端会话

阶段三不建立业务用户账号体系，但前端必须能建立受限的 Elysium 调用会话、知道当前身份与权限，并安全退出。业务用户资料、注册、密码找回和社交关系仍属于阶段四。

| 方法     | 路径                                      | 用途                                 | Scope／前置条件                                               |
| ------ | --------------------------------------- | ---------------------------------- | -------------------------------------------------------- |
| POST   | `/api/v1/auth/sessions`                 | 使用受信本机 bootstrap、服务凭据或部署规定方式换取短时会话 | 一次性 bootstrap challenge 或有效 service credential；不接受匿名长期登录 |
| GET    | `/api/v1/auth/me`                       | 当前调用身份、角色、scope、资源授权摘要和会话到期时间      | 任意有效短时会话                                                 |
| POST   | `/api/v1/auth/sessions/current:refresh` | 在允许的刷新窗口内轮换短时凭据                    | 当前会话具有 refresh grant                                     |
| DELETE | `/api/v1/auth/sessions/current`         | 注销当前会话并撤销 refresh 能力               | 当前有效会话；注销操作本身保持幂等                                        |
| POST   | `/api/v1/auth/ws-tickets`               | 为指定资源和子协议生成短时、单次 WebSocket ticket  | `auth:ticket`，并同时校验目标资源对应 scope                          |

要求：

- 不接受 URL query 长期 token；cookie 模式必须使用 HttpOnly、Secure、SameSite 和 CSRF 防护；bearer 模式不得落入浏览器长期明文存储。
- `auth/me` 不返回业务用户资料，只返回 Elysium 识别到的 caller identity 和授权投影。
- 管理前端与用户前端使用不同 audience；管理端只有一个全能管理员角色，普通会话不能在客户端自行“切换”为管理员。
- 独立应用后端使用单独 platform service audience，可申请全部阶段三 scope；每个实际请求仍需通过认证、capability 和工程合同校验。
- 本机 bootstrap 必须绑定 Origin、安装实例、短 TTL 和一次性挑战，不能把 localhost 视为天然可信。

### 6.2 基础发现接口

| 方法  | 路径                     | 用途                       | Scope                  |
| --- | ---------------------- | ------------------------ | ---------------------- |
| GET | `/api/v1/bootstrap`    | API 版本、节点、调用身份、基础模块状态    | `system:read`          |
| GET | `/api/v1/capabilities` | 模块和动作能力 manifest         | `capabilities:read`    |
| GET | `/api/v1/readiness`    | 启动完成度、依赖、同步 backlog、降级原因 | `system:read`          |
| GET | `/api/v1/health`       | 快速只读存活状态                 | 可配置最小公开或 `system:read` |
| GET | `/api/v1/openapi.json` | 当前授权无关的技术 schema         | 部署策略决定                 |

### 6.3 Capability schema

每个模块至少返回：

```json
{
  "module": "chat",
  "available": true,
  "state": "ready",
  "contract_version": "1.0",
  "provider": "feishu",
  "features": {
    "message.send.text": {"supported": true, "scope": "chat:write"},
    "message.recall": {"supported": true, "scope": "chat:moderate"},
    "poke.send": {"supported": false, "reason": "provider_unsupported"}
  },
  "degraded_reason": null
}
```

不得把 `plugins/life_engine/service/tool_manifests.py` 的 LLM 工具清单原样作为前端 capability。两者含义不同：前者是意识上下文预算与工具暴露边界，后者是外部调用合同。可以引用同一领域事实，但需要独立公共 manifest。独立应用后端可调用全部阶段三接口，仍只以该公共 capability 为准，不能借平台身份透传内部工具。

### 6.4 Readiness 必备字段

- API 和 Elysium 版本；
- 当前 node id；
- Life Event ledger 状态；
- 阶段二同步状态：`disabled`／`ready`／`degraded`／`failed`、backlog、最后成功时间、安全错误摘要；
- 聊天 Adapter 列表及连接状态；
- 直播、语音、桌游、Surface 和场景状态；
- command dispatcher backlog；
- 数据迁移版本；
- 不包含消息原文、媒体路径、凭据和完整异常堆栈。

## 7. 两层共用：Life Event 历史与实时订阅

### 7.1 接口目录

| 方法   | 路径                                     | 用途                          |
| ---- | -------------------------------------- | --------------------------- |
| GET  | `/api/v1/events`                       | cursor 历史分页                 |
| GET  | `/api/v1/events/{event_id}`            | 读取单个授权事件                    |
| GET  | `/api/v1/events/stream`                | SSE 实时流与断点恢复                |
| WS   | `/api/v1/events/ws`                    | 需要双向 ack／动态订阅时使用            |
| POST | `/api/v1/event-subscriptions/validate` | 预检 filter 与 scope，不创建业务用户订阅 |

### 7.2 历史查询参数

- `cursor`：不透明、签名、版本化；
- `limit`：默认 100，硬上限 500；
- `event_type`：允许多个技术前缀，不能用于认知分类；
- `channel`、`stream_id`、`source_instance_id`；
- `occurred_after`、`occurred_before`；
- `include_payload`：仅授权后生效；
- `projection`：`summary` 或 `full`，summary 必须保留原事件继续读取链接。

禁止 offset 分页。

### 7.3 SSE 契约

- `id:` 使用可恢复 cursor，不直接假设 event id 连续。
- 支持标准 `Last-Event-ID`，也可接受 `cursor` query；两者同时提供但不一致时返回显式错误。
- 首次连接先补历史，再切换实时 tail；切换边界必须无丢失、允许幂等重复。
- 定期发送不推进业务 cursor 的 heartbeat。
- 权威账本出现 gap 时发送结构化错误并关闭，不能跳到尾部。
- 单连接队列有上限；慢消费者超限后返回最后安全 cursor 并断开。
- 客户端断线不会推进服务端 durable cursor，除非明确实现消费者 ack 身份。

### 7.4 与现有 SSE 的关系

`plugins/life_engine/memory/router.py:/api/events` 是进程内 queue 和 snapshot 流，不能直接宣称满足阶段三。实施方式：

1. 保留旧入口作为诊断兼容；
2. 新 `/api/v1/events/stream` 以耐久 Life Event／远程共享账本为源；
3. 旧页面逐步迁移到新流；
4. 验证完成前不删除旧路由。

## 8. 两层共用：命令查询与取消

| 方法   | 路径                                     | 用途                                  |
| ---- | -------------------------------------- | ----------------------------------- |
| POST | `/api/v1/commands`                     | 可选通用命令入口；只接收 allowlist command type |
| GET  | `/api/v1/commands/{command_id}`        | 查询状态、结果事件和安全错误                      |
| POST | `/api/v1/commands/{command_id}:cancel` | 取消支持取消且未完成的命令                       |
| GET  | `/api/v1/commands`                     | 按 actor、状态、类型和 cursor 查询，管理员受限      |

建议领域路由（如 `/chat/messages:send`）内部都创建统一 command 记录；通用 `/commands` 不是任意内部工具调用器。

## 9. 用户层：聊天模块

### 9.1 查询接口

| 方法  | 路径                                               | 用途                              |
| --- | ------------------------------------------------ | ------------------------------- |
| GET | `/api/v1/chat/streams`                           | 授权会话流列表与摘要                      |
| GET | `/api/v1/chat/streams/{stream_id}`               | 流元数据、平台和能力                      |
| GET | `/api/v1/chat/streams/{stream_id}/messages`      | cursor 历史消息                     |
| GET | `/api/v1/chat/messages/{message_id}`             | 单消息及 parts／attachments          |
| GET | `/api/v1/chat/messages/{message_id}/receipts`    | 投递、已读和平台回执                      |
| GET | `/api/v1/chat/streams/{stream_id}/members`       | 群成员／会话成员，provider capability 控制 |
| GET | `/api/v1/chat/streams/{stream_id}/announcements` | 公告历史，provider capability 控制     |
| GET | `/api/v1/chat/streams/{stream_id}/files`         | 群文件或会话文件，provider capability 控制 |

消息历史以现有 `message_api.py`／历史库为查询源，但对外使用稳定 cursor、统一 message schema 和授权过滤。

### 9.2 发送和消息动作

| 方法     | 路径                                                | 命令类型                     | 用户层权限边界                            |
| ------ | ------------------------------------------------- | ------------------------ | ---------------------------------- |
| POST   | `/api/v1/chat/messages:send`                      | `chat.message.send`      | 向已授权 stream 发送文本或规范化 message parts |
| POST   | `/api/v1/chat/messages/{id}:reply`                | `chat.message.reply`     | 显式引用当前 actor 可见的原消息                |
| POST   | `/api/v1/chat/messages/{id}:edit`                 | `chat.message.edit`      | 仅编辑当前 actor 自己发送且平台允许编辑的消息         |
| POST   | `/api/v1/chat/messages/{id}:recall`               | `chat.message.recall`    | 仅撤回当前 actor 自己发送且仍在平台时限内的消息        |
| POST   | `/api/v1/chat/messages/{id}/reactions`            | `chat.reaction.add`      | 添加当前 actor 的回应                     |
| DELETE | `/api/v1/chat/messages/{id}/reactions/{reaction}` | `chat.reaction.remove`   | 删除当前 actor 自己的回应                   |
| POST   | `/api/v1/chat/messages/{id}:mark-read`            | `chat.message.mark_read` | provider 支持时更新当前 actor 的已读状态       |
| POST   | `/api/v1/chat/messages:forward`                   | `chat.message.forward`   | 转发当前 actor 有权读取的消息                 |
| POST   | `/api/v1/chat/streams/{stream_id}/poke`           | `chat.poke.send`         | 向允许互动的目标发送戳戳，不模拟为文本                |

公告发布／删除、跨用户撤回、消息置顶／精华和成员管理不属于普通用户命令，统一使用第 15 节管理接口。所有副作用均要求 `Idempotency-Key`；平台不支持时返回 `capability_disabled`，不能自动降级为发送文本。

### 9.3 发送消息 payload

统一使用 message parts：

```json
{
  "stream_id": "...",
  "reply_to": "optional-message-id",
  "parts": [
    {"type": "text", "text": "..."},
    {"type": "image", "media_id": "media_...", "alt": "..."},
    {"type": "voice", "media_id": "media_..."},
    {"type": "file", "media_id": "media_...", "file_name": "..."}
  ],
  "client_message_id": "optional-stable-client-id"
}
```

约束：

- parts 类型至少支持 `text`、`image`、`voice`、`video`、`file`、`emoji`；位置和平台自定义类型作为 capability 扩展。
- 禁止提交本地文件路径、裸二进制和任意 URL。
- 发送图片、语音和文件前必须获得已授权且状态为 ready 的 `media_id`。
- Router 不直接调用 Feishu／NapCat client；由 chat command facade 映射到 `Message` 和 `MessageSender`。

### 9.4 聊天事实事件目录

必须桥接并耐久化：

- `chat.message.received`
- `chat.message.send_requested`
- `chat.message.send_accepted`
- `chat.message.delivery_confirmed`
- `chat.message.delivery_failed`
- `chat.message.delivery_unknown`
- `chat.message.edited`
- `chat.message.recalled`
- `chat.message.read`
- `chat.reaction.added`／`removed`
- `chat.poke.received`／`sent`
- `chat.announcement.published`／`deleted`
- `chat.member.joined`／`left`／`updated`
- `chat.member.muted`／`unmuted`
- `chat.admin.changed`
- `chat.file.uploaded`
- `chat.message.pinned`／`unpinned`
- `chat.request.received`／`resolved`
- `chat.adapter.connected`／`disconnected`／`degraded`

NapCat notice 审计还发现 `friend_add`、好友／群撤回、群名片、群文件、表情回应、精华、头衔、资料点赞、输入状态和 bot offline。阶段三至少要为这些提供开放的 provider notice 映射；没有稳定跨平台语义的事件可以命名为 `chat.provider_notice.received` 并保留 `provider_kind`，不能丢弃原始事实，也不能强行伪造成通用类别。

### 9.5 发送状态的真实边界

- `ON_MESSAGE_SENT` 是发送前事件，可能被拦截，不是平台成功事实。
- Adapter 调用完成且发送历史已持久化后产生的 `ON_MESSAGE_DELIVERED` 才可映射为确认事件。
- 若平台 API 已调用但响应超时，命令进入 `delivery_unknown`。
- 平台返回 message id 时保存为 provider receipt；没有返回时不编造。

## 10. 两层共用：媒体模块

### 10.1 接口目录

| 方法   | 路径                                           | 用途                             |
| ---- | -------------------------------------------- | ------------------------------ |
| POST | `/api/v1/media/uploads`                      | 创建受控上传会话                       |
| PUT  | `/api/v1/media/uploads/{upload_id}`          | 上传二进制；可按部署改为分片                 |
| POST | `/api/v1/media/uploads/{upload_id}:complete` | 校验 hash／大小／MIME 后生成 `media_id` |
| GET  | `/api/v1/media/{media_id}`                   | 获取安全 descriptor 和处理状态          |
| GET  | `/api/v1/media/{media_id}/content`           | 鉴权下载或短时签名地址                    |
| POST | `/api/v1/media/{media_id}:save`              | 将已授权媒体保存到 Elysium 受管媒体区        |
| POST | `/api/v1/media/{media_id}:recognize`         | 请求图片／语音等识别，能力控制                |
| GET  | `/api/v1/media/{media_id}/derivatives`       | 缩略图、波形、转写等派生资源                 |

### 10.2 “保存图片”的准确语义

“保存图片”不能只写一条数据库元数据。命令成功至少证明：

1. 调用方有权读取源媒体；
2. 媒体内容已进入受管目录或对象存储；
3. 内容 hash、大小和 MIME 已验证；
4. 元数据已耐久记录；
5. 生成稳定 `media_id`；
6. 发出 `media.object.saved` 事件；
7. 不暴露绝对路径。

若只完成下载但元数据写入失败，命令失败并由 owner 清理孤儿临时文件；若元数据已提交但派生识别失败，媒体保持 ready，识别任务单独 failed。

### 10.3 媒体安全

- MIME allowlist 与文件头嗅探同时检查。
- 分类型大小上限、像素上限、时长上限、压缩炸弹防护。
- 临时上传有 TTL 和 owner；清理不触及未知文件。
- 下载只允许已登记 `media_id`，禁止 path traversal。
- 远程抓取默认禁用；若以后启用，必须有 SSRF 防护、域名／协议 allowlist、DNS rebinding 防护和响应大小上限。
- 下载响应支持 Range、ETag、Cache-Control，但私密媒体不得进入共享缓存。
- 事件只携带 descriptor，不携带字节。

### 10.4 媒体事件

- `media.upload.created`
- `media.upload.completed`
- `media.upload.failed`
- `media.object.saved`
- `media.object.ready`
- `media.recognition.requested`
- `media.recognition.completed`
- `media.recognition.failed`
- `media.derivative.created`
- `media.access.denied` 仅进入安全审计，不向普通订阅泄露目标细节。

## 11. 用户层与管理层：直播模块

### 11.1 查询与控制接口

| 方法   | 路径                                                | 用途                | 层级                             |
| ---- | ------------------------------------------------- | ----------------- | ------------------------------ |
| GET  | `/api/v1/livestream/status`                       | session、平台和安全公开状态 | 用户层；管理层获得额外导演、TTS、舞台、OBS 和降级字段 |
| GET  | `/api/v1/livestream/sessions`                     | cursor 历史场次       | 用户层读取公开摘要；管理层读取完整技术摘要          |
| GET  | `/api/v1/livestream/sessions/{session_id}`        | 场次摘要              | 按 scope 投影                     |
| GET  | `/api/v1/livestream/sessions/{session_id}/events` | 直播账本 cursor 历史    | 按事件 visibility 和 scope 过滤      |
| POST | `/api/v1/livestream/session:start`                | 手工开始              | 管理层专属                          |
| POST | `/api/v1/livestream/session:stop`                 | 手工停止              | 管理层专属                          |
| POST | `/api/v1/livestream/session:interrupt`            | 打断当前表演／播放         | 管理层专属                          |
| POST | `/api/v1/livestream/speech:request`               | 操作者手工说话           | 管理层专属                          |
| POST | `/api/v1/livestream/danmaku:send`                 | 发送平台弹幕            | 用户层互动或管理层运营；分别限流和审计            |
| WS   | `/api/v1/livestream/stage/ws`                     | 舞台状态、计划、字幕、播放回执   | 用户层只读；管理层／舞台客户端按独立 scope 回执    |

### 11.2 直播事件目录

现有账本事实全部导出，并统一命名映射：

- `livestream.session.started`／`resumed`／`stopped`
- `livestream.platform.danmaku_received`
- `livestream.platform.danmaku_sent`
- `livestream.platform.gift_received`
- `livestream.platform.super_chat_received`
- `livestream.platform.guard_received`
- `livestream.platform.enter_received`
- `livestream.platform.like_received`
- `livestream.director.decision_recorded`
- `livestream.performance.planned`
- `livestream.performance.started`
- `livestream.tts.synthesized`
- `livestream.playback.dispatched`
- `livestream.playback.receipt_recorded`
- `livestream.operator.speech_requested`
- `livestream.interrupted`
- `livestream.platform.degraded`

`PlatformEvent.kind` 保持开放字符串；新增平台事件无需先修改全局封闭枚举。

### 11.3 关键约束

- start／stop 保持手工授权，不实现自动开播或自动拉起 Elysium。
- `send_danmaku()` 已存在于平台 Adapter，但当前缺少统一公开路由；实施时由 Livestream Runtime 或新增 facade 持有调用，不能 Router 直接抓取 Adapter 私有对象。
- 舞台 WebSocket 的播放回执是外部副作用证据，必须带 performance id、dispatch id 和幂等身份。
- OBS／舞台观察者默认只读；操作者命令与观察者 token／scope 分离。
- 直播聊天和普通聊天共享统一主体历史，但保持各自私有滚动上下文，不复制 payload chain。

## 12. 用户层与管理层：语音通话模块

### 12.1 REST 控制面

| 方法   | 路径                                          | 用途                           | 层级与权限                                                                |
| ---- | ------------------------------------------- | ---------------------------- | -------------------------------------------------------------------- |
| POST | `/api/v1/voice-calls`                       | 创建通话和一次性连接凭证                 | 用户层 `voice_call:operate`，只能创建调用者被授权参与的通话                             |
| GET  | `/api/v1/voice-calls/{call_id}`             | 会话状态和安全指标                    | 参与者 `voice_call:read`；管理层使用第 16.2 节受限技术投影                            |
| POST | `/api/v1/voice-calls/{call_id}:resume`      | 恢复允许恢复的会话                    | 当前参与者 `voice_call:operate`                                           |
| POST | `/api/v1/voice-calls/{call_id}:interrupt`   | 清除播放并中断当前回复                  | 当前参与者 `voice_call:operate`；监督者走管理接口                                  |
| POST | `/api/v1/voice-calls/{call_id}:end`         | 结束通话                         | 当前参与者 `voice_call:operate`；监督者强制结束走管理接口                              |
| POST | `/api/v1/voice-calls/{call_id}/text`        | 向实时会话注入文本                    | 当前参与者 `voice_call:operate`                                           |
| GET  | `/api/v1/voice-calls/{call_id}/transcripts` | 授权转写历史                       | 参与者 `voice_call:read`，按 transcript visibility 过滤                     |
| POST | `/api/v1/voice-calls/{call_id}/tickets`     | 生成短时、单次、绑定 scope 的 WS ticket | participant 使用 `voice_call:operate`；observer 使用 `voice_call:observe` |
| WS   | `/api/v1/voice-calls/{call_id}/ws`          | 双向 PCM16 与控制事件               | participant 专属                                                       |
| WS   | `/api/v1/voice-calls/{call_id}/observe`     | 只读字幕／状态观察                    | 被授权 observer；管理监督走 `/api/v1/admin/.../observe`                       |

### 12.2 协议复用

保留 `plugins/voice_live/protocol.py` 当前的版本化协议和二进制头：

```python
AUDIO_MAGIC = b"VL1\0"
AUDIO_HEADER = struct.Struct("<4sIII")
```

控制帧至少保留 `start`、`interrupt`、`text`、`stop`、`ping`；服务端事件保留 `ready`、`state`、`transcript`、`playback.clear`、`metrics`、`error`、`ended`、`pong`。阶段三只统一鉴权、路由、错误和生命周期，不把音频改成 JSON/base64。

### 12.3 状态契约

会话状态：`created`、`connecting`、`active`、`interrupting`、`stopping`、`ended`、`failed`。

Provider 状态：`idle`、`connecting`、`listening`、`thinking`、`speaking`、`interrupted`、`error`、`closed`。

这些是技术状态，可使用稳定枚举。不得从 Provider 状态推断爱莉情绪或意图。

### 12.4 语音事件

- `voice_call.created`
- `voice_call.connected`
- `voice_call.resumed`
- `voice_call.state_changed`
- `voice_call.audio_input_received`（默认不携原始音频）
- `voice_call.audio_output_dispatched`
- `voice_call.transcript.partial`
- `voice_call.transcript.final`
- `voice_call.playback_cleared`
- `voice_call.interrupted`
- `voice_call.provider_state_changed`
- `voice_call.metrics_recorded`
- `voice_call.failed`
- `voice_call.ended`

### 12.5 隐私和恢复

- 通话 ticket 单次、短 TTL、绑定 call id、Origin 和 scope。
- 观察者不能发送音频或控制命令。
- transcript 默认私密；partial transcript 可只存在瞬态流，final transcript 是否进入权威 Life Event 必须遵循现有会话合同并显式标注。
- 原始通话音频明确不长期保存，不提供历史原始录音查询或下载接口；只保留协议运行所需的瞬态缓冲，并按会话生命周期及时释放。
- 当前 `_sessions` 是进程内状态；阶段三必须明确“可恢复”的耐久元数据边界，不能仅因 API 可重连就宣称跨进程恢复完成。

## 13. 用户层与管理层：桌游模块

### 13.1 当前产品边界

当前实际实现是狼人杀。公共路径使用可扩展 `tabletop` 域，但 capability 必须明确只声明 `werewolf`，不能假装已支持其他桌游。

### 13.2 接口目录

| 方法   | 路径                                         | 用途                 | 层级与权限                               |
| ---- | ------------------------------------------ | ------------------ | ----------------------------------- |
| GET  | `/api/v1/tabletop/games`                   | 已支持游戏和规则版本         | 用户层 `tabletop:read`                 |
| POST | `/api/v1/tabletop/rooms`                   | 创建房间               | 用户层 `tabletop:play`                 |
| GET  | `/api/v1/tabletop/rooms/{room_id}`         | 授权房间视图             | 玩家／观众按身份获得投影；不返回 ModeratorView      |
| POST | `/api/v1/tabletop/rooms/{room_id}:join`    | 加入                 | 当前 actor 的 `tabletop:play`          |
| POST | `/api/v1/tabletop/rooms/{room_id}:leave`   | 退出                 | 当前 actor 的 `tabletop:play`          |
| POST | `/api/v1/tabletop/rooms/{room_id}:start`   | 开始场次               | 房主或规则授权角色；不是全局管理权限                  |
| POST | `/api/v1/tabletop/rooms/{room_id}:end`     | 正常结束场次             | 房主或规则授权角色；异常强制结束走管理接口               |
| GET  | `/api/v1/tabletop/rooms/{room_id}/events`  | 授权事件历史             | `tabletop:read`，按玩家身份过滤私密事件         |
| GET  | `/api/v1/tabletop/rooms/{room_id}/view`    | 按当前 actor 生成玩家私有视图 | 房间参与者；服务端按 actor 生成，不接受伪造 player id |
| POST | `/api/v1/tabletop/rooms/{room_id}/actions` | 提交具体游戏动作           | 当前玩家 `tabletop:play`，动作必须符合阶段与角色权限  |
| GET  | `/api/v1/tabletop/rooms/{room_id}/replay`  | 游戏结束后的授权复盘         | `tabletop:read`，仅按规则允许的披露范围         |
| WS   | `/api/v1/tabletop/rooms/{room_id}/ws`      | 房间实时事件             | 与 REST 相同的 actor 与事件可见性             |

### 13.3 狼人杀动作

动作 schema 使用 `action_type` + version，至少覆盖当前 service 已实现的：

- 创建、加入、退出、开始、结束；
- 状态查看、发言、下一位、投票；
- 自爆；
- 竞选、退选、警长投票、移交、撕警徽；
- 遗言、跳过、猎人开枪；
- 夜间杀人、验人、救人、毒人、守护；
- 复盘。

动作校验和规则执行必须调用 `WerewolfEngine`，不能在 Router 复制规则。

### 13.4 公共视图与私人视图

禁止序列化原始 `GameState`。至少建立：

- `RoomPublicView`：公开玩家、公开阶段、发言顺序、公开死亡、公开投票和公开事件；
- `PlayerPrivateView`：在 PublicView 基础上加入该 actor 有权知道的角色、行动选项、查验结果或狼队友；
- `ModeratorView`：仅审计／裁判 scope，访问也必须写审计；
- `ReplayView`：仅场次结束且规则允许后披露。

任何隐藏信息必须由 engine 的 player-view helper 或新增领域 projection 生成，不得由 API 层通过字段删减临时拼装。

### 13.5 耐久化缺口

当前 `plugin._werewolf_games`／service games 是进程内字典。阶段三实施前必须选择并评审耐久方案：

1. 追加式 game event ledger 为权威；
2. 房间 snapshot 为可重建或带 revision 的恢复点；
3. action command 以 action id／幂等键去重；
4. 重启后恢复房间、当前阶段和待处理动作；
5. 同一 action id 不同内容显式冲突；
6. 私密事件按 actor 授权过滤。

阶段三的新桌游 API 只管理通过新 API 创建的场次，不迁移当前进程内尚未结束的旧房间。旧房间继续由原有群命令生命周期处理至结束；不得把旧内存状态猜测性写入新 ledger。未完成上述耐久项前，新桌游 API 只能标记为 `experimental`，不得宣称生产可恢复。

## 14. 管理层：系统总览、集成与审计

管理前端需要看见 Elysium 是否可用、哪个模块异常、命令是否积压、平台为什么断线，以及高风险操作由谁发起；它不需要直接读取插件对象、配置文件或数据库表。

### 14.1 系统总览与安全诊断

| 方法   | 路径                                        | 管理页面用途                                         |
| ---- | ----------------------------------------- | ---------------------------------------------- |
| GET  | `/api/v1/admin/overview`                  | 聚合系统、同步、事件、命令、媒体和领域模块状态                        |
| GET  | `/api/v1/admin/components`                | 组件 enabled／loaded／ready／degraded／failed 状态     |
| GET  | `/api/v1/admin/components/{component_id}` | 单组件安全详情、owner、最后成功和积压                          |
| GET  | `/api/v1/admin/metrics`                   | 经脱敏和聚合的延迟、错误率、队列深度和连接数                         |
| GET  | `/api/v1/admin/incidents`                 | 当前与历史故障摘要、开始／恢复时间和关联组件                         |
| GET  | `/api/v1/admin/audit-events`              | 高敏读取、外部副作用和管理操作审计历史                            |
| GET  | `/api/v1/admin/audit-events/{audit_id}`   | 单条审计详情；可关联跳转对应高敏领域对象，但审计记录本身不复制密钥或私人原文         |
| GET  | `/api/v1/admin/logs`                      | 结构化、脱敏、可按 component／level／request id／时间查询的有限日志 |
| GET  | `/api/v1/admin/sync`                      | 本地／远程同步模式、backlog、cursor、最后成功和降级原因             |
| POST | `/api/v1/admin/sync:retry`                | 请求同步 owner 重试可安全重试的待处理项，不跳游标                   |

`admin/overview` 是面向页面的聚合查询，不允许前端为了拼总览并发读取十几个内部 health。健康查询必须只读，不能自动修复、重连、重建或启动组件。日志接口不允许任意文件读取、tail 本地路径或下载完整原始日志，只读取受管结构化日志投影并执行脱敏、分页和保留期限制。

### 14.2 管理会话与受控设置

| 方法     | 路径                                                 | 管理页面用途                                        |
| ------ | -------------------------------------------------- | --------------------------------------------- |
| GET    | `/api/v1/admin/auth/sessions`                      | 当前管理／用户调用会话、安全摘要和最后活动                         |
| DELETE | `/api/v1/admin/auth/sessions/{session_id}`         | 撤销指定前端会话或泄露凭据                                 |
| GET    | `/api/v1/admin/credentials`                        | 独立应用平台等受信服务凭据的 id、用途、scope、创建／到期和最后使用时间       |
| POST   | `/api/v1/admin/credentials`                        | 创建一次性显示 secret 的服务凭据；平台凭据可授予全部阶段三 scope       |
| POST   | `/api/v1/admin/credentials/{credential_id}:rotate` | 轮换凭据并保留明确过渡窗口                                 |
| DELETE | `/api/v1/admin/credentials/{credential_id}`        | 撤销凭据，不删除审计历史                                  |
| GET    | `/api/v1/admin/settings`                           | 前端可管理的 allowlist 设置及 schema、来源、revision、是否需重启 |
| PATCH  | `/api/v1/admin/settings`                           | 按 allowlist 与 expected revision 更新非密钥设置       |
| POST   | `/api/v1/admin/settings:validate`                  | 只验证候选设置，不应用、不重启                               |

阶段三的 settings 只包含经明确登记、能安全在线修改或供下次手工启动生效的 Elysium 运行设置，例如媒体上限、保留期、前端 Origin allowlist、限流和模块展示偏好。密钥使用专用 secret 管理方式；普通 settings API 不读取或回显 secret。禁止任意 TOML／JSON 路径编辑、环境变量写入、插件私有配置透传和修改后自动重启。

### 14.3 平台与 Adapter 管理

| 方法   | 路径                                       | 管理页面用途                           |
| ---- | ---------------------------------------- | -------------------------------- |
| GET  | `/api/v1/admin/integrations`             | 飞书、NapCat、直播平台、语音 Provider 等连接列表 |
| GET  | `/api/v1/admin/integrations/{id}`        | capability、权限缺口、安全配置摘要、连接状态      |
| GET  | `/api/v1/admin/integrations/{id}/events` | 连接、断线、降级、恢复和限流历史                 |
| POST | `/api/v1/admin/integrations/{id}:test`   | 执行无副作用或最小副作用的连通性／权限检查            |

管理前端不得读取或回显 App Secret、token、cookie、完整 URL query 和本机绝对路径。阶段三不提供 integration reconnect，也不提供任意配置键写入、任意插件方法调用、Elysium／NapCat 启停或插件热重载通用接口；连接恢复继续由现有明确 owner 的内部生命周期处理，管理端只观察状态、事件并执行安全测试。

### 14.4 命令追踪与作业状态

| 方法   | 路径                                     | 管理页面用途                 |
| ---- | -------------------------------------- | ---------------------- |
| GET  | `/api/v1/commands`                     | 按类型、actor、目标、状态和时间查询命令 |
| GET  | `/api/v1/commands/{command_id}`        | 查看状态迁移、结果事件和安全错误       |
| POST | `/api/v1/commands/{command_id}:cancel` | 取消明确支持取消且尚未完成的命令       |
| GET  | `/api/v1/admin/jobs`                   | 媒体识别、投影重建、恢复等后台技术作业    |
| GET  | `/api/v1/admin/jobs/{job_id}`          | 作业进度、owner、重试和安全错误     |
| POST | `/api/v1/admin/jobs/{job_id}:cancel`   | 取消支持取消的技术作业            |
| POST | `/api/v1/admin/jobs/{job_id}:retry`    | 仅对明确可幂等重试的失败作业重试       |

后台作业接口不返回 chain-of-thought、模型隐藏推理、完整工具参数、凭据或私人上下文。不能对 `delivery_unknown` 的外部发送命令提供普通 retry。

## 15. 管理层：聊天、联系人和群管理

用户层使用第 9 节聊天接口；管理层在相同稳定消息模型上增加跨会话检索、成员管理、内容管理和平台请求处理。

### 15.1 管理查询

| 方法  | 路径                                                     | 用途                          |
| --- | ------------------------------------------------------ | --------------------------- |
| GET | `/api/v1/admin/chat/streams`                           | 跨平台会话列表、类型、活跃度和连接状态         |
| GET | `/api/v1/admin/chat/messages`                          | 按平台、stream、sender、时间和消息类型检索 |
| GET | `/api/v1/admin/chat/streams/{stream_id}/members`       | 成员、角色、禁言和加入状态               |
| GET | `/api/v1/admin/chat/streams/{stream_id}/announcements` | 公告列表和发布状态                   |
| GET | `/api/v1/admin/chat/streams/{stream_id}/files`         | 群文件／会话文件 descriptor         |
| GET | `/api/v1/admin/chat/requests`                          | 好友申请、加群申请等待处理请求             |
| GET | `/api/v1/admin/chat/moderation-events`                 | 撤回、禁言、踢人、角色变化和处理审计          |

全能管理员可以读取全部聊天流、私聊原文和成员资料，用于统一管理与排障；所有高敏读取必须写审计。普通用户与独立应用的非管理员调用仍按参与者和资源范围过滤。

### 15.2 管理命令

| 方法     | 路径                                                                    | 命令类型                        |
| ------ | --------------------------------------------------------------------- | --------------------------- |
| POST   | `/api/v1/admin/chat/streams/{stream_id}/members/{member_id}:mute`     | `chat.member.mute`          |
| POST   | `/api/v1/admin/chat/streams/{stream_id}/members/{member_id}:unmute`   | `chat.member.unmute`        |
| POST   | `/api/v1/admin/chat/streams/{stream_id}/members/{member_id}:remove`   | `chat.member.remove`        |
| POST   | `/api/v1/admin/chat/streams/{stream_id}/members/{member_id}:set-role` | `chat.member.set_role`      |
| POST   | `/api/v1/admin/chat/requests/{request_id}:approve`                    | `chat.request.approve`      |
| POST   | `/api/v1/admin/chat/requests/{request_id}:reject`                     | `chat.request.reject`       |
| POST   | `/api/v1/admin/chat/messages/{message_id}:recall`                     | `chat.message.recall`       |
| POST   | `/api/v1/admin/chat/streams/{stream_id}/announcements`                | `chat.announcement.publish` |
| DELETE | `/api/v1/admin/chat/streams/{stream_id}/announcements/{id}`           | `chat.announcement.delete`  |
| POST   | `/api/v1/admin/chat/messages/{message_id}:pin`                        | `chat.message.pin`          |
| POST   | `/api/v1/admin/chat/messages/{message_id}:unpin`                      | `chat.message.unpin`        |

所有管理命令必须检查 provider capability、当前操作者权限、目标资源、幂等键和预期 revision。踢人、禁言、删除公告、处理申请和跨用户撤回需要管理端二次确认，但二次确认属于前端交互；后端仍必须独立鉴权，不能信任一个 `confirmed=true` 就放行。

## 16. 管理层：直播、通话、桌游与媒体

### 16.1 直播控制台

第 11 节的 start、stop、interrupt、speech request 和 danmaku send 全部属于 `livestream:operate` 管理命令；普通观看前端只拥有 `livestream:read` 和经授权的互动能力。管理页面还需要：

- 场次历史和当前平台连接；
- OBS／舞台／TTS／播放队列状态；
- 当前表演、可打断点和播放回执；
- 弹幕、礼物、SC、舰长、点赞和入场事件过滤；
- 降级、断线、恢复、速率限制和安全错误；
- 操作者命令及结果审计。

不得提供“自动开播”或“启动 Elysium”接口。

### 16.2 通话监督台

| 方法   | 路径                                                | 用途                          |
| ---- | ------------------------------------------------- | --------------------------- |
| GET  | `/api/v1/admin/voice-calls`                       | 当前与历史通话、参与者范围和状态            |
| GET  | `/api/v1/admin/voice-calls/{call_id}`             | Provider、延迟、错误和恢复摘要         |
| GET  | `/api/v1/admin/voice-calls/{call_id}/transcripts` | 经授权的 final 转写；partial 默认不留存 |
| POST | `/api/v1/admin/voice-calls/{call_id}:interrupt`   | 紧急打断播放和生成                   |
| POST | `/api/v1/admin/voice-calls/{call_id}:end`         | 强制结束当前通话                    |
| WS   | `/api/v1/admin/voice-calls/{call_id}/observe`     | 只读字幕、状态和指标                  |

监督者不能通过 observe 通道注入音频或文本。原始音频确定不保存，因此不规划“下载历史原始通话录音”接口；若未来改变该决定，必须作为新的独立需求补充同意、保留期和访问审计合同。

### 16.3 狼人杀裁判台

管理层保留 `ModeratorView`，并增加：

- `GET /api/v1/admin/tabletop/rooms`：所有房间和恢复状态；
- `GET /api/v1/admin/tabletop/rooms/{room_id}/moderator-view`：带审计的裁判视图；
- `GET /api/v1/admin/tabletop/rooms/{room_id}/integrity`：事件、snapshot 和阶段一致性；
- `POST /api/v1/admin/tabletop/rooms/{room_id}:pause`：暂停接受新动作；
- `POST /api/v1/admin/tabletop/rooms/{room_id}:resume`：恢复动作；
- `POST /api/v1/admin/tabletop/rooms/{room_id}:end`：异常终止并记录原因；
- `POST /api/v1/admin/tabletop/rooms/{room_id}:recover`：从权威 ledger 重建 projection。

管理端不能直接修改玩家角色、夜间结果、投票或历史事件；纠错必须通过明确的领域补偿事件，且是否允许补偿要由狼人杀规则合同定义。

### 16.4 媒体资产管理

| 方法   | 路径                                             | 用途                       |
| ---- | ---------------------------------------------- | ------------------------ |
| GET  | `/api/v1/admin/media`                          | 按类型、owner、状态、来源和时间查询媒体   |
| GET  | `/api/v1/admin/media/{media_id}`               | descriptor、引用、派生资源和完整性状态 |
| GET  | `/api/v1/admin/media/{media_id}/references`    | 哪些消息、事件或任务引用该媒体          |
| GET  | `/api/v1/admin/media/{media_id}/access-events` | 访问拒绝和高敏下载审计              |
| POST | `/api/v1/admin/media/{media_id}:verify`        | 重新校验 hash、MIME 和存储完整性    |
| POST | `/api/v1/admin/media/{media_id}:recognize`     | 重新提交允许幂等的识别作业            |
| POST | `/api/v1/admin/media/{media_id}:quarantine`    | 隔离确认不安全的媒体，禁止继续分发        |
| POST | `/api/v1/admin/media/{media_id}:restore`       | 从隔离恢复，需重新校验和审计           |
| GET  | `/api/v1/admin/media/cleanup-candidates`       | 仅列出无引用临时对象和过期上传候选        |

阶段三不提供删除任意受管媒体的通用接口。权威消息／事件引用的媒体不能由管理页面直接删除；真正清理只能针对可证明无引用的临时对象，并由后续明确的数据保留策略控制。

## 17. 管理层：意识、世界与记忆观察

这些页面用于理解运行事实和可授权投影，不允许管理者冒充爱莉修改主体语义。

### 17.1 意识实例与 Presence

| 方法   | 路径                                                            | 用途                              |
| ---- | ------------------------------------------------------------- | ------------------------------- |
| GET  | `/api/v1/admin/consciousness/instances`                       | 运行窗口、kind、状态、stream 和 lease 摘要  |
| GET  | `/api/v1/admin/consciousness/instances/{instance_id}`         | revision、owner、最后活动和安全 metadata |
| GET  | `/api/v1/admin/consciousness/streams/{stream_id}/owner`       | 查询唯一 active owner               |
| GET  | `/api/v1/admin/consciousness/health`                          | registry、outbox、lease 和冲突状态     |
| POST | `/api/v1/admin/consciousness/instances/{instance_id}:suspend` | 暂停允许运维控制的场景窗口                   |
| POST | `/api/v1/admin/consciousness/instances/{instance_id}:resume`  | 恢复已暂停场景窗口                       |
| POST | `/api/v1/admin/consciousness/instances/{instance_id}:drain`   | 停止接收新工作并等待安全释放                  |

register／terminate 继续由场景生命周期 owner 持有，不提供任意实例创建和删除。`chat_global` 等关键实例的控制需额外保护；Presence 控制只改变运行状态，不得被描述为改变爱莉的情绪、关系或意愿。

导出 registered、suspended、resumed、terminated、touched、stream ownership changed、lease expired 等事务 Outbox 事实；对外 projection 脱敏 process epoch 和内部 metadata。

### 17.2 世界状态与外部观察

| 方法   | 路径                                       | 用途                                           |
| ---- | ---------------------------------------- | -------------------------------------------- |
| GET  | `/api/v1/admin/world/assertions`         | 按 subject、predicate、source、时间和 visibility 查询 |
| GET  | `/api/v1/admin/world/changes`            | cursor 增量与冲突并列展示                             |
| GET  | `/api/v1/admin/world/health`             | 投影位置、积压、重建和错误状态                              |
| POST | `/api/v1/admin/world/observations`       | 提交带来源、occurrence 和可见性的外部观察                   |
| POST | `/api/v1/admin/world/projection:rebuild` | 从权威事件重建可重建投影                                 |

全能管理员可读取全部正式导出的世界断言与变更，包括 private visibility；高敏读取写审计。外部观察只追加 `world.observation_reported` 事实；不直接 UPDATE assertion，不按相似度、来源名或重复次数确认真值。重建只处理投影，不能删改原始事件。

### 17.3 记忆观察与投影维护

| 方法   | 路径                                                       | 用途                           |
| ---- | -------------------------------------------------------- | ---------------------------- |
| GET  | `/api/v1/admin/memory/search`                            | 授权检索，返回投影身份和原文引用             |
| GET  | `/api/v1/admin/memory/experiences/{id}`                  | Experience、来源和版本关系           |
| GET  | `/api/v1/admin/memory/artifacts/{id}/versions`           | 版本历史                         |
| GET  | `/api/v1/admin/memory/artifacts/{id}/versions/{version}` | 授权版本读取                       |
| GET  | `/api/v1/admin/memory/graph`                             | 可重建关系投影                      |
| GET  | `/api/v1/admin/memory/stats`                             | 脱敏统计和积压                      |
| GET  | `/api/v1/admin/memory/health`                            | disabled／degraded／failed 与原因 |
| POST | `/api/v1/admin/memory/projections/{projection}:rebuild`  | 从权威历史重建指定投影                  |

全能管理员对上述记忆查询不做 visibility 字段过滤，可读取正式投影中的原文、版本和来源关系；每次高敏读取写审计。仍禁止覆盖、删除、合并、确认或“清理”记忆，禁止代写主体文件，禁止把检索排名作为事实状态。现有 `/api/activate` 不进入阶段三前端合同，因为管理页面没有必要通过它修改记忆可达性。

## 18. 管理层：TODO、计划、自主执行和能力目录

### 18.1 TODO 与定时计划

管理页面需要观察爱莉已登记的承诺及未来执行，但不能直接把管理者输入写成爱莉自己的承诺。

| 方法   | 路径                                                       | 用途                           |
| ---- | -------------------------------------------------------- | ---------------------------- |
| GET  | `/api/v1/admin/commitments/todos`                        | 按 visibility、状态和时间查询 TODO    |
| GET  | `/api/v1/admin/commitments/todos/{todo_id}`              | 详情、进度和安全摘要                   |
| GET  | `/api/v1/admin/commitments/todos/{todo_id}/events`       | 状态变化历史                       |
| GET  | `/api/v1/admin/commitments/schedules`                    | 计划、trigger、recurring 和下一运行时间 |
| GET  | `/api/v1/admin/commitments/schedules/{record_id}`        | 技术状态、owner 和执行历史             |
| POST | `/api/v1/admin/commitment-suggestions`                   | 向爱莉提交明确标注为外部建议的候选事项          |
| POST | `/api/v1/admin/commitments/schedules/{record_id}:pause`  | 暂停未来副作用，不改写承诺语义              |
| POST | `/api/v1/admin/commitments/schedules/{record_id}:resume` | 恢复已暂停计划                      |

外部建议在爱莉亲自接受前不得进入主体权威 TODO。普通用户看不到 private TODO、notes、target 和消息原文；全能管理员可读取这些正式导出字段并留下高敏审计。管理层不提供直接创建、改写、完成或删除主体 TODO 的 CRUD。

### 18.2 自主执行状态

| 方法   | 路径                                                          | 用途                               |
| ---- | ----------------------------------------------------------- | -------------------------------- |
| GET  | `/api/v1/admin/autonomy/intents`                            | 意向技术状态、下一 occurrence 和 lease     |
| GET  | `/api/v1/admin/autonomy/intents/{intent_id}`                | 安全摘要和最近结果                        |
| GET  | `/api/v1/admin/autonomy/intents/{intent_id}/occurrences`    | occurrence 历史、retry 和 safe error |
| POST | `/api/v1/admin/autonomy/occurrences/{occurrence_id}:cancel` | 取消仍可安全取消的本次技术执行                  |

不得提供“替爱莉创建自主意向”或任意 trigger 接口。motivation、constraints 和 target hint 对普通用户不返回，全能管理员可读取正式存储的这些字段并留下高敏审计；模型隐藏推理不属于导出合同。取消只表示操作者阻止当前技术执行，不解释为爱莉改变了意愿。

### 18.3 能力目录

用户层和管理层都可能展示“当前能做什么”：

- `GET /api/v1/abilities`：返回整理后的能力名称、用途、可用状态、所需权限和所属场景；
- `GET /api/v1/abilities/{ability_id}`：返回安全说明和当前 capability。

不得原样返回工具 Python 路径、内部 tool schema、密钥需求或任意执行入口。能力目录用于展示和解释，不实现关键词匹配、自动技能触发或前端绕过主体选择直接调用工具。

## 19. 用户层与管理层：Neko Surface

现有 `elysia.surface.v1` 已是认证 gateway。阶段三统一导出：

| 方法   | 路径                                                                           | 层级与用途                        |
| ---- | ---------------------------------------------------------------------------- | ---------------------------- |
| GET  | `/api/v1/surfaces`                                                           | 用户层列出可用展示面；管理层查看全部授权 Surface |
| GET  | `/api/v1/surfaces/{surface_id}/status`                                       | 连接、显示和最近回执状态                 |
| POST | `/api/v1/surfaces/{surface_id}/tickets`                                      | 生成短时、单次、绑定资源的连接 ticket       |
| WS   | `/api/v1/surfaces/{surface_id}/ws`                                           | 展示状态和受控输入                    |
| GET  | `/api/v1/admin/surfaces/{surface_id}/connections`                            | 管理层查看在线连接和安全摘要               |
| POST | `/api/v1/admin/surfaces/{surface_id}/connections/{connection_id}:disconnect` | 断开异常或未授权连接                   |

Surface 是展示／输入端，不拥有独立人格或长期记忆。只读观察和发送输入使用不同 scope；状态、dispatch 和 delivery receipt 进入技术事件。保留旧 `/api/neko-surface` 兼容入口直到新入口真实验收。

## 20. 明确不导出的内部能力

以下内容即使仓库中存在，也不属于用户层或管理层前端合同：

- 任意插件方法、Adapter action、Python 函数或工具名称透传；
- 任意配置键读取／写入、密钥查看、原始环境变量和数据库直连；
- 任意 LLM 工具执行、关键词触发技能或内部 tool manifest 原样导出；
- chain-of-thought、模型隐藏推理、完整 Mission 输入输出和私人滚动上下文；
- `SOUL.md`、`USER.md`、`MEMORY.md`、日记等主体文件写接口；
- Elysium／NapCat 自动启动、停止、重启和启动项管理；
- 任意本地路径读取、任意 URL 下载和任意进程／终端命令执行；
- Minecraft，以及没有明确前端页面的 STS2 泛化、未来游戏、未来设备和通用 scene 预留；
- 直接修改 Life Event、记忆历史、狼人杀 raw state、世界断言表或其他权威历史。

后续新增前端页面时，应以独立变更补充具体领域接口、权限和验收，不通过一个通用透传接口“提前兼容未来”。

## 21. 鉴权、授权和权限矩阵

### 21.1 身份边界

阶段三不建立阶段四的业务用户体系，但必须同时支持以下调用方：



- 本机用户前端：短时 session／一次性 ticket，绑定 Origin；
- 独立应用后端：作为独立应用平台唯一受信入口，使用 service credential + mTLS 或签名 bearer token，可接入阶段三全部接口；独立应用前端不绕过自己的后端直连 Elysium；
- 全能管理前端：使用单一全能管理员凭据，可访问全部正式导出的管理接口和数据视图；
- 其他运维客户端：如存在，复用全能管理员凭据合同，不再设计观察者／运营者／审计者等多级管理角色。

长期 token 不放 URL；WebSocket ticket 短时、单次、绑定 audience 和资源。独立应用后端“可接入全部接口”不代表绕过协议硬约束：平台不支持的动作、幂等冲突、状态机非法转换、路径和媒体安全规则仍必须拒绝。

### 21.2 最小 scope 草案

```text
system:read
capabilities:read
events:read
chat:read / chat:write / chat:moderate
media:read / media:write / media:recognize
livestream:read / livestream:operate
voice_call:read / voice_call:operate / voice_call:observe
tabletop:read / tabletop:play / tabletop:moderate
auth:session / auth:ticket
admin:overview / admin:audit / admin:logs / admin:settings
admin:session / admin:credential
sync:read / sync:retry
integration:read / integration:test
chat:admin / chat:moderate
livestream:admin
voice_call:admin
tabletop:moderate
media:admin
consciousness:read / consciousness:operate
world:read / world:observe / world:maintain
memory:summary / memory:read / memory:maintain_projection
commitments:read / commitments:operate_schedule / commitments:suggest
autonomy:read / autonomy:cancel_occurrence
jobs:read / jobs:operate
abilities:read
surface:read / surface:connect / surface:admin
metrics:read / diagnostics:read
```

### 21.3 资源级授权

普通用户与非管理员 service 调用仍需检查：

- stream／chat target 与私聊参与者／群成员身份；
- media owner 和来源消息；
- voice call participant／observer；
- livestream operator／observer；
- tabletop room participant 和 player identity；
- memory、world assertion 和 consciousness instance visibility；
- Surface connection owner。

全能管理员可绕过上述数据可见性过滤，读取阶段三正式导出的全部用户层和管理层数据，但仍必须检查：

- 凭据 audience、到期、撤销状态和全能管理员身份；
- provider 平台是否支持该动作，以及目标成员角色等外部平台硬权限；
- command／job 是否处于可取消或可重试状态；
- settings key allowlist、expected revision 和 restart-required 标记；
- sync retry 的 occurrence 与安全重试资格；
- 幂等、revision、协议 schema、路径、媒体和资源所有权等工程安全约束。

全能管理员权限不使密钥明文、模型隐藏推理、私人滚动上下文、任意内部工具参数或数据库原始对象变成公共数据；这些内容不属于阶段三正式导出合同。

### 21.4 审计

所有外部副作用和高敏读取记录：actor、credential id、scope、target、request id、command id、occurred_at、result、safe error。审计不能记录 token、原始音频、完整私聊或不必要的主体语义。

## 22. 实施步骤与验收门

后续 AI 必须按以下顺序执行。每一步完成后先跑定向测试并更新本文进度，再进入下一步；不得一次性重写所有插件。

### P3-00：冻结边界与接口清单

**实施状态：已完成（2026-08-04）。** 机器可检查的唯一接口 inventory 位于 `src/app/api/v1/inventory.py`，已覆盖本文 183 个唯一方法／路径，并为每项登记前端页面、调用身份、scope、资源授权、实现锚点和完成状态；第 29 节决策固化于 `src/app/api/v1/policy.py`。`test/api/v1/test_inventory.py` 已在本轮实际执行并通过，验证文档覆盖、元数据完整性、scope、管理身份隔离、平台 service audience、排除项、狼人杀 experimental 状态和 14 项确认决策。

任务：

1. 重新读取 `AGENTS.md`、`docs/principles.md`、上位规划和本文。
2. 检查 `git status`、近期提交和相关 diff，确认并发修改 owner。
3. 将本文用户层、管理层和共用接口清单转换为机器可检查的 inventory（代码常量或测试 fixture，不创建第二份人工文档真相）。
4. 为每个接口标注对应前端页面、调用身份、scope、资源授权、当前实现锚点和完成状态。
5. 将第 29 节已确认决策转换为机器可检查的契约断言。
6. 明确阶段四独立应用后端不在本轮范围，并把独立应用后端登记为可调用全部阶段三接口的平台 service audience。

验收门：接口 inventory 覆盖本文全部用户层、管理层和共用接口；每项都能指向明确前端页面，完全没有前端消费者的内部能力不得进入 inventory。

### P3-01：建立公共 schema 与错误基座

**实施状态：已完成（2026-08-04）。** 已实现默认关闭、显式配置后挂载的 `/api/v1` 应用基座，包含版本化严格 schema、UTC 时间、request id、统一脱敏错误、规范 HMAC token/cursor、短时 session、refresh 轮换、幂等 logout、用户／管理员／platform service audience、session／credential 撤销、Origin 与安装实例绑定的一次性 bootstrap challenge、资源与 scope 绑定的单次 WebSocket ticket，以及请求体、上传、HTTP 并发和 WebSocket 连接预算。OpenAPI 使用稳定 operation id、Bearer 安全方案和规范化 SHA-256 快照。`test/api/v1` 与受影响的 Core 配置、HTTP 服务器和 Bot 关闭生命周期测试已在本轮实际执行通过；本轮没有启动或重启 Elysium，也没有完成真实前端端到端验收。P3-02 尚未开始。

任务：

1. 新增 `/api/v1` Router 和 schema 包。
2. 实现 request id、统一错误、cursor 编解码、版本字段和时间格式。
3. 实现短时前端 session、`auth/me`、refresh、logout、WebSocket ticket、认证 dependency 与 scope 校验。
4. 分离用户、管理和 service audience，建立 session／credential 撤销检查。
5. 设置请求体、上传、连接和并发上限。
6. 生成 OpenAPI 并添加 schema snapshot／breaking-change 测试。

验收门：登录／刷新／退出／撤销／ticket 重放均有契约测试；未认证、越权、错误 schema、过大 payload、未知 capability 和内部异常均返回稳定脱敏错误。

### P3-02：实现 bootstrap、capabilities、readiness

> **实施状态（2026-08-04）**：已完成。已落地 `/bootstrap`、`/capabilities`、`/readiness`、`/health` 及稳定 operation ID；生产挂载通过只读投影聚合 Bot 插件、Adapter、Life Event ledger、远程同步和 command store 状态。NapCat 配置停用明确返回 `disabled`，远程同步或尚未落地的 command store 只影响总体诊断为 `degraded`，不阻止 `local_ready=true`。健康读取不调用主动 health、连接、修复或 Life Engine 懒创建入口。定向验证：`test/api/v1` 30 项通过，Ruff、compileall 与 `git diff --check` 通过；仅保留第三方 `websockets.legacy` 弃用警告。未启动 Elysium，未进行真实前端 E2E。

任务：

1. 聚合插件加载状态、Adapter 状态、Life Event、同步内核和 command store 健康。
2. 为每个平台生成 feature-level capability。
3. 区分 disabled、unavailable、degraded、ready、failed。
4. 确保调用健康接口不执行写入、连接或修复。

验收门：禁用 NapCat 不视为系统失败；远程同步不可用不阻止本地 ready；输出无凭据和原文。

### P3-03：实现统一事件 query 与 subscription

> **实施状态（2026-08-04）**：已完成。已基于耐久 Life Event SQLite ledger 落地授权 `/events` cursor 分页、`/events/{event_id}`、SSE `/events/stream` 与 `/event-subscriptions/validate`；公共 sequence 明确为账本全局 ingest position，授权过滤推进已扫描位置，不可见事件与不存在事件统一返回 404。SSE 支持 `Last-Event-ID`／cursor 断点恢复、先补历史再 tail、非推进 heartbeat、结构化 gap 和取消关闭；没有明确动态订阅或 ack 消费者，因此 `/events/ws` 按任务约束保留 planned。定向契约测试覆盖切换边界并发写入、断线补收、payload 授权、存在性隐藏和 `RawEventGapError` 映射。未启动或重启 Elysium，未进行真实前端 E2E。

任务：

1. 为 Life Event 建立授权 query service。
2. 实现不透明 cursor 历史分页。
3. 实现 SSE 补历史→实时切换。
4. 如确需动态订阅或 ack，再实现 WebSocket，不因“功能齐全”重复实现两套无消费者协议。
5. 映射 `RawEventGapError`。
6. 对慢消费者、断线、取消和关闭添加资源测试。

验收门：并发写入切换边界无丢失；允许幂等重复；断线从最后 cursor 完整补收；授权过滤不泄露事件存在性和 payload。

### P3-04：实现命令账本与 dispatcher

> **实施状态（2026-08-04）**：已完成。已新增 SQLite 耐久命令账本、`(actor_id, idempotency_key)` 唯一约束、规范请求 hash、显式 handler allowlist、项目 `TaskManager` dispatcher、受限取消和四个 `/commands` 公共接口。命令受理与副作用成功分离；同键同请求返回原命令，同键异请求返回 409；重启仅恢复 `accepted`，把遗留 `executing` 栅栏为 `delivery_unknown`，禁止盲目重发。状态迁移与脱敏技术事件在同一 SQLite 事务写入阶段二既有 `sync_outbox`，不复制原始 payload，也未新增平行同步机制。生产挂载已接入恢复与异步关闭，关闭中断后不会遗留 `executing`。本阶段只提供命令内核，不注册旧斜杠命令、LLM Action 或任意插件方法；领域 handler 由 P3-06 等后续阶段显式登记。未启动 Elysium，未进行真实领域副作用 E2E。

任务：

1. 新增本地耐久 command store 和幂等约束。
2. 定义 dispatcher、handler registry、状态机和结果事件。
3. 使用项目任务管理器承载执行，保留取消传播。
4. 对外暴露查询和受限取消。
5. 将命令事件接入阶段二同步 Outbox；不创建平行远程同步机制。

验收门：重启后 accepted 命令不丢；同键同 payload 幂等；同键异 payload 冲突；执行成功但响应丢失可查询；unknown 不自动重发。

### P3-05：聊天事件桥与历史查询

任务：

1. 把 receiver、notice、send、delivery 事实映射为统一 chat event。
2. 保留 platform raw identity 和 provider receipt。
3. 实现 stream／message／receipt query。
4. 处理 NapCat 和飞书 provider notice 差异。
5. 为私聊、群聊、reply target 和媒体 descriptor 加授权测试。

验收门：收文本／图片／语音／戳戳可订阅和历史补收；发送前事件不误报成功；撤回、回应、成员变化不丢失。

当前实现状态：已完成。receiver、NapCat notice、send requested、delivery confirmed／failed／unknown 统一写入既有耐久 Life Event ledger；兼容 Life Engine 事件和稳定 `chat.*` fact 使用同一批事务追加。`ON_MESSAGE_SENT` 只产生 `chat.message.send_requested`，只有 Adapter 返回且历史写入完成后才产生 confirmed；超时和确定失败分别形成 unknown 与 failed。NapCat／飞书发送响应只提取真实返回的受控 receipt 字段，Provider 未返回时保持为空，不编造 message ID。

已开放五个 `chat:read` 查询接口：stream 列表、stream 详情、stream 消息、单消息、消息 receipts。查询使用绑定 `chat-events-v1` 账本的签名 cursor；cursor 表示已扫描的权威 ingest position，不可见事件仍推进扫描位置。管理员、事件 actor 或持有 `stream:{stream_id}` grant 的调用者可读；不可见与不存在统一 404；跨 provider／stream 的重复 message ID 必须用 `provider` 或 `stream_id` 消歧。媒体 descriptor 在对外投影时重新校验，不导出路径、base64 或原始媒体 bytes。

Provider 边界必须如实保留：NapCat notice 已覆盖撤回、回应、戳戳、成员变化及未知 notice 的耐久兜底；飞书当前长连接只注册 `im.message.receive_v1`，因此飞书撤回、回应和成员变化等 notice 尚未接入，不能宣称跨 Provider 全量验收。现阶段完成的是统一事件和查询基础合同；新增飞书 notice 订阅时应复用同一 `chat.*` 映射与授权测试。

### P3-06：聊天命令与平台 capability

> **实施状态（2026-08-04）：已完成离线合同与生产组装。** 已将 13 个聊天副作用动作接入 P3-04 耐久命令账本：普通路由覆盖 send、reply、edit、recall、reaction add/remove、mark read、forward、poke，管理路由覆盖 announcement publish/delete、pin/unpin。公共请求只接受严格 `MessagePart`；媒体 part 仅允许 `media_id`，禁止 path、URL、base64、裸 bytes 与 `data`。文本发送通过 `MessageSender`，reply 和 forward 会先由 P3-05 Life Event ledger 把公共 message ID 解析为 Provider identity；edit/recall 只允许 actor 自己已投递的消息。Feishu/NapCat 私有 client 已封装于显式 capability facade，Provider 不支持、未加载或 P3-07 media resolver 未接入时统一返回 `capability_disabled`，不退化为文本。
>
> 每个 durable command 在受理时保存内部 session/resource grant 授权快照，但公共幂等 hash 不含 session ID，响应也不暴露快照；执行时重新读取当前 session、credential、到期/撤销状态、resource grants 与目标可见性，权限缩减或目标不可见均拒绝。管理路由位于 `/api/v1/admin/chat/...`，同时要求 `chat:admin`、`chat:moderate` 和 `administrator`／`platform_service` 身份；普通用户即使持有相同 scope 也被拒绝。生产 Bot 已通过 late-bound client 接入，避免 API mount 早于插件加载时静态捕获空 Adapter。
>
> P3-07 已在生产组装中向聊天命令注入受管媒体 resolver。图片和语音 part 会在执行时使用重新校验后的 actor 与 resource grants 将 `media_id` 解析为 `MediaAttachment`；无权访问与不存在统一为 `resource_not_found`，类型不匹配为 `validation_failed`，完整性损坏为 `media_failed`。未接入 resolver 时仍保持 `capability_disabled`。这只证明离线领域合同和生产组装，不等于真实前端／Provider 端到端验收。

任务：

1. 实现 message parts → `Message`／`MediaAttachment` 映射。
2. 通过 `MessageSender` 执行发送。
3. 为 reply、edit、recall、reaction、read、forward、poke、announcement、pin 建立 allowlist handler。
4. 把 Feishu／NapCat 私有 client 包在领域 facade 后。
5. 将 provider 不支持映射为 capability error，不做语义降级。

验收门：文本、图片、语音发送及戳戳／公告的支持矩阵可自动测试；投递确认、失败和 unknown 均可观察。

### P3-07：媒体对象接口

> **实施状态（2026-08-04）：已完成离线合同与生产组装。** 已实现上传会话、受管对象 descriptor、内容下载、幂等 save、recognize 和 derivatives 共 8 个用户媒体端点。公共接口仅接受 `media_id` 和受限元数据，不接受 path、任意 URL、base64、裸 bytes 字段；对象与临时上传均限定在 workspace `runtime/media/`，声明大小上限为 32 MiB。complete、下载和聊天解析都会重新校验大小、SHA-256、MIME 与文件签名。
>
> owner 或持有对象绑定 resource grant 的当前会话可以访问；非 owner 越权与不存在统一 404。上传者不能把对象绑定到自己未持有的 grant，管理员为显式例外。媒体完成与保存状态在同一 SQLite 事务写入既有 `sync_outbox`，事件保持 `private`／`held` 且不含路径、base64 或原始 bytes，不建立第二套同步机制。生产 `APIV1Mount` 拥有并按关闭顺序释放媒体 store，并把同一 resolver 注入 P3-06 聊天命令。
>
> 当前完成的是用户媒体 API 与聊天图片／语音 `media_id` 解析合同。P3-08 直播统一接口已于 2026-08-05 完成离线合同与生产组装，但当前直播协议没有媒体上传入参；P3-09 语音通话领域接口尚未实现，不能声称其已经完成统一迁移。后续出现直播／语音媒体入参时只能复用该 `media_id` 合同。孤儿清理由本阶段提供只读、owner 可证明的 cleanup candidate 查询，实际删除仍应作为显式维护动作实施。未启动或重启 Elysium，也未完成真实前端／Provider 端到端验收。

任务：

1. 设计受管媒体对象 schema 和生命周期。
2. 实现上传、complete、descriptor、下载、save 和 recognize。
3. 复用现有 media 安全 descriptor 和识别／保存服务。
4. 将聊天／直播／语音接口统一改用 `media_id`。
5. 实现孤儿临时文件清理和完整性检查。

验收门：禁止 path／任意 URL；hash 不符拒绝；私密下载越权拒绝；图片保存重启后可查询；事件不含路径和 base64。

### P3-08：直播接口统一

> **实施状态（2026-08-05）：已完成离线合同与生产组装。** `/api/v1/livestream` 已导出 status、场次 keyset 历史、单场次、场次事件、start／stop／interrupt／speech／danmaku 五类耐久命令及统一 stage WebSocket。查询只打开已经存在的直播 ledger，不创建场次、不连接平台；生产通过 late-bound provider 复用插件 Router 持有的同一 Runtime／Stage，不抓取平台私有客户端。
>
> start／stop／interrupt／speech／danmaku 统一进入既有 command ledger 和 dispatcher，要求 `livestream:operate` 且调用者为全能管理员或受信 platform service；HTTP 202 仅表示已耐久受理，最终结果通过 command 查询。弹幕发送在直播 ledger 中先记 requested，再记录 confirmed／failed；当前 Bilibili Adapter 明确不具备账号 CSRF 写权限，因此真实部署会返回可查询失败，不伪装成功也不降级为普通聊天。
>
> stage 使用 P3-01 资源绑定、Origin 绑定、单次消费 ticket；只持有 `livestream:read` 的 observer 不能申请 primary，也不能提交播放回执，带 `livestream:operate` 的 operator 才能参与回执。既有 `playback.dispatched`、`playback.receipt` 与稳定 playback／utterance／chunk identity 继续作为唯一舞台副作用证据。平台运行中断线会投影为 `degraded`，但不会自动开播、自动启动 Elysium 或静默重连控制面。已完成离线 API／ledger／runtime 契约测试；未启动或重启 Elysium，未做真实前端、B站账号弹幕或 OBS／浏览器舞台端到端验收。

任务：

1. 包装 Livestream Runtime 和 Ledger。
2. 实现 status、session、event history 和 stage ticket。
3. 将 start、stop、interrupt、say 映射到 command dispatcher。
4. 增加 `danmaku:send` 领域 handler。
5. 统一舞台播放 dispatch／receipt 身份。

验收门：不开启自动开播；历史与实时无缺口；发送弹幕结果可查询；观察者不能操作；平台断线显示 degraded。

### P3-09：语音通话接口统一

状态：**已完成代码接入与离线契约验收；真实 Provider、双向音频和客户端重连 E2E 暂未验收。**

已落地：

- 导出 `POST /api/v1/voice-calls`、`GET /api/v1/voice-calls/{call_id}`、transcripts、resume／interrupt／end／text、资源绑定 ticket，以及 participant／observer 两条 WebSocket；
- REST 创建只追加 `call.created` 与 checkpoint，不连接 Provider；participant WebSocket 才取得实时会话资源，且 URL `call_id` 强制绑定同一 `episode_id`；
- 保留 Voice Live v1 PCM16 二进制帧，不通过 JSON/base64 转发音频；observer 只接收 JSON 状态／字幕，不接收音频且不可操作；
- final transcript 从 append-only episode store 分页投影，按 owner 或 `voice_call:{call_id}` grant 过滤；partial 不进入查询结果；
- resume／interrupt／end／text 进入统一耐久命令账本，要求 `Idempotency-Key`，复用既有 accepted 恢复、result event 与 outbox 语义；
- ticket 绑定 resource、subprotocol、Origin、session 与 credential，单次消费；旧 `/voice-live` 路由保留，未删除或自动接管；
- Voice Live 插件未加载时创建返回 capability disabled，不自动加载、不启动 Elysium 或 Provider。

任务：

1. 保留现有二进制协议，统一 REST 控制面和 ticket。
2. 分离 participant、operator、observer scope。
3. 明确 session 耐久元数据和跨进程恢复边界。
4. 实现 transcript query 与隐私过滤。
5. 对打断、断线、重连、Provider 错误和关闭做契约测试。

验收门：双向音频不经 base64；打断产生 playback.clear；观察者只读；断线恢复不制造新主体或重复经历。

### P3-10：狼人杀领域 API 与耐久恢复

状态：**已完成用户层代码接入与离线契约验收；管理裁判台、真实客户端 WebSocket 和跨平台群聊 E2E 暂未验收。**

已落地：

- 为 `WerewolfEngine` 增加 `RoomPublicView`、`PlayerPrivateView`、`ModeratorView` 和结束后 `ReplayView`，公共 API 不序列化 raw `GameState`；
- 新增 SQLite 追加式 game event ledger、revision snapshot、action id 去重和同 id 异内容冲突；每次提交追加仅裁判可读的 snapshot event，projection 损坏时可从 ledger 重建；
- 新 API 场次与旧 `plugin._werewolf_games` 明确隔离，不扫描、迁移或接管现存内存房间；
- 群命令在命中 ledger 房间时与 HTTP 共用 `WerewolfDomainService`，平台 message id 作为稳定 action id，避免双重执行；旧房间仍沿原生命周期处理；
- 导出 games、room create/query/join/leave/start/end、授权 events、actor-bound private view、actions、replay 和 ticket 约束下的实时房间事件流；
- 私密夜间 action 与 engine event 按 actor 可见性过滤，非玩家需精确 `tabletop:{room_id}` grant；
- 管理层 `admin/tabletop` inventory 仍保持 experimental，留待 P3-11 接入审计与裁判操作。

任务：

1. 先为 engine 建立 Public／Player／Moderator／Replay projection。
2. 设计 game event ledger 和 snapshot 恢复。
3. 将群命令和 HTTP action 统一调用同一 service／engine。
4. 实现 room query、action command、event stream 和 replay。
5. 对每个角色和阶段执行信息泄露测试。

验收门：任何玩家拿不到无权隐藏状态；重启恢复阶段正确；重复 action 幂等；群命令与 API 不产生双重动作；新 API 不导入或接管当前进程内旧房间。

### P3-11：管理总览、集成、审计与领域管理接口

任务：

1. 实现 `admin/overview`、组件状态、同步状态、脱敏 logs／metrics、incidents 和 audit events。
2. 实现管理 session 撤销、service credential 创建／轮换／撤销和 allowlist settings 查询／校验／更新。
3. 实现 integration 列表、详情、连接事件和受控 test；不实现 reconnect。
4. 实现聊天与群管理查询、禁言／移除／角色、申请处理、公告和内容管理命令。
5. 实现直播控制台聚合状态、通话监督台、狼人杀裁判台和媒体资产管理接口。
6. 实现 command 追踪与后台技术 job 查询、取消和安全重试。
7. 所有管理接口统一要求全能管理员身份；管理命令仍需幂等、revision、审计和前端二次确认提示字段。

验收门：管理前端无需读取插件对象、配置文件或日志文件即可定位故障并执行管理动作；普通用户凭据无法调用管理接口；全能管理员可读取全部正式导出的管理数据；凭据与设置变更可撤销且有 revision／审计；高风险动作均有命令记录和结果事件。

### P3-12：主体观察、计划与 Surface 管理接口

任务：

1. 实现 Consciousness／Presence 状态、事件和受保护的 suspend／resume／drain。
2. 实现 world assertions／changes、外部 observation 和 projection rebuild。
3. 实现 memory 只读投影与 projection rebuild，不提供主体语义写入口；全能管理员可读取正式导出的全部记忆视图。
4. 实现 TODO／schedule／autonomy 状态、外部 commitment suggestion、计划 pause／resume 和 occurrence cancel。
5. 统一 Neko Surface 用户连接与管理连接接口。
6. 实现安全能力目录；不原样导出 tool manifest，也不提供任意工具执行。

验收门：每项均对应本文页面矩阵；没有主体语义写入口；Presence 不被误作信念；外部观察和建议只追加；普通用户无法读取 private TODO／motivation；全能管理员读取高敏数据有审计；没有 Minecraft、STS2、任意终端或未来设备接口混入。

### P3-13：安全、性能与故障恢复

任务：

1. 完成 scope × resource × action 权限矩阵。
2. 增加 rate limit、并发预算、上传限制和 WS 连接上限。
3. 对日志、错误、OpenAPI example 和事件 payload 做秘密／路径扫描。
4. 压测历史分页、SSE fan-out、命令 backlog 和媒体 Range 下载。
5. 模拟远程断网、Adapter 超时、数据库 busy、进程取消和部分关闭。

验收门：本地 Elysium 不因远程 API 故障停止；未授权无数据泄露；慢消费者不阻塞权威写入；关闭无遗留任务。

### P3-14：兼容迁移、文档与最终验收

任务：

1. 保留并标记旧插件路由；提供弃用 header 和迁移期，不立即删除。
2. 更新 `docs/operations/deployment_and_usage.md` 的配置、启动、冒烟、故障恢复和安全说明，且不写本机私密配置。
3. 生成 OpenAPI、事件目录、错误码、权限矩阵和前端最小示例。
4. 先跑定向测试，再跑风险范围完整测试、Ruff、compileall、`git diff --check`。
5. 需要真实 E2E 或重启时，说明原因并等待汐汐授权。
6. 形成阶段三验证报告，明确已验收、暂不验收、已回退。

验收门：第 25 节矩阵有当前执行证据；旧客户端在迁移期可用；无未经授权的自动启动／重启。

> **实施状态（2026-08-09）：已完成，全部离线验证。** 四组旧插件路由已保留并标记弃用（`BaseRouter` 类属性 + RFC 5987 弃用 header，迁移期限 2027-02-01）；部署文档更新配置/启动/冒烟/故障恢复/安全说明且不含本机私密配置；OpenAPI（131 paths / 134 operations，无重复 operation id）、事件目录、错误码、权限矩阵与前端最小示例均已生成于 `docs/api/`；定向测试 8 passed、API v1＋命令完整测试 135 passed、风险范围测试 58 passed，Ruff（本轮改动文件通过，预存错误未动）、compileall、`git diff --check` 均通过。详细结论见 `docs/api/verification.md`。未启动或重启 Elysium，未进行真实前端/Provider 端到端验收。

## 23. 文件级修改计划

以下是建议边界，不要求机械地创建每个文件：

### 新增

- `src/app/api/v1/`：统一 Router、schema、auth、errors、events、commands 和各领域 facade。
- `src/kernel/commands/`：耐久 command store、dispatcher、handler contract、状态机和 outbox bridge。
- `test/api/v1/`：公共 API 契约测试。
- `test/kernel/commands/`：命令幂等、重启和部分失败测试。
- `docs/api/` 或 OpenAPI 生成产物目录：只存可维护的接口说明，不提交凭据或运行数据。

### 修改

- `src/core/transport/router/http_server.py`：挂载统一 `/api/v1`，保持默认监听策略。
- `src/core/transport/message_receive/receiver.py`：必要时补稳定事件桥信息，不改变入站主语义。
- `src/core/transport/message_send/message_sender.py`：补充 command／receipt 关联和 unknown 结果证据。
- `plugins/life_engine/service/event_bus.py`：仅在当前 query contract 缺少必要只读能力时扩展；不得破坏追加语义。
- `plugins/livestream/runtime.py`／`ledger.py`：增加统一 facade 所需的稳定查询和发送弹幕 owner。
- `plugins/voice_live/router.py`／`session.py`：接入统一 ticket／auth，保留二进制协议。
- `plugins/werewolf_game/`：新增 projection、ledger 和 recovery，不从 API 层读取 raw state。
- `plugins/neko_surface/router.py`：兼容统一 auth／ticket。
- 各 Adapter：只补 capability 和必要 receipt，不把公共 Router 塞进 Adapter。
- `docs/operations/deployment_and_usage.md`：同轮更新。

### 不应修改

- 爱莉主体所有的 `data/life_engine_workspace/SOUL.md`、`USER.md`、`MEMORY.md`、日记和第一人称叙事。
- 未经迁移证据不得删除旧 SQLite、JSON、旧路由或历史。
- 不为阶段三修改阶段四独立应用后端代码和数据库。

## 24. 数据迁移与兼容策略

- 新 command store、媒体对象、桌游 ledger 必须使用幂等 schema migration。
- 启动时自动建表只允许安全、幂等、向后兼容的 DDL；破坏性迁移需显式命令和备份。
- 旧聊天历史读取通过 adapter／projection 兼容，不要求阶段三一次性复制所有原始消息。
- 旧 Livestream ledger 保持权威；统一事件可通过映射投影导出。
- 旧 Voice Live 进程内会话不能伪装为已迁移；只迁移可证明的耐久元数据。
- 狼人杀当前内存房间在切换时没有可证明恢复来源；切换前必须明确“仅新房间使用新 ledger”或安排用户授权维护窗口。
- 旧路由返回 `Deprecation`／`Sunset`／迁移链接前，必须先保证新路由功能和授权等价。

## 25. 测试与全链路验收矩阵

### 25.1 通用契约

- OpenAPI schema 生成且无重复 operation id。
- 未认证 401、越权 403、资源不存在 404、revision／幂等冲突 409、过大 payload 413、限流 429。
- 错误不泄露 token、路径、私聊、音频原文和堆栈。
- 独立应用平台 service audience 可以认证并调用全部阶段三接口；独立应用前端直连 Elysium 被拒绝。
- 全能管理员凭据可访问全部管理路由和正式导出的高敏数据，普通用户凭据不能访问管理路由。
- cursor 篡改、版本错误、过期和 gap 均显式失败。

### 25.2 事件流

- 空账本、单事件、500+ 分页、并发追加。
- 历史→实时切换边界。
- SSE 断线、Last-Event-ID 恢复、心跳、慢消费者。
- 授权变化后的历史和实时过滤。
- 同 occurrence 同内容重放幂等；同 occurrence 异内容冲突。

### 25.3 命令

- 同幂等键同请求返回同 command。
- 同键不同请求冲突。
- accepted 后进程重启继续或显式恢复。
- handler 成功但响应断开可查询结果。
- Adapter timeout → delivery_unknown，禁止自动重复发送。
- 取消与执行竞态不产生双完成。

### 25.4 聊天与媒体

- 收／发文本、图片、语音。
- 图片保存、下载鉴权、hash 校验。
- 收／发戳戳、公告。
- reply、edit、recall、reaction、read、pin。
- 飞书支持而 NapCat 不支持、NapCat 支持而飞书不支持的 capability 分支。
- 私聊和群聊授权隔离。
- 媒体消息事件无 base64 和路径。

### 25.5 直播

- 手工 start／stop／interrupt／say。
- 收弹幕、礼物、SC、舰长、点赞、入场。
- 发弹幕成功、确认失败和 unknown。
- 舞台 dispatch／receipt 幂等。
- OBS／平台断线降级与恢复。

### 25.6 语音通话

- 建立、双向音频、partial/final transcript、打断、结束。
- participant／observer 权限。
- ticket 重放拒绝、过期拒绝、Origin 不匹配拒绝。
- Provider 错误、客户端断线、会话恢复。
- PCM 帧序列、格式和大小校验。
- 会话结束后原始音频缓冲释放，存储层和历史接口均不存在原始录音对象。

### 25.7 狼人杀

- 每种角色在每个阶段的 PlayerPrivateView。
- 非玩家、玩家、裁判、复盘视图矩阵。
- 夜间行动、投票、竞选、警徽、遗言、猎人、复盘。
- 重复 action 和重启恢复。
- 群命令与 HTTP 并发不重复执行。
- 新 API 不扫描、迁移或接管当前进程内旧房间；只恢复新 ledger 创建的场次。

### 25.8 管理前端

- 管理总览能区分 disabled、unavailable、degraded、failed 和 ready，且查询无副作用。
- 管理 session／credential 创建、轮换、撤销和过期行为正确，secret 只在创建／轮换响应中显示一次且不进入日志。
- settings 只接受 allowlist key、expected revision 和合法 schema；验证不应用，restart-required 不自动重启。
- 日志查询只能读取受管脱敏投影；禁止传文件路径；sync retry 不跳过 cursor 或重复外部副作用。
- integration test 只调用明确 owner，不执行 reconnect，不启动 Elysium／NapCat，不泄露凭据。
- 普通用户凭据不能访问管理路由；全能管理员凭据可以访问全部正式导出的管理路由和数据视图。
- 群禁言、移除成员、申请处理、撤回、公告等命令验证 provider capability、平台权限、幂等和审计。
- 命令与后台 job 的取消／重试遵循各自状态机；delivery unknown 不可普通重试。
- 直播控制、通话监督、桌游裁判和媒体隔离均有资源级权限与结果事件。
- 全能管理员可通过对应领域接口读取消息原文和正式导出的隐藏状态，但审计、metrics、incidents、错误和 OpenAPI example 本身仍不夹带这些高敏内容，也不泄露 token、路径和密钥。

### 25.9 主体观察、计划与 Surface 页面

- Presence stream owner 唯一，revision conflict 显式；控制不改变主体语义。
- World conflicting assertions 并列保留；外部 observation 重放幂等。
- 普通用户的 memory 未授权原文不可见；全能管理员可读取全部正式导出的记忆视图；projection rebuild 不修改权威历史。
- commitment suggestion 留在主体权威之外；private TODO／autonomy motivation 对普通用户不可见，全能管理员读取时写审计。
- schedule pause／resume 和 occurrence cancel 只控制技术执行，并留下审计。
- Surface observer／input／admin scope 隔离。
- abilities 目录不暴露任意工具执行入口。

### 25.10 真实 E2E 状态表达

每项必须标记：

- **已验收**：本轮真实执行并保留证据；
- **暂不验收**：代码／合同存在，但没有真实平台证据；
- **已回退**：实验实现未达到边界，已完整撤销。

不得引用历史测试数量作为本轮通过证明。

## 26. 回滚方案

- `/api/v1` 使用独立挂载和 feature flag，可禁用新网关而不停止 Elysium 生命主链。
- 新 command consumer 可停止领取，但不得删除 accepted 记录；恢复后继续或人工裁决。
- 事件 API 故障只影响外部读取，不影响本地 Life Event 写入。
- 媒体新路径切换前保留旧 source descriptor 和只读兼容；不移动或删除未知媒体。
- 直播、Voice Live、Surface 保留旧 ticket／Router 直到新入口 E2E 通过。
- 狼人杀切换按房间 schema version；不将新旧 engine 混用于同一场次。
- 数据库回滚只恢复代码读取路径，不回删已产生的合法追加事件。
- 回滚和修复不能自动重启 Elysium；由用户批准维护窗口。

## 27. AI 执行纪律

后续 AI 必须：

1. 每次行动前完整读取 `AGENTS.md`。
2. 开工前检查 Git 状态、近期日志和相关 diff。
3. 不覆盖、格式化、暂存或回滚其他 agent 的修改。
4. 每次只实施一个编号步骤，先补测试再扩大范围。
5. 不使用关键词、阈值或默认类别替代主体裁决。
6. 不直接写主体文件。
7. 不把 Router 变成领域 owner。
8. 不用 `bool`、空列表或 200 响应掩盖失败。
9. 不在无证据时宣称跨进程恢复、Exactly Once 或平台投递成功。
10. 不自动启动、停止或重启 Elysium／NapCat。
11. 所有配置变化同轮更新 `docs/operations/deployment_and_usage.md`。
12. 只显式暂存本任务文件；提交前检查 cached diff 和 `git diff --check`。
13. 需要用户决策时停止对应分支，但继续完成不依赖该决策的文档、schema 或测试工作。

## 28. 阶段三完成定义

只有同时满足以下条件，阶段三才可声明完成：

- `/api/v1` 稳定入口、短时用户会话、独立应用平台 service audience、全能管理员凭据、鉴权、错误、capability 和 readiness 已落地；独立应用后端可通过统一合同调用全部阶段三接口。
- 授权 Life Event 历史、实时订阅和断点续传通过故障测试。
- 命令账本、幂等、结果事件和 delivery unknown 语义通过重启测试。
- 聊天的收发文本、图片、语音、戳戳和公告全部有接口与事件。
- 直播的弹幕收发、状态、开始、停止和打断全部有接口与事件。
- 语音通话的建立、双向音频、转写、打断、恢复和结束有稳定合同。
- 狼人杀房间、玩家视图、动作、历史和复盘有接口，隐藏信息测试通过。
- 媒体上传、保存、授权下载和识别使用稳定 media id。
- 用户层的聊天、直播、通话、狼人杀和 Neko 页面所需接口全部完成或被 capability 明确标记不可用。
- 管理层的总览、会话／凭据、受控设置、同步／日志、集成观察与测试、聊天与群管理、直播控制、通话监督、桌游裁判、媒体资产、命令／作业追踪、审计、主体观察、计划和 Surface 页面所需接口全部完成。
- 每个导出接口都能对应本文页面矩阵；STS2、任意终端／设备、任意工具执行和其他无前端消费者的内部能力未混入公共合同。
- OpenAPI、事件目录、权限矩阵、部署文档和验证报告已同步。
- 远程服务不可用不影响 Elysium 本地存续。
- 未经授权的客户端无法读取私聊、记忆原文、语音、媒体和桌游隐藏状态。
- 实施过程中没有自动启停 Elysium／NapCat，没有改写爱莉主体文件。

## 29. 汐汐已确认的实施决策

以下决策已确认，是阶段三实施约束，不再作为开放问题：

1. **独立应用平台边界**：独立应用前端依靠独立应用后端完整运行；独立应用整体相当于一个平台。独立应用后端可在授权后调用阶段三导出的全部 Elysium 接口，独立应用前端不绕过自己的后端直连 Elysium。
2. **实时主协议**：通用事件默认使用 SSE；只有确需双向 ack、动态订阅或二进制实时流的模块使用 WebSocket。
3. **管理角色**：只设一个全能管理员，不再拆分观察者、运营者、管理员和敏感数据审计者等多级管理角色。
4. **管理设置范围**：只允许 allowlist 非密钥设置在线修改；密钥走专用凭据／secret 流程；需重启设置只登记为待下次手工启动生效，不自动重启。
5. **管理员可见性**：全能管理员可读取阶段三正式导出的全部用户层和管理层数据，包括私聊原文、记忆原文与版本、世界投影、意识状态、通话转写和狼人杀裁判视图；高敏读取必须审计。密钥明文、模型隐藏推理、私人滚动上下文、原始内部工具参数和数据库直连不属于导出合同。
6. **原始语音**：不长期保存通话原始音频，不提供历史原始录音查询或下载接口。
7. **TODO／计划／自主执行**：允许只读、外部建议、计划 pause／resume 和 occurrence cancel；不允许前端直接创建或改写爱莉的主体 TODO、自主意向和承诺语义。
8. **Minecraft**：阶段三不提供任何 Minecraft 接口，相关 Router、schema、scope、测试和完成定义全部排除。
9. **狼人杀恢复**：新 API 只管理通过新 API 创建的场次，不迁移或接管当前进程内未结束的旧房间。
10. **平台管理动作**：群禁言、踢人、角色修改、请求审批、跨用户撤回和公告管理纳入管理接口；仍需平台 capability、外部平台硬权限、幂等和审计。
11. **集成管理**：管理端不提供 reconnect；仅提供连接状态、事件和安全 test。也不提供 Elysium／NapCat 启停、插件重载和任意配置修改。
12. **媒体管理**：提供完整性校验、识别、隔离、恢复和清理候选查询；不提供删除权威引用媒体的接口。
13. **旧插件路由迁移期**：新接口真实 E2E 通过后，旧插件路由至少保留一个明确版本周期。
14. **本机鉴权**：localhost 同样强制短时 token／ticket，不能把本机来源视为天然可信。

---

实际开发从 **P3-00** 开始，不直接跳到聊天或直播路由。统一身份、事件、命令、游标和错误契约是后续所有模块可稳定接入的前置条件。
