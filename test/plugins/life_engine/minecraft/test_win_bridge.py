"""Failure-visible WSL-to-Windows bridge tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import plugins.life_engine.minecraft.win_bridge as win_bridge_module
from plugins.life_engine.minecraft.win_bridge import WinBridge, WindowsBridgeError


def test_existing_wsl_interop_needs_no_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = tmp_path / "WSLInterop"
    entry.write_text("enabled", encoding="ascii")
    monkeypatch.setattr(win_bridge_module, "_WSL_INTEROP_ENTRY", entry)
    monkeypatch.setattr(win_bridge_module, "_BINFMT_REGISTER", tmp_path / "missing")

    WinBridge._ensure_windows_interop()


def test_missing_wsl_interop_is_diagnosable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        win_bridge_module,
        "_WSL_INTEROP_ENTRY",
        tmp_path / "WSLInterop",
    )
    monkeypatch.setattr(
        win_bridge_module,
        "_BINFMT_REGISTER",
        tmp_path / "register",
    )

    with pytest.raises(WindowsBridgeError, match="unavailable"):
        WinBridge._ensure_windows_interop()
