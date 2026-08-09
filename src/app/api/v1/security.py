"""P3-13 public API authorization metadata, budgets, and leak guards."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from .policy import ALL_EXPORTED_SCOPES

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "app_secret",
        "authorization",
        "cookie",
        "local_path",
        "password",
        "path",
        "refresh_token",
        "service_credential",
        "access_token",
    }
)
_WINDOWS_PATH = re.compile(r"(?i)(?:^|[\s\"'])(?:[a-z]:[\\/]|\\\\)[^\s\"']+")
_POSIX_PRIVATE_PATH = re.compile(r"(?:^|[\s\"'])/(?:home|root|Users|var|tmp)/[^\s\"']+")
_CREDENTIAL_URL = re.compile(r"(?i)https?://[^\s/?#]+[^\s#]*(?:[?&](?:token|key|secret|signature|credential)=)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:authorization|api[_-]?key|app[_-]?secret|password|refresh[_-]?token|access[_-]?token)\s*[:=]\s*[^\s,;}]+"
)


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """One coarse route permission; domain facades still enforce object grants."""

    scope: str
    resources: tuple[str, ...]
    actions: tuple[str, ...]
    administrator_only: bool = False


_RULES = (
    PermissionRule("system:read", ("system:*",), ("read",)),
    PermissionRule("capabilities:read", ("capability:*",), ("read",)),
    PermissionRule("events:read", ("event:*", "stream:*", "consciousness:*"), ("read", "subscribe")),
    PermissionRule("chat:read", ("chat:*", "stream:*", "message:*"), ("read",)),
    PermissionRule("chat:write", ("chat:*", "stream:*", "message:*"), ("create", "reply", "forward")),
    PermissionRule("chat:moderate", ("chat:*", "stream:*", "message:*"), ("edit", "recall", "react", "mark_read", "poke")),
    PermissionRule("media:read", ("media:*",), ("read", "download")),
    PermissionRule("media:write", ("media:*",), ("create", "upload", "complete", "save")),
    PermissionRule("media:recognize", ("media:*",), ("recognize",)),
    PermissionRule("livestream:read", ("livestream:*",), ("read", "subscribe")),
    PermissionRule("livestream:operate", ("livestream:*",), ("start", "stop", "interrupt", "speak", "send")),
    PermissionRule("voice_call:read", ("voice_call:*",), ("read",)),
    PermissionRule("voice_call:operate", ("voice_call:*",), ("create", "resume", "interrupt", "stop", "send", "connect")),
    PermissionRule("voice_call:observe", ("voice_call:*",), ("observe",)),
    PermissionRule("tabletop:read", ("tabletop:*",), ("read", "replay")),
    PermissionRule("tabletop:play", ("tabletop:*",), ("create", "join", "leave", "start", "act", "connect")),
    PermissionRule("tabletop:moderate", ("tabletop:*",), ("moderate", "end")),
    PermissionRule("auth:session", ("session:self",), ("read", "refresh", "revoke")),
    PermissionRule("auth:ticket", ("realtime:*",), ("issue_ticket",)),
    PermissionRule("admin:overview", ("admin:overview",), ("read",), True),
    PermissionRule("admin:audit", ("admin:audit",), ("read",), True),
    PermissionRule("admin:logs", ("admin:logs",), ("read",), True),
    PermissionRule("admin:settings", ("admin:settings",), ("read", "validate", "update"), True),
    PermissionRule("admin:session", ("session:*",), ("read", "revoke"), True),
    PermissionRule("admin:credential", ("credential:*",), ("create", "rotate", "revoke"), True),
    PermissionRule("sync:read", ("sync:*",), ("read",), True),
    PermissionRule("sync:retry", ("sync:*",), ("retry",), True),
    PermissionRule("integration:read", ("integration:*",), ("read",), True),
    PermissionRule("integration:test", ("integration:*",), ("test",), True),
    PermissionRule("chat:admin", ("chat:*", "stream:*", "message:*"), ("admin",), True),
    PermissionRule("livestream:admin", ("livestream:*",), ("admin",), True),
    PermissionRule("voice_call:admin", ("voice_call:*",), ("admin", "observe"), True),
    PermissionRule("media:admin", ("media:*",), ("admin", "verify"), True),
    PermissionRule("consciousness:read", ("consciousness:*",), ("read",), True),
    PermissionRule("consciousness:operate", ("consciousness:*",), ("suspend", "resume", "drain"), True),
    PermissionRule("world:read", ("world:*",), ("read",), True),
    PermissionRule("world:observe", ("world:*",), ("observe",), True),
    PermissionRule("world:maintain", ("world:*",), ("rebuild",), True),
    PermissionRule("memory:summary", ("memory:*",), ("read_summary",), True),
    PermissionRule("memory:read", ("memory:*",), ("read",), True),
    PermissionRule("memory:maintain_projection", ("memory:*",), ("rebuild",), True),
    PermissionRule("commitments:read", ("commitment:*",), ("read",), True),
    PermissionRule("commitments:operate_schedule", ("commitment:*", "schedule:*"), ("pause", "resume", "cancel"), True),
    PermissionRule("commitments:suggest", ("commitment:*",), ("suggest",)),
    PermissionRule("autonomy:read", ("autonomy:*",), ("read",), True),
    PermissionRule("autonomy:cancel_occurrence", ("autonomy:*",), ("cancel",), True),
    PermissionRule("jobs:read", ("job:*", "command:*"), ("read",)),
    PermissionRule("jobs:operate", ("job:*", "command:*"), ("create", "cancel", "retry")),
    PermissionRule("abilities:read", ("ability:*",), ("read",)),
    PermissionRule("surface:read", ("surface:*",), ("read",)),
    PermissionRule("surface:connect", ("surface:*",), ("connect",)),
    PermissionRule("surface:input", ("surface:*",), ("input", "send"), True),
    PermissionRule("surface:admin", ("surface:*",), ("admin",), True),
    PermissionRule("metrics:read", ("metrics:*",), ("read",), True),
    PermissionRule("diagnostics:read", ("diagnostics:*",), ("read",), True),
    PermissionRule("admin:jobs", ("admin:jobs", "job:*", "command:*"), ("read", "cancel", "retry"), True),
    PermissionRule("admin:integrations", ("integration:*",), ("read", "test", "configure"), True),
)
SCOPE_PERMISSION_MATRIX = {rule.scope: rule for rule in _RULES}
if frozenset(SCOPE_PERMISSION_MATRIX) != ALL_EXPORTED_SCOPES:
    missing = sorted(ALL_EXPORTED_SCOPES - frozenset(SCOPE_PERMISSION_MATRIX))
    extra = sorted(frozenset(SCOPE_PERMISSION_MATRIX) - ALL_EXPORTED_SCOPES)
    raise RuntimeError(f"permission matrix mismatch: missing={missing}, extra={extra}")


def permission_rule(scope: str) -> PermissionRule:
    """Return the declared route permission for one exported scope."""

    try:
        return SCOPE_PERMISSION_MATRIX[scope]
    except KeyError as exc:
        raise ValueError(f"unknown exported scope: {scope}") from exc


def action_is_declared(scope: str, *, resource: str, action: str) -> bool:
    """Check the machine-readable scope x resource x action declaration."""

    rule = permission_rule(scope)
    return action in rule.actions and any(
        fnmatchcase(resource, pattern) for pattern in rule.resources
    )


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """Bounded in-memory token buckets keyed by a non-secret caller digest."""

    def __init__(
        self,
        *,
        requests_per_minute: int,
        burst: int,
        max_keys: int = 10_000,
    ) -> None:
        if requests_per_minute < 1 or burst < 1 or max_keys < 1:
            raise ValueError("rate limit values must be positive")
        self._rate = requests_per_minute / 60.0
        self._burst = float(burst)
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        """Return the current bounded key count for health and tests."""

        return len(self._buckets)

    async def consume(self, key: str) -> tuple[bool, int]:
        """Consume one request and return ``(allowed, retry_after_seconds)``."""

        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    oldest = min(
                        self._buckets,
                        key=lambda item: self._buckets[item].updated_at,
                    )
                    del self._buckets[oldest]
                bucket = _Bucket(tokens=self._burst, updated_at=now)
                self._buckets[key] = bucket
            bucket.tokens = min(
                self._burst,
                bucket.tokens + (now - bucket.updated_at) * self._rate,
            )
            bucket.updated_at = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0
            retry_after = max(1, math.ceil((1.0 - bucket.tokens) / self._rate))
            return False, retry_after

    @staticmethod
    def request_key(authorization: str | None, client_host: str | None) -> str:
        """Hash credentials so limiter state never retains bearer material."""

        source = authorization.strip() if authorization else f"anonymous:{client_host or 'unknown'}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicExposure:
    """One unsafe value discovered in a public projection."""

    location: str
    reason: str


def find_public_exposures(value: Any, *, inspect_keys: bool = True) -> tuple[PublicExposure, ...]:
    """Scan JSON-compatible output for credential material and local paths."""

    findings: list[PublicExposure] = []

    def visit(current: Any, location: str) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                key_text = str(key)
                child_location = f"{location}.{key_text}"
                if inspect_keys and key_text.strip().lower() in _SENSITIVE_KEYS:
                    findings.append(PublicExposure(child_location, "sensitive_key"))
                    continue
                visit(child, child_location)
            return
        if isinstance(current, (list, tuple)):
            for index, child in enumerate(current):
                visit(child, f"{location}[{index}]")
            return
        if not isinstance(current, str):
            return
        if _WINDOWS_PATH.search(current) or _POSIX_PRIVATE_PATH.search(current):
            findings.append(PublicExposure(location, "local_path"))
        elif _CREDENTIAL_URL.search(current):
            findings.append(PublicExposure(location, "credential_url"))
        elif _SECRET_ASSIGNMENT.search(current):
            findings.append(PublicExposure(location, "secret_assignment"))

    visit(value, "$")
    return tuple(findings)


def sanitize_public_value(value: Any) -> Any:
    """Return a JSON-compatible projection with unsafe leaves explicitly redacted."""

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text.strip().lower() in _SENSITIVE_KEYS:
                sanitized[key_text] = _REDACTED
            else:
                sanitized[key_text] = sanitize_public_value(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_public_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(sanitize_public_value(child) for child in value)
    if isinstance(value, str) and find_public_exposures(value, inspect_keys=False):
        return _REDACTED
    return value


__all__ = [
    "SCOPE_PERMISSION_MATRIX",
    "PermissionRule",
    "PublicExposure",
    "TokenBucketRateLimiter",
    "action_is_declared",
    "find_public_exposures",
    "permission_rule",
    "sanitize_public_value",
]
