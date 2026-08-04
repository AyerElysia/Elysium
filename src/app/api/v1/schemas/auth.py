"""API v1 身份、会话和 ticket schema。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import StrictModel, VersionedModel

Audience = Literal[
    "elysium-user-frontend",
    "elysium-admin-frontend",
    "elysium-platform-service",
]
GrantType = Literal["bootstrap_challenge", "service_credential"]


class SessionCreateRequest(VersionedModel):
    """使用一次性本机 challenge 或服务凭据换取短时会话。"""

    grant_type: GrantType
    audience: Audience
    bootstrap_challenge: str | None = Field(default=None, min_length=20, max_length=4096)
    service_credential: str | None = Field(default=None, min_length=20, max_length=2048)
    origin: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_grant(self) -> "SessionCreateRequest":
        if self.grant_type == "bootstrap_challenge":
            if not self.bootstrap_challenge or self.service_credential:
                raise ValueError("bootstrap_challenge grant requires only bootstrap_challenge")
            if self.audience == "elysium-platform-service":
                raise ValueError("bootstrap challenge cannot issue platform service sessions")
            if not self.origin:
                raise ValueError("bootstrap challenge grant requires origin")
        elif not self.service_credential or self.bootstrap_challenge:
            raise ValueError("service_credential grant requires only service_credential")
        return self


class SessionRefreshRequest(VersionedModel):
    """轮换短时会话凭据。"""

    refresh_token: str = Field(min_length=20, max_length=4096)


class WSTicketRequest(VersionedModel):
    """为一个资源和子协议申请单次 WebSocket ticket。"""

    resource: str = Field(min_length=1, max_length=512)
    subprotocol: str = Field(pattern=r"^elysium\.[a-z0-9._-]+\.v[1-9][0-9]*$")
    scopes: tuple[str, ...] = Field(min_length=1, max_length=20)
    origin: str | None = Field(default=None, max_length=512)


class CallerIdentity(StrictModel):
    """Elysium 识别到的调用方身份。"""

    actor_id: str
    credential_id: str | None = None
    audience: Audience
    role: Literal["user", "administrator", "platform_service"]
    scopes: tuple[str, ...]
    resource_grants: tuple[str, ...] = ()
    session_id: str
    expires_at: datetime


class SessionResponse(StrictModel):
    """新建或刷新的短时会话。"""

    token_type: Literal["Bearer"] = "Bearer"
    access_token: str
    refresh_token: str
    expires_at: datetime
    refresh_expires_at: datetime
    identity: CallerIdentity


class WSTicketResponse(StrictModel):
    """不落入 URL 的单次 WebSocket ticket。"""

    ticket: str
    expires_at: datetime
    resource: str
    subprotocol: str
    scopes: tuple[str, ...]


class LogoutResponse(StrictModel):
    """幂等注销结果。"""

    revoked: bool
