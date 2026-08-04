"""API v1 基础发现、能力与就绪状态 schema。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .auth import CallerIdentity
from .common import TimestampedModel, VersionedModel

OperationalState = Literal[
    "disabled",
    "unavailable",
    "degraded",
    "ready",
    "failed",
]


class FeatureCapability(VersionedModel):
    """一个稳定的公共 feature 合同及当前调用方授权投影。"""

    supported: bool
    scope: str = Field(min_length=1, max_length=100)
    authorized: bool


class CapabilityManifest(VersionedModel):
    """独立于内部 LLM 工具清单的公共领域能力 manifest。"""

    module: str = Field(min_length=1, max_length=100)
    available: bool
    state: OperationalState
    contract_version: str = Field(pattern=r"^\d+\.\d+$")
    provider: str | None = Field(default=None, max_length=100)
    features: dict[str, FeatureCapability] = Field(default_factory=dict)
    degraded_reason: str | None = Field(default=None, max_length=200)


class ComponentStatus(VersionedModel):
    """不含凭据、路径和原文的组件技术状态。"""

    component: str = Field(min_length=1, max_length=120)
    state: OperationalState
    enabled: bool
    owner: str = Field(min_length=1, max_length=120)
    backlog: int | None = Field(default=None, ge=0)
    last_success_at: datetime | None = None
    degraded_reason: str | None = Field(default=None, max_length=200)


class AdapterStatus(VersionedModel):
    """平台 Adapter 的只读连接状态。"""

    provider: str = Field(min_length=1, max_length=100)
    component: str = Field(min_length=1, max_length=120)
    state: OperationalState
    enabled: bool
    connected: bool
    degraded_reason: str | None = Field(default=None, max_length=200)


class BootstrapResponse(VersionedModel, TimestampedModel):
    """前端启动所需的最小、已鉴权技术投影。"""

    api_version: str
    elysium_version: str
    node_id: str = Field(min_length=1, max_length=200)
    identity: CallerIdentity
    modules: tuple[ComponentStatus, ...]
    generated_at: datetime


class CapabilitiesResponse(VersionedModel, TimestampedModel):
    """当前公共模块和 feature-level capability 列表。"""

    api_version: str
    node_id: str = Field(min_length=1, max_length=200)
    capabilities: tuple[CapabilityManifest, ...]
    generated_at: datetime


class ReadinessResponse(VersionedModel, TimestampedModel):
    """本地就绪状态与可选依赖降级投影。"""

    api_version: str
    elysium_version: str
    node_id: str = Field(min_length=1, max_length=200)
    state: OperationalState
    local_ready: bool
    dependencies: tuple[ComponentStatus, ...]
    adapters: tuple[AdapterStatus, ...]
    migration_version: str = Field(min_length=1, max_length=100)
    generated_at: datetime


class HealthResponse(VersionedModel, TimestampedModel):
    """快速、只读且不触发修复的 API 存活响应。"""

    api_version: str
    node_id: str = Field(min_length=1, max_length=200)
    state: OperationalState
    alive: bool
    generated_at: datetime


__all__ = [
    "AdapterStatus",
    "BootstrapResponse",
    "CapabilitiesResponse",
    "CapabilityManifest",
    "ComponentStatus",
    "FeatureCapability",
    "HealthResponse",
    "OperationalState",
    "ReadinessResponse",
]
