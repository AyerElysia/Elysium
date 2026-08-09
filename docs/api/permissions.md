# API v1 权限矩阵

> 本文档汇总 `/api/v1` 公共接口的授权边界：调用身份、所需 scope、资源级授权
> 与当前实现状态。身份与 scope 的权威来源是 `src/app/api/v1/policy.py`、
> `inventory.py` 与各 Router；本矩阵是与当前实现核对后的快照。
>
> 身份（audience）：
> - **user_frontend**（`elysium-user-frontend`）— 普通前端会话
> - **administrator**（`elysium-admin-frontend`）— 全能管理员前端会话
> - **platform_service**（`elysium-platform-service`）— 独立应用后端，可申请全部 scope
>
> 状态标注：✔ validated（已实现并通过契约测试）；◐ experimental（已注册但
> 未完全验收）；○ planned（目录中规划、尚未实现，OpenAPI 不包含）。

## 1. 认证与基础

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| POST | `/auth/sessions` | 全部 | `auth:session` | ✔ |
| GET | `/auth/me` | 全部 | 任意有效会话 | ✔ |
| POST | `/auth/sessions/current:refresh` | 全部 | refresh grant | ✔ |
| DELETE | `/auth/sessions/current` | 全部 | 当前会话 | ✔ |
| POST | `/auth/ws-tickets` | 全部 | `auth:ticket` + 目标资源 scope | ✔ |
| GET | `/bootstrap` | 全部 | `system:read` | ✔ |
| GET | `/capabilities` | 全部 | `capabilities:read` | ✔ |
| GET | `/readiness` | 全部 | `system:read` | ✔ |
| GET | `/health` | 全部 | 最小公开 | ✔ |
| GET | `/openapi.json` | 全部 | 按部署策略 | ✔ |

## 2. 事件

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| GET | `/events` | 全部 | `events:read` | ✔ |
| GET | `/events/stream` (SSE) | 全部 | `events:read` | ✔ |
| GET | `/events/{event_id}` | 全部 | `events:read` | ✔ |
| POST | `/event-subscriptions/validate` | 全部 | `events:read` | ✔ |
| WS | `/events/ws` | 全部 | `events:read` | ○（无双向消费者，未实施） |

> 事件按 `visibility.scope`、调用者资源授权与已扫描账本位置过滤；不可见与
> 不存在统一处理；`include_payload` 需要额外授权。

## 3. 聊天

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| GET | `/chat/streams` | 全部 | `chat:read` | ✔ |
| GET | `/chat/streams/{stream_id}` | 全部 | `chat:read` | ✔ |
| GET | `/chat/streams/{stream_id}/messages` | 全部 | `chat:read` | ✔ |
| GET | `/chat/messages/{message_id}` | 全部 | `chat:read` | ✔ |
| GET | `/chat/messages/{message_id}/receipts` | 全部 | `chat:read` | ✔ |
| POST | `/chat/messages:send` | 全部 | `chat:write` | ✔ |
| POST | `/chat/messages/{id}:reply` | 全部 | `chat:write` | ✔ |
| POST | `/chat/messages/{id}:edit` | 全部 | `chat:write` | ✔ |
| POST | `/chat/messages/{id}:recall` | 全部 | `chat:write`（owner）| ✔ |
| POST | `/chat/messages/{id}/reactions` | 全部 | `chat:write` | ✔ |
| DELETE | `/chat/messages/{id}/reactions/{reaction}` | 全部 | `chat:write` | ✔ |
| POST | `/chat/messages/{id}:mark-read` | 全部 | `chat:write` | ✔ |
| POST | `/chat/messages:forward` | 全部 | `chat:write` | ✔ |
| POST | `/chat/streams/{stream_id}/poke` | 全部 | `chat:write` | ✔ |
| GET | `/chat/streams/{stream_id}/members` | 全部 | `chat:read` | ○ |
| GET | `/chat/streams/{stream_id}/announcements` | 全部 | `chat:read` | ○ |
| GET | `/chat/streams/{stream_id}/files` | 全部 | `chat:read` | ○ |

## 4. 命令

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| POST | `/commands` | 全部 | `jobs:operate` + Idempotency-Key | ✔ |
| GET | `/commands` | 全部 | `jobs:read`（普通仅自己） | ✔ |
| GET | `/commands/{command_id}` | 全部 | `jobs:read` | ✔ |
| POST | `/commands/{command_id}:cancel` | 全部 | `jobs:operate` | ✔ |

