"""P3-00 阶段三接口范围与确认决策契约测试。"""

import re
from pathlib import Path

from src.app.api.v1.inventory import API_INVENTORY, API_INVENTORY_BY_KEY
from src.app.api.v1.policy import ALL_EXPORTED_SCOPES, PHASE_THREE_POLICY

ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "docs" / "architecture" / "阶段三-Elysium应用后端接口导出开发步骤.md"
TABLE_ROUTE_PATTERN = re.compile(
    r"\|\s*(GET|POST|PUT|PATCH|DELETE|WS)\s*\|\s*`(/api/v1[^` ]*)`"
)
BULLET_ROUTE_PATTERN = re.compile(
    r"`(GET|POST|PUT|PATCH|DELETE|WS)\s+(/api/v1/[^`]+)`"
)


def _documented_routes() -> set[tuple[str, str]]:
    text = PLAN_PATH.read_text(encoding="utf-8")
    return set(TABLE_ROUTE_PATTERN.findall(text)) | set(
        BULLET_ROUTE_PATTERN.findall(text)
    )


def test_inventory_covers_every_documented_frontend_route() -> None:
    """权威计划中的每个正式方法／路径都必须在 inventory 中。"""

    assert set(API_INVENTORY_BY_KEY) == _documented_routes()


def test_inventory_keys_are_unique_and_complete() -> None:
    """每个合同必须有唯一键和 P3-00 规定的所有元数据。"""

    assert len(API_INVENTORY_BY_KEY) == len(API_INVENTORY)
    for contract in API_INVENTORY:
        assert contract.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "WS"}
        assert contract.path.startswith("/api/v1/") or contract.path == "/api/v1/bootstrap"
        assert contract.frontend_pages
        assert contract.caller_identities
        assert contract.scopes
        assert contract.resource_authorization.strip()
        assert contract.implementation_anchor.strip()
        assert contract.status in {"planned", "experimental", "implemented", "validated"}


def test_every_scope_is_declared_by_phase_three_policy() -> None:
    """inventory 不能暗中引入权限矩阵外的 scope。"""

    inventory_scopes = {
        scope for contract in API_INVENTORY for scope in contract.scopes
    }
    assert inventory_scopes <= ALL_EXPORTED_SCOPES


def test_admin_routes_reject_user_frontend_identity_by_contract() -> None:
    """所有管理路由都必须排除普通用户前端身份。"""

    admin_routes = [
        contract for contract in API_INVENTORY if contract.path.startswith("/api/v1/admin/")
    ]
    assert admin_routes
    assert all(
        "user_frontend" not in contract.caller_identities for contract in admin_routes
    )


def test_platform_service_can_reach_every_exported_contract() -> None:
    """独立应用后端作为平台 service 可申请全部阶段三合同。"""

    assert PHASE_THREE_POLICY.platform_service_can_request_all_exported_scopes
    assert all(
        "platform_service" in contract.caller_identities
        for contract in API_INVENTORY
    )


def test_excluded_internal_capabilities_are_not_exported() -> None:
    """P3-00 不得混入没有前端消费者的内部或主体语义能力。"""

    paths = {contract.path.lower() for contract in API_INVENTORY}
    forbidden_fragments = {
        "minecraft",
        "terminal",
        "process",
        "plugin/reload",
        "integration/reconnect",
        "tool/execute",
        "soul.md",
        "memory/delete",
        "media/{media_id}:delete",
        "voice-calls/{call_id}/recording",
    }
    assert not {
        fragment
        for fragment in forbidden_fragments
        if any(fragment in path for path in paths)
    }


def test_tabletop_routes_remain_experimental_until_recovery_exists() -> None:
    """新狼人杀 API 在耐久恢复完成前不能标为已实现或已验收。"""

    tabletop = [
        contract
        for contract in API_INVENTORY
        if contract.domain in {"tabletop", "admin_tabletop"}
    ]
    assert tabletop
    assert {contract.status for contract in tabletop} == {"experimental"}


def test_all_fourteen_confirmed_decisions_are_frozen() -> None:
    """第 29 节十四项确认决策必须映射为可执行断言。"""

    policy = PHASE_THREE_POLICY
    assert not policy.independent_app_backend_in_scope
    assert not policy.independent_app_frontend_direct_access
    assert policy.platform_service_can_request_all_exported_scopes
    assert policy.default_event_transport == "sse"
    assert policy.websocket_requires_bidirectional_or_binary_need
    assert policy.administrator_roles == ("administrator",)
    assert policy.settings_allowlist_only
    assert not policy.settings_may_return_secrets
    assert not policy.settings_may_restart_processes
    assert policy.administrator_can_read_all_exported_views
    assert policy.sensitive_admin_reads_are_audited
    assert not policy.raw_call_audio_is_persisted
    assert not policy.raw_call_audio_history_api
    assert not policy.commitment_semantic_crud
    assert policy.commitment_suggestions_are_external_until_accepted
    assert not policy.minecraft_exported
    assert not policy.tabletop_imports_legacy_in_memory_rooms
    assert not policy.integration_reconnect_exported
    assert not policy.process_control_exported
    assert not policy.authoritative_media_delete_exported
    assert policy.legacy_routes_minimum_compatibility_cycles >= 1
    assert not policy.localhost_is_trusted_without_auth
