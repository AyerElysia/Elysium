"""Minecraft 具身体验系统。

让爱莉以视觉感知 + VLA 运动皮层的方式游玩 Minecraft。
她的意识层决定做什么（意图），VLA 在视觉闭环中执行键鼠操作，
就像人类：你决定“挖那块矿”，手自动完成精确操作。

架构：
- win_bridge: WSL2 → Windows API 桥接（PowerShell）
- launcher: MC 安装/启动/世界管理
- capture: 游戏窗口截图（意识层高清 + VLA 低清）
- input_control: SendInput 键鼠控制
- vla_engine: VLA 模型加载/推理
- motor_loop: VLA 闭环执行 + Reflex
- consciousness: 意识层决策 + 上下文管理
- tools: nucleus_minecraft 工具
- prompts: MC 相关 prompt 模板
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
    from .consciousness import MinecraftSession
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