## 5. 媒体

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| POST | `/media/uploads` | 全部 | `media:write` | ✔ |
| PUT | `/media/uploads/{upload_id}` | 全部 | `media:write` | ✔ |
| POST | `/media/uploads/{upload_id}:complete` | 全部 | `media:write` | ✔ |
| GET | `/media/{media_id}` | 全部 | `media:read` + resource grant | ✔ |
| GET | `/media/{media_id}/content` | 全部 | `media:read` + resource grant | ✔ |
| POST | `/media/{media_id}:save` | 全部 | `media:write` | ✔ |
| POST | `/media/{media_id}:recognize` | 全部 | `media:recognize` | ✔ |
| GET | `/media/{media_id}/derivatives` | 全部 | `media:read` | ✔ |

> 校验 media owner、显式 resource grant、状态、hash、MIME 与受管对象身份；
> 不可见与不存在统一处理。

## 6. 直播

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| GET | `/livestream/status` | 全部 | `livestream:read` | ✔ |
| GET | `/livestream/sessions` | 全部 | `livestream:read` | ✔ |
| GET | `/livestream/sessions/{session_id}` | 全部 | `livestream:read` | ✔ |
| GET | `/livestream/sessions/{session_id}/events` | 全部 | `livestream:read` | ✔ |
| POST | `/livestream/session:start` | 全部 | `livestream:operate` | ✔ |
| POST | `/livestream/session:stop` | 全部 | `livestream:operate` | ✔ |
| POST | `/livestream/session:interrupt` | 全部 | `livestream:operate` | ✔ |
| POST | `/livestream/speech:request` | 全部 | `livestream:operate` | ✔ |
| POST | `/livestream/danmaku:send` | 全部 | `livestream:operate` | ✔ |
| WS | `/livestream/stage/ws` | 全部 | `livestream:read` + stage ticket | ✔ |

## 7. 语音通话

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| POST | `/voice-calls` | 全部 | `voice_call:operate` | ✔ |
| GET | `/voice-calls/{call_id}` | 全部 | `voice_call:read` | ✔ |
| GET | `/voice-calls/{call_id}/transcripts` | 全部 | `voice_call:read`（participant/observer） | ✔ |
| POST | `/voice-calls/{call_id}:resume` | 全部 | `voice_call:operate` | ✔ |
| POST | `/voice-calls/{call_id}:interrupt` | 全部 | `voice_call:operate` | ✔ |
| POST | `/voice-calls/{call_id}:end` | 全部 | `voice_call:operate` | ✔ |
| POST | `/voice-calls/{call_id}/text` | 全部 | `voice_call:operate` | ✔ |
| POST | `/voice-calls/{call_id}/tickets` | 全部 | `voice_call:operate` + resource | ✔ |
| WS | `/voice-calls/{call_id}/ws` | 全部 | `voice_call:operate` + 单次 ticket | ✔ |
| WS | `/voice-calls/{call_id}/observe` | 全部 | `voice_call:observe` + ticket | ✔ |

## 8. 桌游（狼人杀）

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| GET | `/tabletop/games` | 全部 | `tabletop:read` | ✔ |
| POST | `/tabletop/rooms` | 全部 | `tabletop:play` | ✔ |
| GET | `/tabletop/rooms/{room_id}` | 全部 | `tabletop:read` | ✔ |
| POST | `/tabletop/rooms/{room_id}:join` | 全部 | `tabletop:play` | ✔ |
| POST | `/tabletop/rooms/{room_id}:leave` | 全部 | `tabletop:play` | ✔ |
| POST | `/tabletop/rooms/{room_id}:start` | 全部 | `tabletop:play` | ✔ |
| POST | `/tabletop/rooms/{room_id}:end` | 全部 | `tabletop:play` | ✔ |
| POST | `/tabletop/rooms/{room_id}/actions` | 全部 | `tabletop:play` | ✔ |
| GET | `/tabletop/rooms/{room_id}/view` | 全部 | `tabletop:read` | ✔ |
| GET | `/tabletop/rooms/{room_id}/events` | 全部 | `tabletop:read` | ✔ |
| GET | `/tabletop/rooms/{room_id}/replay` | 全部 | `tabletop:read` | ✔ |
| WS | `/tabletop/rooms/{room_id}/ws` | 全部 | `tabletop:play` + ticket | ✔ |

## 9. 能力与 Surface

| 方法 | 路径 | 身份 | Scope | 状态 |
| --- | --- | --- | --- | --- |
| GET | `/abilities` | 全部 | `abilities:read` | ✔ |
| GET | `/abilities/{ability_id}` | 全部 | `abilities:read` | ✔ |
| GET | `/surfaces` | 全部 | `surface:read` | ✔ |
| GET | `/surfaces/{surface_id}/status` | 全部 | `surface:read` | ✔ |
| POST | `/surfaces/{surface_id}/tickets` | 全部 | `surface:connect` + resource | ✔ |
| WS | `/surfaces/{surface_id}/ws` | 全部 | `surface:connect` + 单次 ticket | ○（无双向消费者） |

