"""阶段三已确认实施决策的机器可检查合同。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PhaseThreePolicy:
    """不得由后续实现静默改变的阶段三边界。"""

    independent_app_backend_in_scope: bool
    independent_app_frontend_direct_access: bool
    platform_service_can_request_all_exported_scopes: bool
    default_event_transport: str
    websocket_requires_bidirectional_or_binary_need: bool
    administrator_roles: tuple[str, ...]
    settings_allowlist_only: bool
    settings_may_return_secrets: bool
    settings_may_restart_processes: bool
    administrator_can_read_all_exported_views: bool
    sensitive_admin_reads_are_audited: bool
    raw_call_audio_is_persisted: bool
    raw_call_audio_history_api: bool
    commitment_semantic_crud: bool
    commitment_suggestions_are_external_until_accepted: bool
    minecraft_exported: bool
    tabletop_imports_legacy_in_memory_rooms: bool
    integration_reconnect_exported: bool
    process_control_exported: bool
    authoritative_media_delete_exported: bool
    legacy_routes_minimum_compatibility_cycles: int
    localhost_is_trusted_without_auth: bool


PHASE_THREE_POLICY = PhaseThreePolicy(
    independent_app_backend_in_scope=False,
    independent_app_frontend_direct_access=False,
    platform_service_can_request_all_exported_scopes=True,
    default_event_transport="sse",
    websocket_requires_bidirectional_or_binary_need=True,
    administrator_roles=("administrator",),
    settings_allowlist_only=True,
    settings_may_return_secrets=False,
    settings_may_restart_processes=False,
    administrator_can_read_all_exported_views=True,
    sensitive_admin_reads_are_audited=True,
    raw_call_audio_is_persisted=False,
    raw_call_audio_history_api=False,
    commitment_semantic_crud=False,
    commitment_suggestions_are_external_until_accepted=True,
    minecraft_exported=False,
    tabletop_imports_legacy_in_memory_rooms=False,
    integration_reconnect_exported=False,
    process_control_exported=False,
    authoritative_media_delete_exported=False,
    legacy_routes_minimum_compatibility_cycles=1,
    localhost_is_trusted_without_auth=False,
)

ALL_EXPORTED_SCOPES = frozenset(
    {
        "system:read",
        "capabilities:read",
        "events:read",
        "chat:read",
        "chat:write",
        "chat:moderate",
        "media:read",
        "media:write",
        "media:recognize",
        "livestream:read",
        "livestream:operate",
        "voice_call:read",
        "voice_call:operate",
        "voice_call:observe",
        "tabletop:read",
        "tabletop:play",
        "tabletop:moderate",
        "auth:session",
        "auth:ticket",
        "admin:overview",
        "admin:audit",
        "admin:logs",
        "admin:settings",
        "admin:session",
        "admin:credential",
        "sync:read",
        "sync:retry",
        "integration:read",
        "integration:test",
        "chat:admin",
        "livestream:admin",
        "voice_call:admin",
        "media:admin",
        "consciousness:read",
        "consciousness:operate",
        "world:read",
        "world:observe",
        "world:maintain",
        "memory:summary",
        "memory:read",
        "memory:maintain_projection",
        "commitments:read",
        "commitments:operate_schedule",
        "commitments:suggest",
        "autonomy:read",
        "autonomy:cancel_occurrence",
        "jobs:read",
        "jobs:operate",
        "abilities:read",
        "surface:read",
        "surface:connect",
        "surface:admin",
        "metrics:read",
        "diagnostics:read",
    }
)

PLATFORM_SERVICE_AUDIENCE = "elysium-platform-service"
USER_FRONTEND_AUDIENCE = "elysium-user-frontend"
ADMIN_FRONTEND_AUDIENCE = "elysium-admin-frontend"
