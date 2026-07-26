"""键鼠控制模块。

通过 WinBridge (PowerShell + SendInput) 向 Minecraft 窗口发送键盘和鼠标输入。
VLA 输出的原子动作通过此模块执行。

WSL2 环境：使用 Windows SendInput API 代替 xdotool。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .win_bridge import WinBridge, get_bridge

logger = logging.getLogger("life_engine.minecraft.input")


class ActionType(str, Enum):
    """VLA 动作类型。"""

    MOVE = "move"          # 鼠标移动（视角）
    CLICK = "click"        # 鼠标点击
    KEY = "key"            # 按键
    KEY_HOLD = "key_hold"  # 按住按键
    SCROLL = "scroll"      # 滚轮（切换快捷栏）
    NOOP = "noop"          # 等待
    DONE = "done"          # 子目标完成


@dataclass(slots=True)
class Action:
    """一个原子动作。"""

    type: ActionType
    # 鼠标移动
    dx: int = 0
    dy: int = 0
    # 点击/按键
    button: str = ""       # "left" / "right" / "middle"
    key: str = ""          # "w", "a", "s", "d", "space", "shift", "e", "q", "1"-"9"
    # 按住时长（秒）
    duration: float = 0.0
    # 滚轮
    scroll_amount: int = 0
    # 附加信息
    info: str = ""

    @classmethod
    def move(cls, dx: int, dy: int) -> "Action":
        return cls(type=ActionType.MOVE, dx=dx, dy=dy)

    @classmethod
    def click(cls, button: str = "left") -> "Action":
        return cls(type=ActionType.CLICK, button=button)

    @classmethod
    def press(cls, key: str) -> "Action":
        return cls(type=ActionType.KEY, key=key)

    @classmethod
    def key_hold(cls, key: str, duration: float) -> "Action":
        return cls(type=ActionType.KEY_HOLD, key=key, duration=duration)

    @classmethod
    def scroll(cls, amount: int) -> "Action":
        return cls(type=ActionType.SCROLL, scroll_amount=amount)

    @classmethod
    def noop(cls) -> "Action":
        return cls(type=ActionType.NOOP)

    @classmethod
    def done(cls, info: str = "") -> "Action":
        return cls(type=ActionType.DONE, info=info)


# MC 键位由 WinBridge 内部映射处理


class InputController:
    """键鼠控制器（WSL2 + Windows SendInput）。

    通过 WinBridge 向 MC 窗口发送输入。
    安全锁：只在 MC 窗口运行时发送输入。
    """

    def __init__(self, bridge: WinBridge | None = None) -> None:
        self._bridge = bridge or get_bridge()
        self._lock = asyncio.Lock()
        self._enabled = True
        self._window_info: dict[str, Any] | None = None

    @property
    def window_info(self) -> dict[str, Any] | None:
        return self._window_info

    @window_info.setter
    def window_info(self, info: dict[str, Any] | None) -> None:
        self._window_info = info

    def set_enabled(self, enabled: bool) -> None:
        """启用/禁用输入（安全开关）。"""
        self._enabled = enabled

    async def execute(self, action: Action) -> bool:
        """执行一个原子动作。"""
        if not self._enabled:
            return False

        async with self._lock:
            # 安全检查：确保 MC 在运行
            if not await self._bridge.is_running():
                logger.warning("MC 未运行，跳过输入")
                return False

            match action.type:
                case ActionType.MOVE:
                    return await self._mouse_move(action.dx, action.dy)
                case ActionType.CLICK:
                    return await self._mouse_click(action.button)
                case ActionType.KEY:
                    return await self._press_key(action.key)
                case ActionType.KEY_HOLD:
                    return await self._hold_key(action.key, action.duration)
                case ActionType.SCROLL:
                    return await self._scroll(action.scroll_amount)
                case ActionType.NOOP:
                    return True
                case ActionType.DONE:
                    return True
        return False

    async def execute_sequence(self, actions: list[Action], interval: float = 0.05) -> bool:
        """执行动作序列。"""
        for action in actions:
            ok = await self.execute(action)
            if not ok and action.type not in (ActionType.NOOP, ActionType.DONE):
                return False
            if interval > 0:
                await asyncio.sleep(interval)
        return True

    async def _mouse_move(self, dx: int, dy: int) -> bool:
        """鼠标相对移动（视角转动）。"""
        if dx == 0 and dy == 0:
            return True
        return await self._bridge.mouse_move(dx, dy)

    async def _mouse_click(self, button: str = "left") -> bool:
        """鼠标点击（在窗口中心）。"""
        if not self._window_info:
            self._window_info = await self._bridge.find_window()
        if not self._window_info:
            return False
        # 点击窗口中心
        cx = self._window_info["x"] + self._window_info["w"] // 2
        cy = self._window_info["y"] + self._window_info["h"] // 2
        return await self._bridge.click_at(cx, cy, button)

    async def _press_key(self, key: str) -> bool:
        """按下并释放一个键。"""
        return await self._bridge.press_key(key)

    async def _hold_key(self, key: str, duration: float) -> bool:
        """按住一个键一段时间。"""
        duration_ms = int(duration * 1000)
        return await self._bridge.hold_key(key, duration_ms)

    async def _scroll(self, amount: int) -> bool:
        """滚轮（切换快捷栏）。正数向上，负数向下。"""
        return await self._bridge.scroll(amount)

    # === 高层便捷方法 ===

    async def walk_forward(self, duration: float = 2.0) -> bool:
        """向前走。"""
        return await self._hold_key("w", duration)

    async def walk_backward(self, duration: float = 1.5) -> bool:
        """向后退。"""
        return await self._hold_key("s", duration)

    async def strafe_left(self, duration: float = 1.0) -> bool:
        """向左平移。"""
        return await self._hold_key("a", duration)

    async def strafe_right(self, duration: float = 1.0) -> bool:
        """向右平移。"""
        return await self._hold_key("d", duration)

    async def jump(self) -> bool:
        """跳跃。"""
        return await self._press_key("space")

    async def sneak(self, duration: float = 1.0) -> bool:
        """潜行。"""
        return await self._hold_key("shift", duration)

    async def mine(self, hold: float = 1.5) -> bool:
        """挖掘（按住左键）。"""
        if not self._window_info:
            self._window_info = await self._bridge.find_window()
        if not self._window_info:
            return False
        cx = self._window_info["x"] + self._window_info["w"] // 2
        cy = self._window_info["y"] + self._window_info["h"] // 2
        # 先移动鼠标到窗口中心，再按住左键
        await self._bridge.click_at(cx, cy, "left")  # 先激活焦点
        await asyncio.sleep(0.1)
        return await self._bridge.hold_mouse("left", int(hold * 1000))

    async def place_block(self) -> bool:
        """放置方块（右键）。"""
        return await self._mouse_click("right")

    async def attack(self) -> bool:
        """攻击（左键）。"""
        return await self._mouse_click("left")

    async def open_inventory(self) -> bool:
        """打开物品栏。"""
        return await self._press_key("e")

    async def drop_item(self) -> bool:
        """丢弃物品。"""
        return await self._press_key("q")

    async def select_slot(self, slot: int) -> bool:
        """选择快捷栏槽位 (1-9)。"""
        if 1 <= slot <= 9:
            return await self._press_key(str(slot))
        return False

    async def type_chat(self, message: str) -> bool:
        """在游戏内发送聊天消息。

        流程：按 T 开聊天框 → 粘贴文字 → 按 Enter 发送。
        支持中文及所有 Unicode 字符。
        """
        try:
            # 1. 打开聊天框
            if not await self._press_key("t"):
                return False
            await asyncio.sleep(0.25)

            # 2. 粘贴文字（剪贴板法，支持中文）
            if not await self._bridge.type_text(message):
                # 发送失败，按 Escape 关闭聊天框
                await self._press_key("escape")
                return False
            await asyncio.sleep(0.15)

            # 3. 按 Enter 发送
            if not await self._press_key("enter"):
                return False

            logger.info(f"聊天消息已发送: {message[:40]}{'...' if len(message) > 40 else ''}")
            return True
        except Exception as exc:
            logger.warning(f"发送聊天失败: {exc}")
            try:
                await self._press_key("escape")
            except Exception:
                pass
            return False
