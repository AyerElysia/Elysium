"""life_engine nucleus_bash audit tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.exec_tools import LifeEngineBashTool, _audit_command


def _make_tool(tmp_path: Path) -> LifeEngineBashTool:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineBashTool(plugin=SimpleNamespace(config=config))


def test_bash_audit_allows_discard_and_fd_redirects() -> None:
    assert _audit_command("curl -s https://example.com 2>/dev/null | head -10") is None
    assert _audit_command("grep -R needle . >/dev/null 2>&1 || true") is None
    assert _audit_command("printf hi 1>&2 2>&1") is None


def test_bash_audit_blocks_file_writing_redirects() -> None:
    error = _audit_command("echo hi > out.txt")

    assert error is not None
    assert "禁止输出重定向" in error


def test_bash_tool_allows_stderr_discard_redirect(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    ok, payload = asyncio.run(
        tool.execute("ls definitely_missing 2>/dev/null || true")
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["exit_code"] == 0
    assert payload["stderr"] == ""


def test_bash_tool_blocks_file_writing_redirect(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    ok, payload = asyncio.run(tool.execute("echo hi > out.txt"))

    assert ok is False
    assert isinstance(payload, dict)
    assert "禁止输出重定向" in payload["error"]
    assert not (tmp_path / "out.txt").exists()
