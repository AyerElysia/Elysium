# Spark Gemini 经 A6 的路由与回退

截至 2026-11-06，本机 New API 的 Gemini 有效路由为：

| 项目 | 配置 |
|---|---|
| 应用入口 | NexusAI / http://localhost:3000/v1 |
| 模型 | gemini-3.8-flash |
| 有效渠道 | 53 / A6API-Gemini |
| 上游 | https://cf.a6api.net |
| 分组与优先级 | default / priority 10 / weight 10 |
| 原 D1 Gemini | 52，禁用，因上游余额不足 |
| 原全模型 A6 | 30，继续禁用；不因切换 Gemini 启用其他模型 |

API 新入口取自 A6 的公共 /api/status api_info，并已从 Spark 验证文本与工具调用。
不要使用旧 https://a6api.com 作为当前新配置入口：本次 Spark 调用返回 HTML。
接口地址可能变更，应先核对供应商自己的公共接入信息及 TLS，再携带凭据发起最小合成请求。

渠道状态、models 与 abilities 必须同时一致。
优先使用既有合法管理会话调用 New API 的渠道管理接口；不要伪造会话或修改用户凭据绕过管理认证。
本次无现成管理会话，在已有明确变更授权、在线备份和只读源码核对后，采用单事务精准更新两张表。
当前容器渠道内存缓存与 Redis 均未开启，后续请求直接读库，因而无需重启。
若部署以后开启缓存，不能照搬此结论；管理 API 会主动刷新，直接写库则须另核对生效机制。

验证分层：/api/v1/health 仅表示 Elysium 存活；
同一 NexusAI 入口的 Gemini 文本、function call 与工具结果回传成功，且消费记录命中 53，才证明中转链路。
最终自然聊天/心跳恢复仍以实际应用请求元数据确认，不通过伪造用户消息或改写历史验收。

本次备份与精确回滚状态在 data/new_api_relay/backups/gemini-a6-switch-20261105-181906/；
其中包含敏感运行数据库，保持私有权限，不得提交 Git。
回退时核对身份后在事务内禁用 53、恢复 52 原状态与 abilities；保留已发生的消费记录，
不整库覆盖、不删除渠道、不恢复无关字段。D1 欠费未解决时不应把回退当作可用修复。

故障、源证据与合成验收明细见 ../report/2026-11-05-gemini-upstream-balance.md。

2026-11-06 因原 CDN 线路出现 HTTP/2/EOF，已改为官方 CF 线路；约 9.8 万 token 的合成工具选择与回传均通过。
QQ 接口存活不代表账号在线，须另核对 OneBot get_status 的 online 字段和反向 WS。
本次恢复明细见 ../report/2026-11-06-gemini-qq-recovery.md。
