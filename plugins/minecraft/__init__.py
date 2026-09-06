"""Independent Minecraft plugin: bodies, scene consciousness and event adapters.

The plugin owns session lifecycle and game protocol implementations. Shared
identity, Presence, subject projections and durable Life Events remain provided
by life_engine through its public scene integration contracts. Loading this
package does not launch a game or construct a second persona or memory store.
"""

from __future__ import annotations

__all__ = [
    "MinecraftSession",
    "MinecraftLauncher",
    "WindowCapture",
    "InputController",
    "WinBridge",
]


def _lazy_import():
    """延迟导入避免启动时加载重量级依赖。"""
    from .launcher import MinecraftLauncher
    from .capture import WindowCapture
    from .input_control import InputController
    from .session import MinecraftSession
    from .win_bridge import WinBridge

    return MinecraftLauncher, WindowCapture, InputController, MinecraftSession, WinBridge


def __getattr__(name: str):
    mapping = {
        "MinecraftSession": 3,
        "MinecraftLauncher": 0,
        "WindowCapture": 1,
        "InputController": 2,
        "WinBridge": 4,
    }
    if name in mapping:
        classes = _lazy_import()
        return classes[mapping[name]]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
