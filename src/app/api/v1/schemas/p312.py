"""P3-12 主体观察、计划与 Surface 的稳定公共 schema。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .common import VersionedModel


class P312Page(VersionedModel):
    """通用只读分页投影。"""

    items: tuple[dict[str, Any], ...] = ()
    next_cursor: str | None = None
    has_more: bool = False


class P312ActionRequest(VersionedModel):
    """带 revision 的技术生命周期命令。"""

    expected_revision: int | None = Field(default=None, ge=0)
    reason: str = Field(default="requested", min_length=1, max_length=500)


class P312ActionResponse(VersionedModel):
    """技术生命周期命令结果。"""

    accepted: bool
    result: dict[str, Any] = Field(default_factory=dict)


class P312WorldObservationRequest(VersionedModel):
    """带来源的外部观察；只追加事实，不修改断言。"""

    report: str = Field(min_length=1, max_length=64 * 1024)
    source_instance_id: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=500)
    predicate: str = Field(default="state_report", min_length=1, max_length=200)
    domain: str = Field(default="", max_length=200)
    status: str = Field(default="", max_length=100)
    stream_id: str = Field(default="", max_length=200)
    observed_at: str = Field(default="", max_length=100)
    valid_from: str = Field(default="", max_length=100)
    valid_to: str = Field(default="", max_length=100)
    supersedes_assertion_id: str = Field(default="", max_length=300)
    retracts_assertion_id: str = Field(default="", max_length=300)
    occurrence_id: str = Field(default="", max_length=300)
    assertion_id: str = Field(default="", max_length=300)
    value: Any = None


class P312ProjectionRebuildRequest(VersionedModel):
    """显式投影重建请求。"""

    batch_size: int = Field(default=500, ge=1, le=5000)


class P312MemorySearchRequest(VersionedModel):
    """记忆只读检索参数。"""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=20, ge=1, le=100)
    time_range_days: int = Field(default=0, ge=0, le=36500)
    file_types: tuple[str, ...] = ()


class P312CommitmentSuggestionRequest(VersionedModel):
    """留在主体权威之外的外部建议。"""

    suggestion: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="administrator", min_length=1, max_length=200)
    target_hint: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=2000)
    occurrence_id: str = Field(default="", max_length=300)


class P312ScheduleActionRequest(VersionedModel):
    """仅控制技术调度，不改写承诺语义。"""

    expected_revision: int | None = Field(default=None, ge=0)
    reason: str = Field(default="requested", min_length=1, max_length=500)


class P312OccurrenceCancelRequest(VersionedModel):
    """取消一个明确的技术 occurrence。"""

    reason: str = Field(default="requested", min_length=1, max_length=500)


class P312SurfaceTicketRequest(VersionedModel):
    """Surface 连接 ticket 参数。"""

    origin: str | None = Field(default=None, max_length=500)
    input_enabled: bool = False


class P312SurfaceTicketResponse(VersionedModel):
    """Surface 单次资源绑定 ticket。"""

    ticket: str
    resource: str
    subprotocol: str
    scopes: tuple[str, ...]
    expires_at: str


class P312Ability(VersionedModel):
    """安全能力目录项，不包含内部 tool manifest。"""

    ability_id: str
    name: str
    description: str
    state: str
    required_scopes: tuple[str, ...] = ()
    module: str


class P312AbilitiesResponse(VersionedModel):
    abilities: tuple[P312Ability, ...]


class P312ConnectionActionRequest(VersionedModel):
    reason: str = Field(default="requested", min_length=1, max_length=500)


class P312Envelope(VersionedModel):
    """非分页的稳定字典投影。"""

    result: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "P312AbilitiesResponse",
    "P312Ability",
    "P312ActionRequest",
    "P312ActionResponse",
    "P312CommitmentSuggestionRequest",
    "P312ConnectionActionRequest",
    "P312Envelope",
    "P312MemorySearchRequest",
    "P312OccurrenceCancelRequest",
    "P312Page",
    "P312ProjectionRebuildRequest",
    "P312ScheduleActionRequest",
    "P312SurfaceTicketRequest",
    "P312SurfaceTicketResponse",
    "P312WorldObservationRequest",
]
