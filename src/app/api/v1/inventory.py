"""阶段三公共接口 inventory。

本模块是 P3-00 的机器可检查范围真相。它描述计划导出的公共合同，
不注册 FastAPI 路由，也不表示对应端点已经实现或验收。
"""

from dataclasses import dataclass
from typing import Literal

CallerIdentity = Literal[
    "user_frontend",
    "administrator",
    "platform_service",
]
EndpointStatus = Literal["planned", "experimental", "implemented", "validated"]


@dataclass(frozen=True, slots=True)
class EndpointContract:
    """一个具有明确前端消费者和授权边界的计划接口。"""

    method: str
    path: str
    domain: str
    frontend_pages: tuple[str, ...]
    caller_identities: tuple[CallerIdentity, ...]
    scopes: tuple[str, ...]
    resource_authorization: str
    implementation_anchor: str
    status: EndpointStatus = "planned"

    @property
    def key(self) -> tuple[str, str]:
        """返回 inventory 内用于去重的稳定方法／路径键。"""

        return self.method, self.path


_ALL_CALLERS: tuple[CallerIdentity, ...] = (
    "user_frontend",
    "administrator",
    "platform_service",
)
_ADMIN_CALLERS: tuple[CallerIdentity, ...] = (
    "administrator",
    "platform_service",
)


def _contracts(
    routes: str,
    *,
    domain: str,
    pages: tuple[str, ...],
    callers: tuple[CallerIdentity, ...],
    scopes: tuple[str, ...],
    resource_authorization: str,
    anchor: str,
    status: EndpointStatus = "planned",
) -> tuple[EndpointContract, ...]:
    """把紧凑的逐行路由声明转换为不可变合同。"""

    contracts: list[EndpointContract] = []
    for line in routes.strip().splitlines():
        method, path = line.split(maxsplit=1)
        contracts.append(
            EndpointContract(
                method=method,
                path=path,
                domain=domain,
                frontend_pages=pages,
                caller_identities=callers,
                scopes=scopes,
                resource_authorization=resource_authorization,
                implementation_anchor=anchor,
                status=status,
            )
        )
    return tuple(contracts)


