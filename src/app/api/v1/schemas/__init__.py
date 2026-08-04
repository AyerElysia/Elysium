"""API v1 公共 Pydantic schema。"""

from .auth import (
    CallerIdentity,
    LogoutResponse,
    SessionCreateRequest,
    SessionRefreshRequest,
    SessionResponse,
    WSTicketRequest,
    WSTicketResponse,
)
from .common import API_VERSION, SCHEMA_VERSION, StrictModel, VersionedModel, utc_now
from .error import ErrorBody, ErrorResponse, RecoveryHint
from .foundation import (
    AdapterStatus,
    BootstrapResponse,
    CapabilitiesResponse,
    CapabilityManifest,
    ComponentStatus,
    FeatureCapability,
    HealthResponse,
    OperationalState,
    ReadinessResponse,
)

__all__ = [
    "API_VERSION",
    "SCHEMA_VERSION",
    "AdapterStatus",
    "BootstrapResponse",
    "CallerIdentity",
    "CapabilitiesResponse",
    "CapabilityManifest",
    "ComponentStatus",
    "ErrorBody",
    "ErrorResponse",
    "FeatureCapability",
    "HealthResponse",
    "LogoutResponse",
    "OperationalState",
    "ReadinessResponse",
    "RecoveryHint",
    "SessionCreateRequest",
    "SessionRefreshRequest",
    "SessionResponse",
    "StrictModel",
    "VersionedModel",
    "WSTicketRequest",
    "WSTicketResponse",
    "utc_now",
]
