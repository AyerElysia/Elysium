"""Structured failure contracts for the unified platform tool."""

from __future__ import annotations

import json

import pytest

from plugins.life_engine.tools.platform_tools import PlatformActionTool


class _FakeProcess:
    def __init__(self, payload: dict) -> None:
        self.returncode = 1
        self._stderr = json.dumps(payload, ensure_ascii=False).encode()

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


@pytest.mark.parametrize(
    ("error", "expected_outcome"),
    [
        (
            {"type": "authentication", "subtype": "token_missing"},
            "user_action_required",
        ),
        (
            {"type": "validation", "subtype": "invalid_argument"},
            "invalid_argument",
        ),
    ],
)
async def test_feishu_cli_failure_preserves_retryability(
    monkeypatch,
    error: dict[str, str],
    expected_outcome: str,
) -> None:
    payload = {"ok": False, "error": error}

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProcess(payload)

    monkeypatch.setattr(
        "plugins.life_engine.tools.platform_tools.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    tool = PlatformActionTool.__new__(PlatformActionTool)
    success, result = await tool._execute_feishu(
        "contact +search-user --as user --query AyerElysia",
        {},
    )

    assert success is False
    assert getattr(result, "technical_outcome", "") == expected_outcome
    assert "命令失败" in str(result)


async def test_feishu_policy_block_is_terminal_without_subprocess(monkeypatch) -> None:
    async def must_not_execute(*_args, **_kwargs):
        raise AssertionError("blocked commands must not start lark-cli")

    monkeypatch.setattr(
        "plugins.life_engine.tools.platform_tools.asyncio.create_subprocess_exec",
        must_not_execute,
    )

    tool = PlatformActionTool.__new__(PlatformActionTool)
    success, result = await tool._execute_feishu("auth login", {})

    assert success is False
    assert getattr(result, "technical_outcome", "") == "policy_blocked"
