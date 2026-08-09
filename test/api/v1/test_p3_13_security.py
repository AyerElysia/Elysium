"""P3-13 security, rate, exposure, and resource budget contracts."""

from __future__ import annotations

import asyncio

import pytest
from src.app.api.v1.security import (
    SCOPE_PERMISSION_MATRIX,
    TokenBucketRateLimiter,
    action_is_declared,
    find_public_exposures,
    sanitize_public_value,
)

from src.app.api.v1.policy import ALL_EXPORTED_SCOPES


def test_scope_resource_action_matrix_covers_every_exported_scope() -> None:
    assert set(SCOPE_PERMISSION_MATRIX) == set(ALL_EXPORTED_SCOPES)
    assert action_is_declared("events:read", resource="event:evt_1", action="read")
    assert action_is_declared("admin:audit", resource="admin:audit", action="read")
    assert not action_is_declared("events:read", resource="admin:audit", action="read")
    assert not action_is_declared("chat:read", resource="chat:stream-1", action="delete")


def test_public_exposure_scanner_rejects_secrets_paths_and_credential_urls() -> None:
    value = {
        "safe": {"state": "ready"},
        "access_token": "secret-token",
        "nested": "Authorization=Bearer secret-token",
        "local": r"C:\Users\ricer\private.db",
        "url": "https://example.test/download?token=secret",
    }
    reasons = {finding.reason for finding in find_public_exposures(value)}
    assert {"sensitive_key", "secret_assignment", "local_path", "credential_url"} <= reasons
    sanitized = sanitize_public_value(value)
    assert sanitized["access_token"] == "[REDACTED]"
    assert sanitized["nested"] == "[REDACTED]"
    assert sanitized["local"] == "[REDACTED]"
    assert sanitized["url"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_token_bucket_is_bounded_and_returns_retry_after() -> None:
    limiter = TokenBucketRateLimiter(requests_per_minute=60, burst=2, max_keys=1)
    assert (await limiter.consume("caller"))[0]
    assert (await limiter.consume("caller"))[0]
    allowed, retry_after = await limiter.consume("caller")
    assert not allowed
    assert retry_after >= 1
    assert limiter.key_count == 1
    assert len(limiter.request_key("Bearer secret", "127.0.0.1")) == 64


@pytest.mark.asyncio
async def test_command_dispatcher_bounded_concurrency_and_backlog(tmp_path) -> None:
    from src.kernel.commands import (
        CommandDispatcher,
        CommandOutcome,
        CommandStatus,
        CommandStore,
        HandlerRegistry,
    )
    from src.kernel.concurrency import TaskManager

    store = CommandStore(tmp_path / "commands.sqlite3")
    registry = HandlerRegistry()
    active = 0
    peak = 0
    release = asyncio.Event()

    async def handler(_command):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return CommandOutcome(status=CommandStatus.SUCCEEDED)

    registry.register(
        "test.command.run",
        handler,
        required_scopes=frozenset({"jobs:operate"}),
    )
    dispatcher = CommandDispatcher(
        store,
        registry=registry,
        task_manager=TaskManager(),
        max_concurrency=1,
        max_backlog=1,
    )

    def accept(key: str):
        request_hash = store.request_hash(
            command_type="test.command.run",
            schema_version=1,
            target={},
            payload={"key": key},
            correlation_id=None,
            expected_revision=None,
        )
        return store.accept(
            idempotency_key=key,
            request_hash=request_hash,
            command_type="test.command.run",
            schema_version=1,
            actor_id="actor",
            caller_role="user",
            scopes=("jobs:operate",),
            target={},
            payload={"key": key},
        )[0]

    first = accept("command-key-1")
    dispatcher.schedule(first.command_id)
    await asyncio.sleep(0)
    second = accept("command-key-2")
    with pytest.raises(RuntimeError, match="backlog"):
        dispatcher.schedule(second.command_id)
    release.set()
    await asyncio.sleep(0.05)
    assert peak == 1
    await dispatcher.close()
    store.close()