API_INVENTORY = (
    *_contracts(
        """
POST /api/v1/auth/sessions
GET /api/v1/auth/me
POST /api/v1/auth/sessions/current:refresh
DELETE /api/v1/auth/sessions/current
POST /api/v1/auth/ws-tickets
""",
        domain="auth",
        pages=("前端启动与登录", "会话安全"),
        callers=_ALL_CALLERS,
        scopes=("auth:session", "auth:ticket"),
        resource_authorization="校验 audience、Origin、安装实例、会话或服务凭据及目标 ticket 资源",
        anchor="src/app/api/v1/runtime.py、auth_store.py、tokens.py 与 schemas/auth.py",
        status="validated",
    ),
    *_contracts(
        """
GET /api/v1/bootstrap
GET /api/v1/capabilities
GET /api/v1/readiness
GET /api/v1/health
""",
        domain="foundation",
        pages=("应用启动", "系统总览", "模块健康"),
        callers=_ALL_CALLERS,
        scopes=("system:read", "capabilities:read"),
        resource_authorization="按部署策略校验调用身份；只返回脱敏技术投影",
        anchor="src/core/transport/router/http_server.py 与已加载组件管理器；需新增聚合 facade",
    ),
    *_contracts(
        """
GET /api/v1/openapi.json
""",
        domain="foundation",
        pages=("应用启动", "系统总览", "模块健康"),
        callers=_ALL_CALLERS,
        scopes=("system:read", "capabilities:read"),
        resource_authorization="按部署策略暴露当前授权无关的技术 schema",
        anchor="src/app/api/v1/runtime.py:create_api_app",
        status="validated",
    ),
    *_contracts(
        """
GET /api/v1/events
GET /api/v1/events/{event_id}
GET /api/v1/events/stream
WS /api/v1/events/ws
POST /api/v1/event-subscriptions/validate
""",
        domain="events",
        pages=("实时事件面板", "事件审计", "断线恢复"),
        callers=_ALL_CALLERS,
        scopes=("events:read",),
        resource_authorization="按事件 visibility、调用者资源授权和已扫描账本位置过滤",
        anchor="plugins/life_engine/service/event_bus.py 与 src/kernel/sync/；需新增授权 query service",
    ),
    *_contracts(
        """
POST /api/v1/commands
GET /api/v1/commands
GET /api/v1/commands/{command_id}
POST /api/v1/commands/{command_id}:cancel
""",
        domain="commands",
        pages=("命令状态", "命令追踪", "故障处理"),
        callers=_ALL_CALLERS,
        scopes=("jobs:read", "jobs:operate"),
        resource_authorization="普通调用者仅访问自己的命令；管理员可读取正式导出的全部命令",
        anchor="需新增 src/kernel/commands/ 耐久账本与 dispatcher",
    ),
    *_contracts(
        """
GET /api/v1/chat/streams
GET /api/v1/chat/streams/{stream_id}
GET /api/v1/chat/streams/{stream_id}/messages
GET /api/v1/chat/messages/{message_id}
GET /api/v1/chat/messages/{message_id}/receipts
GET /api/v1/chat/streams/{stream_id}/members
GET /api/v1/chat/streams/{stream_id}/announcements
GET /api/v1/chat/streams/{stream_id}/files
POST /api/v1/chat/messages:send
POST /api/v1/chat/messages/{id}:reply
POST /api/v1/chat/messages/{id}:edit
POST /api/v1/chat/messages/{id}:recall
POST /api/v1/chat/messages/{id}/reactions
DELETE /api/v1/chat/messages/{id}/reactions/{reaction}
POST /api/v1/chat/messages/{id}:mark-read
POST /api/v1/chat/messages:forward
POST /api/v1/chat/streams/{stream_id}/poke
""",
        domain="chat",
        pages=("会话列表", "聊天窗口", "消息详情"),
        callers=_ALL_CALLERS,
        scopes=("chat:read", "chat:write"),
        resource_authorization="校验 stream 参与者、消息可见性、actor 所有权与 provider capability",
        anchor="src/app/plugin_system/api/message_api.py、send_api.py 与 MessageSender；需新增 chat facade",
    ),
    *_contracts(
        """
POST /api/v1/media/uploads
PUT /api/v1/media/uploads/{upload_id}
POST /api/v1/media/uploads/{upload_id}:complete
GET /api/v1/media/{media_id}
GET /api/v1/media/{media_id}/content
POST /api/v1/media/{media_id}:save
POST /api/v1/media/{media_id}:recognize
GET /api/v1/media/{media_id}/derivatives
""",
        domain="media",
        pages=("媒体查看器", "上传与识别", "媒体资产"),
        callers=_ALL_CALLERS,
        scopes=("media:read", "media:write", "media:recognize"),
        resource_authorization="校验媒体 owner、来源消息授权、状态、hash、MIME 与受管对象身份",
        anchor="src/core/models/media.py 与 src/app/plugin_system/api/media_api.py；需新增受管媒体 store",
    ),
    *_contracts(
        """
GET /api/v1/livestream/status
GET /api/v1/livestream/sessions
GET /api/v1/livestream/sessions/{session_id}
GET /api/v1/livestream/sessions/{session_id}/events
POST /api/v1/livestream/session:start
POST /api/v1/livestream/session:stop
POST /api/v1/livestream/session:interrupt
POST /api/v1/livestream/speech:request
POST /api/v1/livestream/danmaku:send
WS /api/v1/livestream/stage/ws
""",
        domain="livestream",
        pages=("直播间", "互动区", "直播控制台", "舞台与字幕"),
        callers=_ALL_CALLERS,
        scopes=("livestream:read", "livestream:operate", "livestream:admin"),
        resource_authorization="按 session、observer/operator 身份、舞台 ticket 和平台 capability 投影",
        anchor="plugins/livestream/runtime.py、ledger.py 与 router.py；需新增稳定 facade",
    ),
    *_contracts(
        """
POST /api/v1/voice-calls
GET /api/v1/voice-calls/{call_id}
POST /api/v1/voice-calls/{call_id}:resume
POST /api/v1/voice-calls/{call_id}:interrupt
POST /api/v1/voice-calls/{call_id}:end
POST /api/v1/voice-calls/{call_id}/text
GET /api/v1/voice-calls/{call_id}/transcripts
POST /api/v1/voice-calls/{call_id}/tickets
WS /api/v1/voice-calls/{call_id}/ws
WS /api/v1/voice-calls/{call_id}/observe
""",
        domain="voice_calls",
        pages=("实时语音通话", "字幕与状态"),
        callers=_ALL_CALLERS,
        scopes=("voice_call:read", "voice_call:operate", "voice_call:observe"),
        resource_authorization="校验 call participant／observer、单次 ticket、Origin 与 transcript visibility",
        anchor="plugins/voice_live/protocol.py、session.py 与 router.py；需补耐久元数据 facade",
    ),
    *_contracts(
        """
GET /api/v1/tabletop/games
POST /api/v1/tabletop/rooms
GET /api/v1/tabletop/rooms/{room_id}
POST /api/v1/tabletop/rooms/{room_id}:join
POST /api/v1/tabletop/rooms/{room_id}:leave
POST /api/v1/tabletop/rooms/{room_id}:start
POST /api/v1/tabletop/rooms/{room_id}:end
GET /api/v1/tabletop/rooms/{room_id}/events
GET /api/v1/tabletop/rooms/{room_id}/view
POST /api/v1/tabletop/rooms/{room_id}/actions
GET /api/v1/tabletop/rooms/{room_id}/replay
WS /api/v1/tabletop/rooms/{room_id}/ws
""",
        domain="tabletop",
        pages=("狼人杀大厅", "房间", "玩家私密操作", "复盘"),
        callers=_ALL_CALLERS,
        scopes=("tabletop:read", "tabletop:play"),
        resource_authorization="由 engine 按当前 actor、房间成员、角色、阶段和事件 visibility 生成视图",
        anchor="plugins/werewolf_game/engine.py 与 service.py；需新增 projection、ledger 和 recovery",
        status="experimental",
    ),
    *_contracts(
        """
GET /api/v1/admin/overview
GET /api/v1/admin/components
GET /api/v1/admin/components/{component_id}
GET /api/v1/admin/metrics
GET /api/v1/admin/incidents
GET /api/v1/admin/audit-events
GET /api/v1/admin/audit-events/{audit_id}
GET /api/v1/admin/logs
GET /api/v1/admin/sync
POST /api/v1/admin/sync:retry
""",
        domain="admin_system",
        pages=("系统总览", "模块健康", "同步与积压", "安全审计"),
        callers=_ADMIN_CALLERS,
        scopes=("admin:overview", "admin:audit", "admin:logs", "sync:read", "sync:retry", "metrics:read"),
        resource_authorization="要求全能管理员或 platform service；查询只读脱敏，重试不得跳游标",
        anchor="组件管理器、src/kernel/sync/ 与受管日志投影；需新增 admin query facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/auth/sessions
DELETE /api/v1/admin/auth/sessions/{session_id}
GET /api/v1/admin/credentials
POST /api/v1/admin/credentials
POST /api/v1/admin/credentials/{credential_id}:rotate
DELETE /api/v1/admin/credentials/{credential_id}
GET /api/v1/admin/settings
PATCH /api/v1/admin/settings
POST /api/v1/admin/settings:validate
""",
        domain="admin_access",
        pages=("会话管理", "服务凭据", "受控设置"),
        callers=_ADMIN_CALLERS,
        scopes=("admin:session", "admin:credential", "admin:settings"),
        resource_authorization="要求全能管理员；凭据按 id 撤销，设置限 allowlist 与 expected revision",
        anchor="需新增 auth credential store、session store 与 allowlist settings facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/integrations
GET /api/v1/admin/integrations/{id}
GET /api/v1/admin/integrations/{id}/events
POST /api/v1/admin/integrations/{id}:test
""",
        domain="admin_integrations",
        pages=("平台连接", "Adapter 权限诊断"),
        callers=_ADMIN_CALLERS,
        scopes=("integration:read", "integration:test"),
        resource_authorization="要求全能管理员；test 只能调用登记 owner 的无副作用或最小副作用检查",
        anchor="Adapter／插件生命周期和 capability；需新增 integration facade，不导出 reconnect",
    ),
    *_contracts(
        """
GET /api/v1/admin/jobs
GET /api/v1/admin/jobs/{job_id}
POST /api/v1/admin/jobs/{job_id}:cancel
POST /api/v1/admin/jobs/{job_id}:retry
""",
        domain="admin_jobs",
        pages=("后台任务状态", "失败任务处理"),
        callers=_ADMIN_CALLERS,
        scopes=("jobs:read", "jobs:operate"),
        resource_authorization="要求全能管理员并校验 job 状态机、owner、可取消与幂等重试资格",
        anchor="项目任务管理器与各领域 job owner；需新增耐久 job projection",
    ),
    *_contracts(
        """
GET /api/v1/admin/chat/streams
GET /api/v1/admin/chat/messages
GET /api/v1/admin/chat/streams/{stream_id}/members
GET /api/v1/admin/chat/streams/{stream_id}/announcements
GET /api/v1/admin/chat/streams/{stream_id}/files
GET /api/v1/admin/chat/requests
GET /api/v1/admin/chat/moderation-events
POST /api/v1/admin/chat/streams/{stream_id}/members/{member_id}:mute
POST /api/v1/admin/chat/streams/{stream_id}/members/{member_id}:unmute
POST /api/v1/admin/chat/streams/{stream_id}/members/{member_id}:remove
POST /api/v1/admin/chat/streams/{stream_id}/members/{member_id}:set-role
POST /api/v1/admin/chat/requests/{request_id}:approve
POST /api/v1/admin/chat/requests/{request_id}:reject
POST /api/v1/admin/chat/messages/{message_id}:recall
POST /api/v1/admin/chat/streams/{stream_id}/announcements
DELETE /api/v1/admin/chat/streams/{stream_id}/announcements/{id}
POST /api/v1/admin/chat/messages/{message_id}:pin
POST /api/v1/admin/chat/messages/{message_id}:unpin
""",
        domain="admin_chat",
        pages=("消息管理", "群组与成员", "公告与申请"),
        callers=_ADMIN_CALLERS,
        scopes=("chat:admin", "chat:moderate"),
        resource_authorization="要求全能管理员，同时校验 provider capability、平台硬权限、目标与 revision",
        anchor="NapCat client、plugins/feishu_adapter/actions.py；需封装 allowlist chat admin facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/voice-calls
GET /api/v1/admin/voice-calls/{call_id}
GET /api/v1/admin/voice-calls/{call_id}/transcripts
POST /api/v1/admin/voice-calls/{call_id}:interrupt
POST /api/v1/admin/voice-calls/{call_id}:end
WS /api/v1/admin/voice-calls/{call_id}/observe
""",
        domain="admin_voice_calls",
        pages=("通话监督", "转写与指标"),
        callers=_ADMIN_CALLERS,
        scopes=("voice_call:admin",),
        resource_authorization="要求全能管理员；observe 保持只读，高敏 transcript 读取写审计",
        anchor="plugins/voice_live/session.py 与 router.py；需新增监督 facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/media
GET /api/v1/admin/media/{media_id}
GET /api/v1/admin/media/{media_id}/references
GET /api/v1/admin/media/{media_id}/access-events
POST /api/v1/admin/media/{media_id}:verify
POST /api/v1/admin/media/{media_id}:recognize
POST /api/v1/admin/media/{media_id}:quarantine
POST /api/v1/admin/media/{media_id}:restore
GET /api/v1/admin/media/cleanup-candidates
""",
        domain="admin_media",
        pages=("媒体资产", "完整性与访问审计"),
        callers=_ADMIN_CALLERS,
        scopes=("media:admin",),
        resource_authorization="要求全能管理员；操作仅限已登记 media_id，不提供权威引用媒体删除",
        anchor="受管媒体 store 与 media_api.py；需新增引用、隔离和完整性 facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/consciousness/instances
GET /api/v1/admin/consciousness/instances/{instance_id}
GET /api/v1/admin/consciousness/streams/{stream_id}/owner
GET /api/v1/admin/consciousness/health
POST /api/v1/admin/consciousness/instances/{instance_id}:suspend
POST /api/v1/admin/consciousness/instances/{instance_id}:resume
POST /api/v1/admin/consciousness/instances/{instance_id}:drain
""",
        domain="admin_consciousness",
        pages=("意识窗口", "Presence 与 owner"),
        callers=_ADMIN_CALLERS,
        scopes=("consciousness:read", "consciousness:operate"),
        resource_authorization="要求全能管理员；关键实例额外保护并使用 expected revision",
        anchor="plugins/life_engine/service/consciousness.py 与 presence_store.py",
    ),
    *_contracts(
        """
GET /api/v1/admin/world/assertions
GET /api/v1/admin/world/changes
GET /api/v1/admin/world/health
POST /api/v1/admin/world/observations
POST /api/v1/admin/world/projection:rebuild
""",
        domain="admin_world",
        pages=("世界观察", "冲突与投影健康"),
        callers=_ADMIN_CALLERS,
        scopes=("world:read", "world:observe", "world:maintain"),
        resource_authorization="要求全能管理员；观察只追加，rebuild 只处理可重建投影",
        anchor="plugins/life_engine/service/world_projection.py 与 Life Event ledger",
    ),
    *_contracts(
        """
GET /api/v1/admin/memory/search
GET /api/v1/admin/memory/experiences/{id}
GET /api/v1/admin/memory/artifacts/{id}/versions
GET /api/v1/admin/memory/artifacts/{id}/versions/{version}
GET /api/v1/admin/memory/graph
GET /api/v1/admin/memory/stats
GET /api/v1/admin/memory/health
POST /api/v1/admin/memory/projections/{projection}:rebuild
""",
        domain="admin_memory",
        pages=("记忆观察", "版本与投影维护"),
        callers=_ADMIN_CALLERS,
        scopes=("memory:summary", "memory:read", "memory:maintain_projection"),
        resource_authorization="要求全能管理员并审计原文读取；rebuild 不修改权威历史或主体文件",
        anchor="plugins/life_engine/memory/ 只读查询与投影 owner；需新增安全 facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/commitments/todos
GET /api/v1/admin/commitments/todos/{todo_id}
GET /api/v1/admin/commitments/todos/{todo_id}/events
GET /api/v1/admin/commitments/schedules
GET /api/v1/admin/commitments/schedules/{record_id}
POST /api/v1/admin/commitment-suggestions
POST /api/v1/admin/commitments/schedules/{record_id}:pause
POST /api/v1/admin/commitments/schedules/{record_id}:resume
""",
        domain="admin_commitments",
        pages=("TODO 与承诺", "定时计划"),
        callers=_ADMIN_CALLERS,
        scopes=("commitments:read", "commitments:operate_schedule", "commitments:suggest"),
        resource_authorization="要求全能管理员；建议留在主体权威外，pause／resume 只控制技术执行",
        anchor="plugins/life_engine/tools/todo_tools.py 与 schedule_tools.py；需新增只读 facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/autonomy/intents
GET /api/v1/admin/autonomy/intents/{intent_id}
GET /api/v1/admin/autonomy/intents/{intent_id}/occurrences
POST /api/v1/admin/autonomy/occurrences/{occurrence_id}:cancel
""",
        domain="admin_autonomy",
        pages=("自主执行状态", "Occurrence 历史"),
        callers=_ADMIN_CALLERS,
        scopes=("autonomy:read", "autonomy:cancel_occurrence"),
        resource_authorization="要求全能管理员；cancel 仅阻止可安全取消的当前技术执行",
        anchor="plugins/life_engine/autonomy.py；需新增安全状态 projection",
    ),
    *_contracts(
        """
GET /api/v1/surfaces
GET /api/v1/surfaces/{surface_id}/status
POST /api/v1/surfaces/{surface_id}/tickets
WS /api/v1/surfaces/{surface_id}/ws
""",
        domain="surfaces",
        pages=("Neko 展示与交互",),
        callers=_ALL_CALLERS,
        scopes=("surface:read", "surface:connect"),
        resource_authorization="按 surface owner、observer/input scope 与单次 ticket 校验",
        anchor="plugins/neko_surface/router.py 与 elysia.surface.v1 协议",
    ),
    *_contracts(
        """
GET /api/v1/admin/surfaces/{surface_id}/connections
POST /api/v1/admin/surfaces/{surface_id}/connections/{connection_id}:disconnect
""",
        domain="admin_surfaces",
        pages=("Surface 连接管理",),
        callers=_ADMIN_CALLERS,
        scopes=("surface:admin",),
        resource_authorization="要求全能管理员并校验目标 Surface 与连接身份",
        anchor="plugins/neko_surface/router.py；需新增管理连接 facade",
    ),
    *_contracts(
        """
GET /api/v1/admin/tabletop/rooms
GET /api/v1/admin/tabletop/rooms/{room_id}/moderator-view
GET /api/v1/admin/tabletop/rooms/{room_id}/integrity
POST /api/v1/admin/tabletop/rooms/{room_id}:pause
POST /api/v1/admin/tabletop/rooms/{room_id}:resume
POST /api/v1/admin/tabletop/rooms/{room_id}:end
POST /api/v1/admin/tabletop/rooms/{room_id}:recover
""",
        domain="admin_tabletop",
        pages=("狼人杀裁判台", "房间恢复"),
        callers=_ADMIN_CALLERS,
        scopes=("tabletop:moderate",),
        resource_authorization="要求全能管理员；裁判视图审计，recover 只从新 API 权威 ledger 重建",
        anchor="plugins/werewolf_game/engine.py；需新增 moderator projection、ledger 与 recovery",
        status="experimental",
    ),
    *_contracts(
        """
GET /api/v1/abilities
GET /api/v1/abilities/{ability_id}
""",
        domain="abilities",
        pages=("能力目录",),
        callers=_ALL_CALLERS,
        scopes=("abilities:read",),
        resource_authorization="按调用身份与公共 capability 返回安全说明，不暴露任意工具执行",
        anchor="已加载插件 manifest 与领域 capability；需新增独立公共 abilities projection",
    ),
)

API_INVENTORY_BY_KEY = {contract.key: contract for contract in API_INVENTORY}