## 10. 管理（administrator / platform_service）

| 方法 | 路径 | Scope | 状态 |
| --- | --- | --- | --- |
| GET | `/admin/overview` | `admin:overview` | ✔ |
| GET | `/admin/components`、`/components/{id}` | `admin:overview` | ✔ |
| GET | `/admin/metrics` | `metrics:read` | ✔ |
| GET | `/admin/incidents` | `admin:overview` | ✔ |
| GET | `/admin/audit-events`、`/{audit_id}` | `admin:audit` | ✔ |
| GET | `/admin/logs` | `admin:logs` | ✔ |
| GET | `/admin/sync` | `sync:read` | ✔ |
| POST | `/admin/sync:retry` | `sync:retry` | ◐（owner 未接入） |
| GET | `/admin/auth/sessions` | `admin:session` | ✔ |
| DELETE | `/admin/auth/sessions/{session_id}` | `admin:session` | ✔ |
| GET/POST | `/admin/credentials` | `admin:credential` | ✔ |
| POST | `/admin/credentials/{id}:rotate` | `admin:credential` | ✔ |
| DELETE | `/admin/credentials/{id}` | `admin:credential` | ✔ |
| GET/PATCH | `/admin/settings` | `admin:settings` | ✔ |
| POST | `/admin/settings:validate` | `admin:settings` | ✔ |
| GET | `/admin/integrations`、`/{id}`、`/{id}/events` | `integration:read` | ✔ |
| POST | `/admin/integrations/{id}:test` | `integration:test` | ✔ |
| GET | `/admin/jobs`、`/{job_id}` | `jobs:read` | ✔ |
| POST | `/admin/jobs/{job_id}:cancel` / `:retry` | `jobs:operate` | ◐ |
| GET | `/admin/consciousness/instances` 等 | `consciousness:read` | ✔ |
| POST | `/admin/consciousness/instances/{id}:suspend/resume/drain` | `consciousness:operate` | ✔ |
| GET | `/admin/world/assertions`、`/changes`、`/health` | `world:read` | ✔ |
| POST | `/admin/world/observations` | `world:observe` | ✔ |
| POST | `/admin/world/projection:rebuild` | `world:maintain` | ✔ |
| GET | `/admin/memory/search` 等只读 | `memory:summary`/`memory:read` | ✔ |
| POST | `/admin/memory/projections/{p}:rebuild` | `memory:maintain_projection` | ✔ |
| GET | `/admin/commitments/...` | `commitments:read` | ✔ |
| POST | `/admin/commitment-suggestions` | `commitments:suggest` | ✔ |
| POST | `/admin/commitments/schedules/{id}:pause/resume` | `commitments:operate_schedule` | ✔ |
| GET | `/admin/autonomy/intents` 等 | `autonomy:read` | ✔ |
| POST | `/admin/autonomy/occurrences/{id}:cancel` | `autonomy:cancel_occurrence` | ✔ |
| GET | `/admin/surfaces/{id}/connections` | `surface:admin` | ✔ |
| POST | `/admin/surfaces/{id}/connections/{cid}:disconnect` | `surface:admin` | ✔ |
| GET | `/admin/chat/streams/{sid}/announcements` | `chat:admin` | ✔ |
| POST | `/admin/chat/streams/{sid}/announcements` | `chat:admin` | ✔ |
| DELETE | `/admin/chat/streams/{sid}/announcements/{id}` | `chat:admin` | ✔ |
| POST | `/admin/chat/messages/{mid}:pin` / `:unpin` | `chat:admin` | ✔ |

### 10.1 管理中尚未实现（○ planned）

- `/admin/chat/*` 查询与管理操作（members、requests、moderation、recall、mute 等）— 需封装 allowlist chat admin facade；
- `/admin/voice-calls/*` 监督接口 — 需新增监督 facade；
- `/admin/media/*` 管理接口 — 需新增引用、隔离与完整性 facade；
- `/admin/memory/experiences/{id}`、`/artifacts/{id}/versions*` 详情 — 需安全 facade；
- `/admin/tabletop/*` 裁判台 — 需 moderator projection 与 recovery；
- `/admin/commitments` 详情、`/admin/autonomy` 详情 — 需只读 facade。

## 11. 总则

- **普通用户凭据不能访问任何 `/admin/*` 路由**（`role_required`）；
- **platform_service 可申请全部导出 scope**，每个请求仍需通过认证、capability 与
  资源授权校验；
- **不可见与不存在统一处理**，不通过 404/403 差异泄露资源存在性；
- 管理高敏读取（message 原文、隐藏状态、transcript 等）写入审计；
- 所有 `/api/v1` 路由对旧插件 `/livestream`、`/voice-live`、`/api/neko-surface`、
  `/memory_vis` 无继承关系，不共享弃用头。
