"""P3-11 管理面安全投影 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .common import TimestampedModel, VersionedModel
from .foundation import AdapterStatus, ComponentStatus, OperationalState


class AdminPage(VersionedModel):
    """有界的管理查询结果。"""

    data: tuple[dict[str, Any], ...]
    count: int = Field(ge=0, le=500)
    next_cursor: str | None = Field(default=None, max_length=500)
    has_more: bool = False


class AdminOverview(VersionedModel, TimestampedModel):
    """面向管理首页的一次性聚合快照。"""

    node_id: str = Field(min_length=1, max_length=200)
    state: OperationalState
    generated_at: datetime
    components: tuple[ComponentStatus, ...]
    adapters: tuple[AdapterStatus, ...]
    command_counts: dict[str, int]
    active_incidents: int = Field(ge=0)
    audit_events: int = Field(ge=0)


class AdminComponent(VersionedModel, TimestampedModel):
    """单个组件的安全技术投影。"""

    component: ComponentStatus
    details: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime


class AdminMetric(VersionedModel, TimestampedModel):
    """不含原文和凭据的聚合指标。"""

    name: str = Field(min_length=1, max_length=120)
    value: float = Field(ge=0)
    unit: str = Field(min_length=1, max_length=40)
    generated_at: datetime


class AdminSyncStatus(VersionedModel, TimestampedModel):
    """同步 owner 的只读状态。"""

    state: OperationalState
    mode: str = Field(min_length=1, max_length=80)
    backlog: int = Field(ge=0)
    cursor: str | None = Field(default=None, max_length=500)
    last_success_at: datetime | None = None
    degraded_reason: str | None = Field(default=None, max_length=200)
    generated_at: datetime


class AdminSession(VersionedModel, TimestampedModel):
    """不含 token 的会话管理投影。"""

    session_id: str
    actor_id: str
    audience: str
    role: str
    scopes: tuple[str, ...]
    credential_id: str | None = None
    access_expires_at: datetime
    refresh_expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime


class AdminCredential(VersionedModel, TimestampedModel):
    """不含 secret/hash 的服务凭据投影。"""

    credential_id: str
    actor_id: str
    audience: str
    role: str
    scopes: tuple[str, ...]
    resource_grants: tuple[str, ...]
    created_at: datetime
    revoked_at: datetime | None = None


class AdminCredentialCreate(VersionedModel):
    """创建 service credential 的受限输入。"""

    actor_id: str = Field(min_length=1, max_length=200)
    scopes: tuple[str, ...] = Field(min_length=1, max_length=100)
    resource_grants: tuple[str, ...] = Field(default=(), max_length=100)


class AdminCredentialSecret(VersionedModel):
    """创建或轮换时一次性返回的 secret。"""

    credential: AdminCredential
    secret: str = Field(min_length=20, max_length=2048)


class AdminSetting(VersionedModel):
    """allowlist setting 的公开元数据和值。"""

    key: str
    value: Any
    source: Literal["default", "admin"]
    revision: int = Field(ge=0)
    restart_required: bool
    value_schema: dict[str, Any] = Field(serialization_alias="schema")


class AdminSettings(VersionedModel):
    """当前全部受控设置。"""

    revision: int = Field(ge=0)
    settings: tuple[AdminSetting, ...]


class AdminSettingsValidateRequest(VersionedModel):
    """只验证候选设置，不应用。"""

    values: dict[str, Any]


class AdminSettingsPatch(VersionedModel):
    """按 expected revision 更新 allowlist settings。"""

    expected_revision: int = Field(ge=0)
    values: dict[str, Any]


class AdminSettingsValidation(VersionedModel):
    """设置 schema 校验结果。"""

    valid: bool
    errors: tuple[str, ...] = ()
    values: dict[str, Any] = Field(default_factory=dict)


class AdminIntegrationTestRequest(VersionedModel):
    """受控 integration test 的明确测试类型。"""

    check: Literal["health", "capabilities", "permissions"] = "health"


class AdminCommandAccepted(VersionedModel):
    """管理命令的耐久受理结果。"""

    command_id: str
    status: str
    command_type: str
    confirmation_required: bool = True


__all__ = [
    "AdminCommandAccepted",
    "AdminComponent",
    "AdminCredential",
    "AdminCredentialCreate",
    "AdminCredentialSecret",
    "AdminIntegrationTestRequest",
    "AdminMetric",
    "AdminOverview",
    "AdminPage",
    "AdminSession",
    "AdminSetting",
    "AdminSettings",
    "AdminSettingsPatch",
    "AdminSettingsValidateRequest",
    "AdminSettingsValidation",
    "AdminSyncStatus",
]
