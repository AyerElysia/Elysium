# API v1 错误码目录

> 所有 `/api/v1` 错误统一返回 `ErrorResponse` envelope
> （`src/app/api/v1/schemas/error.py`），HTTP 状态码与 `error.code` 共同确定语义。
> 错误不泄露 token、路径、私聊原文、音频原文和堆栈；`request_id` 用于关联审计日志。

```json
{
  "error": {
    "code": "history_gap",
    "message": "请求的事件历史已不连续。",
    "request_id": "req_...",
    "retryable": false,
    "details": {},
    "recovery": {"action": "restart_from_cursor", "cursor": "..."}
  }
}
```

## 1. 认证与授权（401 / 403）

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `unauthenticated` | 401 | 无有效 Bearer 会话、已撤销或已过期 | 否 |
| `forbidden` | 403 | 身份有效但被拒绝 | 否 |
| `scope_required` | 403 | 会话缺少所需 scope | 否 |
| `role_required` | 403 | 需要全能管理员/platform service 身份 | 否 |
| `resource_forbidden` | 403 | 资源级授权不足 | 否 |
| `resource_grant_forbidden` | 403 | 缺少显式资源 grant | 否 |
| `protected_instance` | 403 | 关键实例受额外保护 | 否 |
| `ticket_rejected` | 403 | WebSocket ticket 无效/过期/重放 | 否 |

## 2. 资源与能力（404 / 405 / 503）

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `resource_not_found` | 404 | 资源不存在或不可见（统一处理） | 否 |
| `capability_disabled` | 404 | 目标实时资源未开放 | 否 |
| `method_not_allowed` | 405 | 方法不受支持 | 否 |
| `component_unavailable` | 503 | 领域能力未接入或 ledger 不可用 | 是 |
| `component_degraded` | 503 | 组件降级 | 是 |

## 3. 校验与协议（422 / 415 / 413）

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `validation_failed` | 422 | 请求参数不符合接口协议 | 否 |
| `unsupported_media_type` | 415 | 媒体类型不受支持 | 否 |
| `payload_too_large` | 413 | 请求体超过上限（1 MiB） | 否 |
| `idempotency_key_required` | 422 | 命令需要有效的 `Idempotency-Key` | 否 |
| `cursor_invalid` | 422 | 事件 cursor 无效/被篡改 | 否 |
| `projection_invalid` | 422 | 投影参数不合法 | 否 |

## 4. 状态与并发冲突（409 / 429）

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `resource_state_conflict` | 409 | 资源状态冲突（含 revision 冲突） | 否 |
| `idempotency_conflict` | 409 | 同键不同请求（不同 hash）冲突 | 否 |
| `cursor_conflict` | 409 | `cursor` 与 `Last-Event-ID` 不一致 | 否 |
| `history_gap` | 409 | 请求的事件历史不连续 | 否 |
| `cursor_expired` | 409 | cursor 已过期 | 否 |
| `state_conflict` / `revision_conflict` | 409 | 通用状态/revision 冲突别名 | 否 |
| `command_not_cancellable` | 409 | 命令状态机不允许取消 | 否 |
| `rate_limited` | 429 | 资源预算耗尽 | 是 |
| `command_backlog_full` | 429 | 命令积压达到技术上限 | 是 |

## 5. 媒体领域（来自 media_objects）

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `media_not_found` | 404 | 媒体对象不存在或不可见 | 否 |
| `upload_not_found` | 404 | 上传会话不存在 | 否 |
| `media_type_mismatch` | 422 | 媒体类型与期望不符 | 否 |
| `media_size_mismatch` | 422 | 分片大小不符 | 否 |
| `media_hash_mismatch` | 422 | 分片 hash 校验失败 | 否 |
| `media_validation_failed` | 422 | 媒体校验失败 | 否 |
| `upload_state_conflict` | 409 | 上传状态冲突 | 否 |
| `media_identity_conflict` | 409 | 媒体身份冲突 | 否 |
| `media_integrity_failed` | 500 | 完整性投影校验失败 | 否 |
| `media_recognition_unavailable` | 503 | 识别服务不可用 | 是 |
| `range_not_satisfiable` | 416 | 请求范围不满足 | 否 |
| `media_not_ready` | 409 | 媒体尚未就绪 | 否 |
| `media_access_denied` | 403 | 媒体访问被拒 | 否 |

## 6. 命令领域（来自 src/kernel/commands 与命令路由）

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `idempotency_conflict` | 409 | 同键不同请求冲突 | 否 |
| `command_backlog_full` | 429 | 积压上限 | 是 |
| `command_not_cancellable` | 409 | 当前状态不可取消 | 否 |
| `delivery_unknown` | 202+查询 | 投递结果不可判，禁止普通重试 | 否 |

## 7. 内部与未知

| code | HTTP | 含义 | retryable |
| --- | --- | --- | --- |
| `internal_error` | 500 | 服务内部错误 | 是 |
| `request_rejected` | 500/其他 | 请求无法处理 | 否 |

## 8. 通用约定

- **不可见与不存在统一处理**：`resource_not_found` 不区分"不存在"与"无权查看"；
- **重试语义**：`retryable=true` 的错误可安全退避重试；`delivery_unknown` 等
  不可普通重试；
- **幂等**：注销、取消、重试等操作保持幂等，重放不重复产生外部副作用；
- **脱敏**：`details` 与 `message` 不包含 token、路径、私聊原文、音频原文与堆栈；
- 错误码清单以 `src/app/api/v1/` 实际实现为准，本目录与实现冲突时以实现为准并更新本文档。
