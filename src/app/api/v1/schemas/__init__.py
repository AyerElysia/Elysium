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

__all__ = [
    "API_VERSION",
    "SCHEMA_VERSION",
    "CallerIdentity",
    "ErrorBody",
    "ErrorResponse",
    "LogoutResponse",
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
